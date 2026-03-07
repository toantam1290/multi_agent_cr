"""
utils/market_data.py - Fetch market data từ Binance và whale data (free sources)
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional
import httpx
import pandas as pd
import pandas_ta as ta
from loguru import logger

from config import cfg, WHALE_MIN_USD
from models import TechnicalSignal, WhaleSignal, SentimentSignal, DerivativesSignal

# Retry cho timeout/connection (transient errors)
HTTP_TIMEOUT = 15.0
RETRY_MAX = 3
RETRY_DELAYS = (1, 2, 3)
_RETRY_EXC = (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError)


async def _http_get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: Optional[dict] = None,
    max_retries: int = RETRY_MAX,
    delays: tuple = RETRY_DELAYS,
) -> httpx.Response:
    """GET với retry khi gặp ConnectTimeout / ReadTimeout / ConnectError."""
    last: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return await client.get(url, params=params or {})
        except _RETRY_EXC as e:
            last = e
            if attempt < max_retries - 1:
                delay = delays[min(attempt, len(delays) - 1)]
                logger.warning(
                    f"HTTP request failed (attempt {attempt + 1}/{max_retries}): {type(e).__name__}, retry in {delay}s"
                )
                await asyncio.sleep(delay)
            else:
                raise
    if last:
        raise last
    raise RuntimeError("retry loop exited without result")


class BinanceDataFetcher:
    """Fetch OHLCV data và indicators từ Binance public API"""

    BASE_URL = "https://api.binance.com/api/v3"
    TESTNET_URL = "https://testnet.binance.vision/api/v3"
    FUTURES_BASE = "https://fapi.binance.com/fapi/v1"
    FUTURES_TESTNET = "https://testnet.binancefuture.com/fapi/v1"
    FUTURES_DATA_BASE = "https://fapi.binance.com/futures/data"
    FUTURES_DATA_TESTNET = "https://testnet.binancefuture.com/futures/data"

    def __init__(self):
        # Data luôn dùng mainnet (testnet giá giả, derivatives đã mainnet → inconsistent)
        self.base = self.BASE_URL
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        # Futures data luôn dùng mainnet (testnet futures ít liquidity)
        self._futures_base = self.FUTURES_BASE
        self._futures_data_base = self.FUTURES_DATA_BASE

    async def get_klines(self, symbol: str, interval: str, limit: int = 100) -> pd.DataFrame:
        """Lấy candlestick data (có retry khi timeout/connection)."""
        url = f"{self.base}/klines"
        resp = await _http_get_with_retry(
            self._client, url,
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        resp.raise_for_status()

        cols = ["open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades", "taker_base",
                "taker_quote", "ignore"]
        df = pd.DataFrame(resp.json(), columns=cols)

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)

        return df

    async def get_current_price(self, symbol: str) -> float:
        resp = await _http_get_with_retry(
            self._client, f"{self.base}/ticker/price", params={"symbol": symbol}
        )
        resp.raise_for_status()
        return float(resp.json()["price"])

    async def get_24h_stats(self, symbol: str) -> dict:
        resp = await self._client.get(f"{self.base}/ticker/24hr", params={"symbol": symbol})
        resp.raise_for_status()
        d = resp.json()
        return {
            "price_change_pct": float(d["priceChangePercent"]),
            "volume": float(d["volume"]),
            "high": float(d["highPrice"]),
            "low": float(d["lowPrice"]),
            "quote_volume": float(d["quoteVolume"]),
        }

    # ─── Binance Futures (funding, OI, basis) ───────────────────────────────

    async def get_funding_rate(self, symbol: str) -> float:
        """Lấy funding rate 8h mới nhất. Trả về dạng decimal (0.0001 = 0.01%)."""
        try:
            resp = await self._client.get(
                f"{self._futures_base}/premiumIndex",
                params={"symbol": symbol},
            )
            resp.raise_for_status()
            return float(resp.json().get("lastFundingRate", 0))
        except Exception as e:
            logger.warning(f"Funding rate fetch failed ({symbol}): {e}")
            return 0.0

    async def get_open_interest(self, symbol: str) -> tuple[float, float]:
        """(open_interest_contracts, mark_price). OI value = contracts * mark_price."""
        try:
            oi_resp, prem_resp = await asyncio.gather(
                self._client.get(f"{self._futures_base}/openInterest", params={"symbol": symbol}),
                self._client.get(f"{self._futures_base}/premiumIndex", params={"symbol": symbol}),
            )
            oi_resp.raise_for_status()
            prem_resp.raise_for_status()
            oi = float(oi_resp.json().get("openInterest", 0))
            mark = float(prem_resp.json().get("markPrice", 0))
            return oi, mark
        except Exception as e:
            logger.warning(f"Open interest fetch failed ({symbol}): {e}")
            return 0.0, 0.0

    async def get_mark_price(self, symbol: str) -> tuple[float, float]:
        """(mark_price, index_price). Basis = (mark - index) / index * 100."""
        try:
            resp = await self._client.get(
                f"{self._futures_base}/premiumIndex",
                params={"symbol": symbol},
            )
            resp.raise_for_status()
            d = resp.json()
            return float(d.get("markPrice", 0)), float(d.get("indexPrice", 0))
        except Exception as e:
            logger.warning(f"Mark price fetch failed ({symbol}): {e}")
            return 0.0, 0.0

    async def get_derivatives_signal(self, symbol: str) -> DerivativesSignal:
        """Funding + OI + basis → DerivativesSignal. premiumIndex 1 lần (tránh gọi 2x)."""
        try:
            # premiumIndex 1 call → funding + mark + index; openInterest riêng (có retry)
            prem_resp, oi_resp = await asyncio.gather(
                _http_get_with_retry(
                    self._client,
                    f"{self._futures_base}/premiumIndex",
                    params={"symbol": symbol},
                ),
                _http_get_with_retry(
                    self._client,
                    f"{self._futures_base}/openInterest",
                    params={"symbol": symbol},
                ),
            )
            prem_resp.raise_for_status()
            oi_resp.raise_for_status()

            prem = prem_resp.json()
            funding_rate = float(prem.get("lastFundingRate", 0))
            mark_price = float(prem.get("markPrice", 0))
            index_price = float(prem.get("indexPrice", 0))
            oi_contracts = float(oi_resp.json().get("openInterest", 0))

            oi_usdt = oi_contracts * mark_price if mark_price > 0 else 0

            # OI change 24h — openInterestHist period=1d, limit=2
            oi_change_pct = 0.0
            try:
                hist_resp = await _http_get_with_retry(
                    self._client,
                    f"{self._futures_data_base}/openInterestHist",
                    params={"symbol": symbol, "period": "1d", "limit": 2},
                )
                hist_resp.raise_for_status()
                hist = hist_resp.json()
                if len(hist) >= 2:
                    curr = float(hist[0].get("sumOpenInterestValue", 0))
                    prev = float(hist[1].get("sumOpenInterestValue", 0))
                    oi_change_pct = (curr - prev) / prev * 100 if prev > 0 else 0
            except Exception:
                pass

            basis_pct = (mark_price - index_price) / index_price * 100 if index_price > 0 else 0
            funding_annualized = funding_rate * 3 * 365 * 100  # 8h → 365 ngày, %

            # Logic: funding > 0.05% AND basis > 0.2% → SHORT_SQUEEZE; funding < -0.03% AND basis < -0.1% → LONG_SQUEEZE
            signal = "NEUTRAL"
            score = 0
            if funding_rate > 0.0005 and basis_pct > 0.2:
                signal = "SHORT_SQUEEZE"
                score = -50
            elif funding_rate < -0.0003 and basis_pct < -0.1:
                signal = "LONG_SQUEEZE"
                score = 50

            logger.info(
                f"Derivatives ({symbol}): funding={funding_rate*100:.3f}% "
                f"basis={basis_pct:.2f}% OI=${oi_usdt/1e6:.1f}M signal={signal}"
            )
            return DerivativesSignal(
                funding_rate=funding_rate,
                funding_rate_annualized=funding_annualized,
                open_interest_usdt=oi_usdt,
                oi_change_pct=oi_change_pct,
                basis_pct=basis_pct,
                signal=signal,
                score=score,
            )
        except Exception as e:
            logger.warning(f"Derivatives signal failed ({symbol}): {e}")
            # funding_rate=0.0005 (0.05%) → không pass LONG (<0.05) cũng không pass SHORT (>0.05)
            return DerivativesSignal(funding_rate=0.0005)

    async def compute_technical_signal(self, symbol: str) -> TechnicalSignal:
        """Tính toán tất cả technical indicators"""
        logger.info(f"Computing technical signals for {symbol}")

        # Fetch data ở nhiều timeframes
        df_1h, df_4h, df_1d = await asyncio.gather(
            self.get_klines(symbol, "1h", 100),
            self.get_klines(symbol, "4h", 100),
            self.get_klines(symbol, "1d", 210),  # Cần ≥200 cho EMA200
        )

        # RSI
        rsi_1h = float(ta.rsi(df_1h["close"], length=14).iloc[-1])
        rsi_4h = float(ta.rsi(df_4h["close"], length=14).iloc[-1])

        # EMA crossover (EMA9 vs EMA21)
        ema9 = ta.ema(df_1h["close"], length=9)
        ema21 = ta.ema(df_1h["close"], length=21)
        ema_cross_bullish = (
            float(ema9.iloc[-1]) > float(ema21.iloc[-1]) and
            float(ema9.iloc[-2]) <= float(ema21.iloc[-2])
        )
        ema_cross_bearish = (
            float(ema9.iloc[-1]) < float(ema21.iloc[-1]) and
            float(ema9.iloc[-2]) >= float(ema21.iloc[-2])
        )

        # MACD
        macd_df = ta.macd(df_1h["close"])
        macd_bullish = False
        macd_bearish = False
        if macd_df is not None and not macd_df.empty:
            macd_line = macd_df.iloc[:, 0]
            signal_line = macd_df.iloc[:, 2]
            macd_bullish = float(macd_line.iloc[-1]) > float(signal_line.iloc[-1])
            macd_bearish = float(macd_line.iloc[-1]) < float(signal_line.iloc[-1])

        # Volume spike (volume > 2x average của 20 nến trước)
        avg_volume = df_1h["volume"].iloc[-21:-1].mean()
        current_volume = float(df_1h["volume"].iloc[-1])
        volume_spike = current_volume > avg_volume * 2

        # Bollinger Bands squeeze + width (regime)
        bb = ta.bbands(df_1h["close"], length=20)
        bb_squeeze = False
        bb_width = 0.0
        if bb is not None and not bb.empty:
            bandwidth = (bb.iloc[-1, 2] - bb.iloc[-1, 0]) / bb.iloc[-1, 1]  # BBU - BBL / BBM
            bb_squeeze = bandwidth < 0.02  # Bandwidth < 2%
            bb_width = float(bandwidth)

        # Support/Resistance (simplified: recent swing high/low)
        recent_high = float(df_1d["high"].iloc[-20:].max())
        recent_low = float(df_1d["low"].iloc[-20:].min())

        # Trend xác định bằng EMA 50 so với EMA 200 trên 1D
        ema50_1d = ta.ema(df_1d["close"], length=50)
        ema200_1d = ta.ema(df_1d["close"], length=200)
        if ema50_1d is not None and ema200_1d is not None:
            e50 = float(ema50_1d.iloc[-1])
            e200 = float(ema200_1d.iloc[-1])
            if e50 > e200 * 1.01:
                trend_1d = "uptrend"
            elif e50 < e200 * 0.99:
                trend_1d = "downtrend"
            else:
                trend_1d = "sideways"
        else:
            trend_1d = "sideways"

        # Bullish vs Bearish score (net_score -100 to +100)
        bullish = 0
        bearish = 0
        if 30 <= rsi_1h <= 70:
            bullish += 8
            bearish += 8
        if rsi_1h < 40:
            bullish += 20  # Oversold = long
        if rsi_1h > 70:
            bearish += 20  # Overbought = short
        if ema_cross_bullish:
            bullish += 25
        if ema_cross_bearish:
            bearish += 25
        if macd_bullish:
            bullish += 20
        if macd_bearish:
            bearish += 20
        if volume_spike:
            bullish += 5
            bearish += 5
        if trend_1d == "uptrend":
            bullish += 10
        if trend_1d == "downtrend":
            bearish += 10

        net_score = max(-100, min(100, bullish - bearish))
        direction_bias = "LONG" if net_score > 10 else ("SHORT" if net_score < -10 else "NEUTRAL")
        score = max(0, min(100, net_score + 50))  # Legacy: map -100..100 to 0..100

        # ATR (1h), ADX (4h), ATR ratio
        atr14 = ta.atr(df_1h["high"], df_1h["low"], df_1h["close"], length=14)
        atr50 = ta.atr(df_1h["high"], df_1h["low"], df_1h["close"], length=50)
        atr_value = float(atr14.iloc[-1]) if atr14 is not None and not atr14.empty else 0.0
        atr50_val = float(atr50.iloc[-1]) if atr50 is not None and not atr50.empty else atr_value
        atr_ratio = atr_value / atr50_val if atr50_val > 0 else 0.0
        current_price_1h = float(df_1h["close"].iloc[-1])
        atr_pct = atr_value / current_price_1h * 100 if current_price_1h > 0 else 0.0

        # ADX trên 4h (ổn định hơn 1h). pandas-ta: ADX_14, DMP_14, DMN_14
        adx_df = ta.adx(df_4h["high"], df_4h["low"], df_4h["close"], length=14)
        adx_val = 0.0
        plus_di_val = 0.0
        minus_di_val = 0.0
        if adx_df is not None and not adx_df.empty:
            cols = adx_df.columns.tolist()
            adx_col = next((c for c in cols if "ADX" in str(c)), cols[0] if cols else None)
            dmp_col = next((c for c in cols if "DMP" in str(c)), cols[1] if len(cols) > 1 else None)
            dmn_col = next((c for c in cols if "DMN" in str(c)), cols[2] if len(cols) > 2 else None)
            if adx_col is not None:
                adx_val = float(adx_df[adx_col].iloc[-1])
            if dmp_col is not None:
                plus_di_val = float(adx_df[dmp_col].iloc[-1])
            if dmn_col is not None:
                minus_di_val = float(adx_df[dmn_col].iloc[-1])

        return TechnicalSignal(
            rsi_1h=rsi_1h,
            rsi_4h=rsi_4h,
            ema_cross_bullish=ema_cross_bullish,
            macd_bullish=macd_bullish,
            volume_spike=volume_spike,
            bb_squeeze=bb_squeeze,
            support_level=recent_low,
            resistance_level=recent_high,
            trend_1d=trend_1d,
            score=score,
            net_score=net_score,
            direction_bias=direction_bias,
            atr_value=atr_value,
            atr_pct=atr_pct,
            atr_ratio=atr_ratio,
            adx=adx_val,
            plus_di=plus_di_val,
            minus_di=minus_di_val,
            bb_width=bb_width,
        )

    async def close(self):
        await self._client.aclose()


def classify_regime(
    adx: float,
    plus_di: float,
    minus_di: float,
    bb_width: float,
    atr_ratio: float,
) -> str:
    """
    Regime từ ADX + BB Width + ATR ratio.
    Returns: trending_up | trending_down | ranging | volatile
    """
    if atr_ratio > 1.5:
        return "volatile"
    if adx > 25:
        return "trending_up" if plus_di > minus_di else "trending_down"
    if adx < 20 and bb_width < 0.03:
        return "ranging"
    return "ranging"


def calc_entry_sl_tp(
    direction: str,
    current_price: float,
    atr_value: float,
    regime: str,
) -> tuple[float, float, float]:
    """
    Rule-based entry/SL/TP. ATR multiplier: 1.5 (trending) / 1.2 (ranging).
    Returns (entry, sl, tp).
    """
    mult = 1.5 if regime in ("trending_up", "trending_down") else 1.2
    entry = current_price
    if direction == "LONG":
        sl = entry - mult * atr_value
        tp = entry + 2.0 * (entry - sl)
    else:
        sl = entry + mult * atr_value
        tp = entry - 2.0 * (sl - entry)
    return entry, sl, tp


class WhaleDataFetcher:
    """
    Whale signals từ 3 nguồn free, không cần key:
    1. Binance aggTrades     - large trades trên Binance
    2. Binance orderbook     - large bid/ask walls
    3. Mempool.space         - BTC on-chain (BTC pairs only)
    """

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=12.0)

    async def get_whale_transactions(
        self,
        symbol: str = "BTCUSDT",
        min_usd: int = WHALE_MIN_USD,
        hours_back: int = 4,
    ) -> WhaleSignal:
        coin = symbol.lower().replace("usdt", "")

        tasks = [
            self._fetch_binance_large_trades(symbol, min_usd),
            self._fetch_binance_orderbook_walls(symbol),
        ]
        if coin == "btc":
            tasks.append(self._fetch_mempool_space())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_data = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"Whale source error: {r}")
                continue
            if isinstance(r, list):
                all_data.extend(r)

        return self._build_signal(all_data, symbol)

    async def _fetch_binance_large_trades(
        self, symbol: str, min_usd: float
    ) -> list[dict]:
        """
        Public endpoint, không cần key.
        isBuyerMaker=True  → seller chủ động → sell pressure
        isBuyerMaker=False → buyer chủ động → buy pressure
        """
        try:
            price_resp, trades_resp = await asyncio.gather(
                _http_get_with_retry(
                    self._client,
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": symbol},
                ),
                _http_get_with_retry(
                    self._client,
                    "https://api.binance.com/api/v3/aggTrades",
                    params={"symbol": symbol, "limit": 1000},
                ),
            )
            current_price = float(price_resp.json()["price"])
            trades = trades_resp.json()

            result = []
            for t in trades:
                qty = float(t["q"])
                usd_val = qty * current_price
                if usd_val < min_usd:
                    continue
                is_buyer_maker = t["m"]
                result.append({
                    "source": "binance_trades",
                    "usd": usd_val,
                    "buy_pressure": not is_buyer_maker,
                    "from_type": "seller" if is_buyer_maker else "buyer",
                    "to_type": "exchange",
                    "hash": str(t["a"]),
                })

            logger.info(f"Binance aggTrades: {len(result)} large trades ({symbol})")
            return result

        except Exception as e:
            logger.warning(f"Binance aggTrades failed: {e}")
            return []

    async def _fetch_binance_orderbook_walls(
        self, symbol: str, wall_usd_threshold: float = 2_000_000
    ) -> list[dict]:
        """
        Phát hiện bid/ask walls lớn trong orderbook.
        Bid wall lớn = whale đang đỡ giá → bullish
        Ask wall lớn = whale đang chặn giá → bearish
        """
        try:
            resp, price_resp = await asyncio.gather(
                _http_get_with_retry(
                    self._client,
                    "https://api.binance.com/api/v3/depth",
                    params={"symbol": symbol, "limit": 100},
                ),
                _http_get_with_retry(
                    self._client,
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": symbol},
                ),
            )
            resp.raise_for_status()
            book = resp.json()
            current_price = float(price_resp.json()["price"])

            result = []

            for price_str, qty_str in book["bids"]:
                price = float(price_str)
                qty = float(qty_str)
                usd_val = price * qty
                if usd_val >= wall_usd_threshold:
                    distance_pct = (current_price - price) / current_price * 100
                    result.append({
                        "source": "orderbook_bid",
                        "usd": usd_val,
                        "buy_pressure": True,
                        "from_type": "exchange",
                        "to_type": "buyer",
                        "distance_pct": distance_pct,
                        "hash": f"bid_{price_str}",
                    })

            for price_str, qty_str in book["asks"]:
                price = float(price_str)
                qty = float(qty_str)
                usd_val = price * qty
                if usd_val >= wall_usd_threshold:
                    distance_pct = (price - current_price) / current_price * 100
                    result.append({
                        "source": "orderbook_ask",
                        "usd": usd_val,
                        "buy_pressure": False,
                        "from_type": "seller",
                        "to_type": "exchange",
                        "distance_pct": distance_pct,
                        "hash": f"ask_{price_str}",
                    })

            bid_walls = [r for r in result if r["source"] == "orderbook_bid"]
            ask_walls = [r for r in result if r["source"] == "orderbook_ask"]
            logger.info(
                f"Orderbook walls ({symbol}): "
                f"{len(bid_walls)} bid walls, {len(ask_walls)} ask walls"
            )
            return result

        except Exception as e:
            logger.warning(f"Orderbook fetch failed: {e}")
            return []

    async def _fetch_mempool_space(self, min_btc: float = 100.0) -> list[dict]:
        """
        mempool.space public API — hoàn toàn free, không cần key.
        Track BTC large transactions trong confirmed blocks.
        """
        try:
            # Lấy giá BTC thực từ Binance (không hardcode $95k)
            btc_price = 95000.0  # fallback
            try:
                pr = await self._client.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": "BTCUSDT"},
                )
                if pr.status_code == 200:
                    btc_price = float(pr.json().get("price", 95000))
            except Exception:
                pass

            tip_resp = await self._client.get(
                "https://mempool.space/api/blocks/tip/height"
            )
            tip_resp.raise_for_status()
            tip_height = int(tip_resp.text)

            result = []
            for height in range(tip_height, max(0, tip_height - 3), -1):
                try:
                    # block-height → hash, block/hash/txs → transactions (25 per page)
                    hash_resp = await self._client.get(
                        f"https://mempool.space/api/block-height/{height}"
                    )
                    if hash_resp.status_code != 200:
                        continue
                    block_hash = hash_resp.text.strip()

                    txs_resp = await self._client.get(
                        f"https://mempool.space/api/block/{block_hash}/txs"
                    )
                    if txs_resp.status_code != 200:
                        continue
                    txs = txs_resp.json()

                    for tx in txs:
                        total_btc = sum(
                            vout.get("value", 0)
                            for vout in tx.get("vout", [])
                        ) / 1e8  # satoshi → BTC

                        if total_btc < min_btc:
                            continue

                        usd_approx = total_btc * btc_price

                        result.append({
                            "source": "mempool_btc",
                            "usd": usd_approx,
                            "btc": total_btc,
                            "buy_pressure": False,
                            "from_type": "unknown",
                            "to_type": "unknown",
                            "hash": tx.get("txid", "")[:16],
                        })
                except Exception:
                    continue

            logger.info(f"Mempool.space: {len(result)} large BTC txs")
            return result

        except Exception as e:
            logger.warning(f"Mempool.space failed: {e}")
            return []

    def _build_signal(self, all_data: list[dict], symbol: str) -> WhaleSignal:
        if not all_data:
            return WhaleSignal(score=50)

        total_usd = 0.0
        buy_usd = 0.0
        sell_usd = 0.0
        bid_wall_usd = 0.0
        ask_wall_usd = 0.0
        top_transfers = []

        for d in all_data:
            usd = d["usd"]
            total_usd += usd
            source = d["source"]

            if source == "orderbook_bid":
                bid_wall_usd += usd
            elif source == "orderbook_ask":
                ask_wall_usd += usd
            elif d.get("buy_pressure"):
                buy_usd += usd
            else:
                sell_usd += usd

            if usd > 3_000_000 and source not in ("orderbook_bid", "orderbook_ask"):
                top_transfers.append({
                    "usd": usd,
                    "source": source,
                    "from": d["from_type"],
                    "to": d["to_type"],
                    "hash": d.get("hash", "")[:16],
                })

        trade_total = buy_usd + sell_usd
        buy_ratio = buy_usd / trade_total if trade_total > 0 else 0.5

        wall_total = bid_wall_usd + ask_wall_usd
        bid_dominance = bid_wall_usd / wall_total if wall_total > 0 else 0.5

        trade_score = int(buy_ratio * 100)
        wall_score = int(bid_dominance * 100)
        score = int(trade_score * 0.5 + wall_score * 0.5)

        net_flow = buy_usd - sell_usd

        logger.info(
            f"Whale signal ({symbol}): "
            f"buy=${buy_usd/1e6:.1f}M sell=${sell_usd/1e6:.1f}M "
            f"bid_walls=${bid_wall_usd/1e6:.1f}M ask_walls=${ask_wall_usd/1e6:.1f}M "
            f"score={score}"
        )

        return WhaleSignal(
            large_transfers_count=len(
                [d for d in all_data if d["source"] not in ("orderbook_bid", "orderbook_ask")]
            ),
            large_transfers_usd=total_usd,
            exchange_inflow_usd=sell_usd,
            exchange_outflow_usd=buy_usd,
            top_transfers=sorted(top_transfers, key=lambda x: x["usd"], reverse=True)[:5],
            net_flow=net_flow,
            score=score,
        )

    async def close(self):
        await self._client.aclose()


class FearGreedFetcher:
    """Fetch Fear & Greed Index từ alternative.me"""

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

    async def get(self) -> SentimentSignal:
        try:
            resp = await _http_get_with_retry(
                self._client, "https://api.alternative.me/fng/?limit=1"
            )
            resp.raise_for_status()
            data = resp.json()["data"][0]
            value = int(data["value"])
            label = data["value_classification"]

            # Score: 0-40 fear = bullish opportunity, 60-100 greed = risky
            if value < 20:
                score = 85  # Extreme fear = good buy
            elif value < 40:
                score = 70  # Fear = ok
            elif value < 60:
                score = 50  # Neutral
            elif value < 80:
                score = 35  # Greed = careful
            else:
                score = 15  # Extreme greed = danger

            return SentimentSignal(
                fear_greed_index=value,
                fear_greed_label=label,
                score=score,
            )
        except Exception as e:
            logger.warning(f"Fear & Greed fetch failed: {e}")
            return SentimentSignal(fear_greed_index=50, fear_greed_label="Neutral", score=50)

    async def close(self):
        await self._client.aclose()
