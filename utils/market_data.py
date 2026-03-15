"""
utils/market_data.py - Fetch market data từ Binance và whale data (free sources)
"""
import asyncio
import os
from datetime import datetime, timedelta
from typing import Optional
import httpx
import pandas as pd
import pandas_ta as ta
from loguru import logger

from config import cfg, WHALE_MIN_USD
from models import TechnicalSignal, WhaleSignal, SentimentSignal, DerivativesSignal

# Retry cho timeout/connection (transient errors)
# HTTP_TIMEOUT_SEC: tăng lên 30 nếu mạng chậm / Binance bị throttle (ConnectTimeout)
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SEC", "30"))
RETRY_MAX = 3
RETRY_DELAYS = (1, 2, 3)
_RETRY_EXC = (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError)

# Global rate limiter: max concurrent HTTP requests (tránh Binance 429/418 IP ban)
# Binance futures: 2400 weight/min. Semaphore 8 giới hạn burst, backoff khi 429/418
_GLOBAL_HTTP_SEMAPHORE = asyncio.Semaphore(5)
_rate_limit_backoff: float = 0  # seconds, tăng khi gặp 429/418


class RateLimitedClient:
    """Wrapper httpx.AsyncClient — mọi .get() đi qua global semaphore + auto backoff."""

    def __init__(self, client: httpx.AsyncClient):
        self._inner = client

    async def get(self, url: str, params: dict | None = None, **kwargs) -> httpx.Response:
        global _rate_limit_backoff
        async with _GLOBAL_HTTP_SEMAPHORE:
            if _rate_limit_backoff > 0:
                await asyncio.sleep(_rate_limit_backoff)
            resp = await self._inner.get(url, params=params, **kwargs)
            if resp.status_code in (429, 418):
                _rate_limit_backoff = min(30, _rate_limit_backoff + 5) if _rate_limit_backoff else 10
                logger.warning(
                    f"Rate limited ({resp.status_code}), backoff {_rate_limit_backoff:.0f}s: "
                    f"{url.split('/')[-1].split('?')[0]}"
                )
                await asyncio.sleep(_rate_limit_backoff)
            elif _rate_limit_backoff > 0:
                _rate_limit_backoff = max(0, _rate_limit_backoff - 1)  # Gradually reduce
            return resp

    # Proxy other attributes to inner client
    async def aclose(self):
        await self._inner.aclose()

    @property
    def is_closed(self):
        return self._inner.is_closed


def _create_client() -> RateLimitedClient:
    """Tạo rate-limited HTTP client cho Binance API."""
    return RateLimitedClient(httpx.AsyncClient(timeout=HTTP_TIMEOUT))


async def _http_get_with_retry(
    client: RateLimitedClient | httpx.AsyncClient,
    url: str,
    params: Optional[dict] = None,
    max_retries: int = RETRY_MAX,
    delays: tuple = RETRY_DELAYS,
) -> httpx.Response:
    """GET với retry khi gặp ConnectTimeout / ReadTimeout / ConnectError / 429 / 418."""
    last: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            resp = await client.get(url, params=params or {})
            # Rate limit: retry nếu còn attempt
            if resp.status_code in (429, 418) and attempt < max_retries - 1:
                await asyncio.sleep(min(15, 5 * (attempt + 1)))
                continue
            return resp
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
        self.base = self.FUTURES_BASE
        self._client = _create_client()
        # Futures data luôn dùng mainnet (testnet futures ít liquidity)
        self._futures_base = self.FUTURES_BASE
        self._futures_data_base = self.FUTURES_DATA_BASE
        # Cache valid futures symbols — populated by get_premium_index_full()
        self._futures_symbols: set[str] = set()
        # Auto-blacklist symbols that return 400 on openInterest (settled/delisted)
        self._oi_blacklist: set[str] = set()

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

    async def get_orderbook_spread_pct(self, symbol: str) -> float:
        """Spread % = (best_ask - best_bid) / best_bid * 100. > 0.05% → illiquid."""
        data = await self.get_orderbook_data(symbol)
        return data["spread_pct"]

    async def get_orderbook_data(self, symbol: str) -> dict:
        """
        Fetch 1 lần, trả về spread + imbalance.
        Imbalance = bid_volume_5_levels / ask_volume_5_levels
        > 1.5 = buyer stack nặng = LONG bias, < 0.7 = SHORT bias
        """
        try:
            resp = await _http_get_with_retry(
                self._client, f"{self.base}/depth", params={"symbol": symbol, "limit": 5}
            )
            resp.raise_for_status()
            book = resp.json()
            bids = book.get("bids", [])[:5]
            asks = book.get("asks", [])[:5]
            if not bids or not asks:
                return {"spread_pct": 999.0, "imbalance": 1.0, "bid_stack": 0.0, "ask_stack": 0.0}
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            spread_pct = (best_ask - best_bid) / best_bid * 100 if best_bid > 0 else 999.0
            bid_stack = sum(float(b[1]) for b in bids)
            ask_stack = sum(float(a[1]) for a in asks)
            imbalance = bid_stack / ask_stack if ask_stack > 0 else 1.0
            return {
                "spread_pct": spread_pct,
                "imbalance": imbalance,
                "bid_stack": bid_stack,
                "ask_stack": ask_stack,
            }
        except Exception as e:
            logger.warning(f"get_orderbook_data failed for {symbol}: {e}")
            return {"spread_pct": 999.0, "imbalance": 1.0, "bid_stack": 0.0, "ask_stack": 0.0}

    async def get_cvd_signal(self, symbol: str, limit: int = 500, use_futures: bool = False) -> dict:
        """
        CVD từ aggTrades — buy_vol - sell_vol.
        isBuyerMaker=False (m=0) → market BUY → CVD tăng
        isBuyerMaker=True (m=1) → market SELL → CVD giảm

        use_futures=True: dùng futures aggTrades (cho SMC confluence với funding/OI).
        use_futures=False: spot aggTrades (mặc định, backward compatible).
        """
        base_url = self._futures_base if use_futures else self.base
        try:
            resp = await _http_get_with_retry(
                self._client, f"{base_url}/aggTrades", params={"symbol": symbol, "limit": limit}
            )
            resp.raise_for_status()
            trades = resp.json()
            if not trades:
                return {"cvd": 0.0, "cvd_ratio": 0.5, "buy_vol": 0.0, "sell_vol": 0.0, "cvd_trend": "neutral"}

            buy_vol = sum(float(t["q"]) for t in trades if not t["m"])
            sell_vol = sum(float(t["q"]) for t in trades if t["m"])
            total_vol = buy_vol + sell_vol
            cvd = buy_vol - sell_vol
            cvd_ratio = buy_vol / total_vol if total_vol > 0 else 0.5

            mid = len(trades) // 2
            early_buy = sum(float(t["q"]) for t in trades[:mid] if not t["m"])
            early_sell = sum(float(t["q"]) for t in trades[:mid] if t["m"])
            late_buy = sum(float(t["q"]) for t in trades[mid:] if not t["m"])
            late_sell = sum(float(t["q"]) for t in trades[mid:] if t["m"])
            early_cvd = early_buy - early_sell
            late_cvd = late_buy - late_sell

            # Delta-based: tránh bug khi early_cvd âm (nhân 1.2 đảo chiều so sánh)
            threshold = total_vol * 0.05
            if late_cvd - early_cvd > threshold:
                cvd_trend = "accelerating_buy"
            elif early_cvd - late_cvd > threshold:
                cvd_trend = "accelerating_sell"
            else:
                cvd_trend = "neutral"

            logger.debug(f"{symbol} CVD | buy={buy_vol:.1f} sell={sell_vol:.1f} ratio={cvd_ratio:.2f} trend={cvd_trend}")
            return {
                "cvd": cvd,
                "cvd_ratio": cvd_ratio,
                "buy_vol": buy_vol,
                "sell_vol": sell_vol,
                "cvd_trend": cvd_trend,
            }
        except Exception as e:
            logger.warning(f"get_cvd_signal failed for {symbol}: {e}")
            return {"cvd": 0.0, "cvd_ratio": 0.5, "buy_vol": 0.0, "sell_vol": 0.0, "cvd_trend": "neutral"}

    async def get_24h_stats(self, symbol: str, use_futures: bool = False) -> dict:
        """
        24h ticker stats. use_futures=True: futures price (cho OI×Price logic, khớp với derivatives).
        """
        base_url = self._futures_base if use_futures else self.base
        resp = await _http_get_with_retry(self._client, f"{base_url}/ticker/24hr", params={"symbol": symbol})
        resp.raise_for_status()
        d = resp.json()
        return {
            "price_change_pct": float(d["priceChangePercent"]),
            "volume": float(d["volume"]),
            "high": float(d["highPrice"]),
            "low": float(d["lowPrice"]),
            "quote_volume": float(d["quoteVolume"]),
        }

    async def get_all_tickers_24hr(self) -> list[dict]:
        """Lấy tất cả ticker 24h (futures). Không truyền symbol = all pairs."""
        try:
            resp = await _http_get_with_retry(
                self._client,
                f"{self.base}/ticker/24hr",
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"get_all_tickers_24hr failed: {e}")
            return []

    async def get_premium_index_full(self) -> list[dict]:
        """
        Lấy full premiumIndex (futures) — 1 request, trả toàn bộ symbols.
        Từ đó derive: futures_symbols = set(s['symbol']), funding_map = {s: lastFundingRate}.
        Nếu fail -> return [].
        """
        try:
            resp = await _http_get_with_retry(
                self._client,
                f"{self._futures_base}/premiumIndex",
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                # Populate futures_symbols cache; enrich with exchangeInfo for TRADING status
                self._futures_symbols = {p["symbol"] for p in data if "symbol" in p}
                await self._load_tradable_symbols()
                return data
            return []
        except Exception as e:
            logger.warning(f"get_premium_index_full failed: {e}")
            return []

    async def _load_tradable_symbols(self) -> None:
        """Load only TRADING status symbols from exchangeInfo — filter out SETTLING/CLOSE."""
        try:
            resp = await _http_get_with_retry(
                self._client,
                f"{self._futures_base}/exchangeInfo",
            )
            resp.raise_for_status()
            info = resp.json()
            trading = {
                s["symbol"] for s in info.get("symbols", [])
                if s.get("status") == "TRADING" and s.get("symbol", "").endswith("USDT")
            }
            if trading:
                removed = self._futures_symbols - trading
                if removed:
                    logger.info(f"Filtered {len(removed)} non-TRADING futures symbols")
                self._futures_symbols = trading
        except Exception as e:
            logger.warning(f"_load_tradable_symbols failed: {e}")

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
        if self._futures_symbols and symbol not in self._futures_symbols:
            return 0.0, 0.0
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
        # Skip delisted/settled futures pairs — tránh 400 Bad Request thừa
        if self._futures_symbols and symbol not in self._futures_symbols:
            return DerivativesSignal(funding_rate=0.0005, fetch_ok=False)
        if symbol in self._oi_blacklist:
            return DerivativesSignal(funding_rate=0.0005, fetch_ok=False)
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
            # Auto-blacklist symbols returning 400 (settled/delisted but still in premiumIndex)
            if oi_resp.status_code == 400:
                self._oi_blacklist.add(symbol)
                logger.info(f"Derivatives ({symbol}): OI returned 400, auto-blacklisted")
                return DerivativesSignal(funding_rate=0.0005, fetch_ok=False)
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
                    prev = float(hist[0].get("sumOpenInterestValue", 0))  # ascending: hist[0]=cũ
                    curr = float(hist[1].get("sumOpenInterestValue", 0))  # hist[1]=mới nhất
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
            return DerivativesSignal(funding_rate=0.0005, fetch_ok=False)

    async def compute_technical_signal(self, symbol: str, style: str = "swing") -> TechnicalSignal:
        """
        Tính toán technical indicators.
        style: swing -> 1h/4h/1d (majors, hold longer)
        style: scalp -> 15m direction, 5m timing+ATR, 1h ADX, 4h trend (Option A)
        """
        if style not in ("swing", "scalp"):
            style = "swing"

        logger.info(f"Computing technical signals for {symbol} ({style})")

        import pandas as _pd  # Local import tránh Python 3.12 nested scope issue với pd

        if style == "swing":
            df_fast, df_slow, df_trend = await asyncio.gather(
                self.get_klines(symbol, "1h", 100),
                self.get_klines(symbol, "4h", 100),
                self.get_klines(symbol, "1d", 400),  # EMA200 cần ~400 candle warm-up
            )
            df_atr = df_fast  # swing: ATR từ 1h
            df_adx = df_slow # swing: ADX từ 4h
            min_rows = 50
        else:
            # scalp Option A: 15m direction, 5m timing+ATR, 1h ADX, 4h trend
            df_5m, df_15m, df_1h, df_4h = await asyncio.gather(
                self.get_klines(symbol, "5m", 100),
                self.get_klines(symbol, "15m", 100),
                self.get_klines(symbol, "1h", 100),
                self.get_klines(symbol, "4h", 100),
            )
            df_fast = df_15m  # direction + setup: RSI, EMA, MACD, volume, BB
            df_atr = df_5m    # ATR SL/TP + RSI 5m pullback timing
            df_slow = df_4h   # rsi_4h = RSI-14 trên 4h thực tế
            df_trend = df_4h
            df_adx = df_1h
            min_rows = 50

        if len(df_fast) < min_rows or len(df_slow) < min_rows or len(df_trend) < 50:
            raise ValueError(f"Insufficient data for {symbol}")
        if style == "scalp" and (len(df_atr) < min_rows or len(df_adx) < min_rows):
            raise ValueError(f"Insufficient data for {symbol}")

        def _calc_chop_index(df, period: int = 14) -> float:
            """Chop Index: < 38.2 trending, > 61.8 choppy. Dùng pandas_ta.chop."""
            chop_series = ta.chop(df["high"], df["low"], df["close"], length=period, atr_length=1)
            if chop_series is None or chop_series.empty or _pd.isna(chop_series.iloc[-1]):
                return 50.0
            return float(chop_series.iloc[-1])

        def _safe_rsi(df, length: int = 14, default: float = 50.0) -> float:
            s = ta.rsi(df["close"], length=length)
            if s is None or s.empty or _pd.isna(s.iloc[-1]):
                return default
            return float(s.iloc[-1])

        # RSI: rsi_1h = fast TF, rsi_4h = slow TF
        rsi_1h = _safe_rsi(df_fast, 14)
        rsi_4h = _safe_rsi(df_slow, 14)

        # EMA crossover (EMA9 vs EMA21) trên fast TF — cần price confirm (close > EMA21)
        ema9 = ta.ema(df_fast["close"], length=9)
        ema21 = ta.ema(df_fast["close"], length=21)
        ema_cross_bullish = False
        ema_cross_bearish = False
        ema9_just_crossed_up = False
        ema9_just_crossed_down = False
        ema9_crossed_recent_up = False
        ema9_crossed_recent_down = False
        if ema9 is not None and ema21 is not None and len(ema9) >= 6 and len(ema21) >= 6:
            e9, e21 = float(ema9.iloc[-1]), float(ema21.iloc[-1])
            close = float(df_fast["close"].iloc[-1])
            prev_close = float(df_fast["close"].iloc[-2])
            prev_e9 = float(ema9.iloc[-2])
            cross_bull = e9 > e21 and float(ema9.iloc[-2]) <= float(ema21.iloc[-2])
            cross_bear = e9 < e21 and float(ema9.iloc[-2]) >= float(ema21.iloc[-2])
            ema_cross_bullish = cross_bull and close > e21  # Price confirm
            ema_cross_bearish = cross_bear and close < e21
            # Entry timing: close vừa cross EMA9 (legacy, quá strict)
            ema9_just_crossed_up = close > e9 and prev_close <= prev_e9
            ema9_just_crossed_down = close < e9 and prev_close >= prev_e9
            # Nới: cross trong 3 nến đã đóng gần nhất (iloc[-2,-3,-4]) — tránh nến chưa đóng
            for i in range(2, 5):  # i=2,3,4 → check candles -2,-3,-4 (đã đóng)
                if len(ema9) <= i + 1 or len(df_fast["close"]) <= i + 1:
                    break
                c, c_prev = float(df_fast["close"].iloc[-i]), float(df_fast["close"].iloc[-i - 1])
                e, e_prev = float(ema9.iloc[-i]), float(ema9.iloc[-i - 1])
                if c > e and c_prev <= e_prev:
                    ema9_crossed_recent_up = True
                if c < e and c_prev >= e_prev:
                    ema9_crossed_recent_down = True

        # Swing structure (10 nến gần nhất) — cho SL từ structure thay vì ATR flat
        recent_atr = df_atr.iloc[-10:] if len(df_atr) >= 10 else df_atr
        swing_low = float(recent_atr["low"].min()) if len(recent_atr) > 0 else 0.0
        swing_high = float(recent_atr["high"].max()) if len(recent_atr) > 0 else 0.0

        # VWAP session-anchored: tính từ midnight UTC hôm nay (không phải rolling N candles)
        vwap_val = 0.0
        vwap_distance_pct = 0.0
        if len(df_atr) > 0 and df_atr["volume"].sum() > 0:
            try:
                import pandas as pd
                today_utc = _pd.Timestamp.now(tz="UTC").normalize()  # Midnight UTC
                if hasattr(df_atr.index, "tz") and df_atr.index.tz is not None:
                    df_today = df_atr[df_atr.index >= today_utc]
                else:
                    df_today = df_atr[df_atr.index >= today_utc.tz_localize(None)]
                # Fallback nếu không đủ candles hôm nay
                if len(df_today) < 10:
                    df_today = df_atr
            except Exception:
                df_today = df_atr
            typical = (df_today["high"] + df_today["low"] + df_today["close"]) / 3
            vol_sum = df_today["volume"].sum()
            if vol_sum > 0:
                vwap_val = float((typical * df_today["volume"]).sum() / vol_sum)
            current_price_val = float(df_atr["close"].iloc[-1])
            if vwap_val > 0:
                vwap_distance_pct = (current_price_val - vwap_val) / vwap_val * 100

        # MACD
        macd_df = ta.macd(df_fast["close"])
        macd_bullish = False
        macd_bearish = False
        if macd_df is not None and not macd_df.empty:
            macd_line = macd_df.iloc[:, 0]
            signal_line = macd_df.iloc[:, 2]
            macd_bullish = float(macd_line.iloc[-1]) > float(signal_line.iloc[-1])
            macd_bearish = float(macd_line.iloc[-1]) < float(signal_line.iloc[-1])

        # Volume spike: dùng nến đã đóng (iloc[-2]). avg = 20 nến TRƯỚC iloc[-2] (không bao gồm prev)
        vol_slice = df_fast["volume"].iloc[-22:-2]
        avg_volume = vol_slice.mean() if len(vol_slice) > 0 else 0.0
        prev_volume = float(df_fast["volume"].iloc[-2]) if len(df_fast) >= 2 else 0.0
        volume_spike = avg_volume > 0 and prev_volume > avg_volume * 2
        volume_ratio = prev_volume / avg_volume if avg_volume > 0 else 0.0
        # Volume trend: 3 nến đóng tăng liên tiếp + v2 > 50% avg (tránh micro-volume pass)
        volume_trend_up = False
        if len(df_fast) >= 4 and avg_volume > 0:
            v4, v3, v2 = float(df_fast["volume"].iloc[-4]), float(df_fast["volume"].iloc[-3]), float(df_fast["volume"].iloc[-2])
            volume_trend_up = (v4 < v3 < v2) and (v2 > avg_volume * 0.5)

        # Bollinger Bands squeeze + width (regime)
        bb = ta.bbands(df_fast["close"], length=20)
        bb_squeeze = False
        bb_width = 0.0
        if bb is not None and not bb.empty:
            bandwidth = (bb.iloc[-1, 2] - bb.iloc[-1, 0]) / bb.iloc[-1, 1]  # BBU - BBL / BBM
            bb_squeeze = bandwidth < 0.02  # Bandwidth < 2%
            bb_width = float(bandwidth)

        # Support/Resistance (simplified: recent swing high/low)
        recent_high = float(df_trend["high"].iloc[-20:].max())
        recent_low = float(df_trend["low"].iloc[-20:].min())

        # Trend: swing = EMA50 vs EMA200 trên 1D; scalp = EMA20 vs EMA50 trên 4h
        if style == "swing":
            ema_short = ta.ema(df_trend["close"], length=50)
            ema_long = ta.ema(df_trend["close"], length=200)
        else:
            ema_short = ta.ema(df_trend["close"], length=20)
            ema_long = ta.ema(df_trend["close"], length=50)
        if ema_short is not None and ema_long is not None:
            e_short = float(ema_short.iloc[-1])
            e_long = float(ema_long.iloc[-1])
            if _pd.isna(e_short) or _pd.isna(e_long) or e_long <= 0:
                trend_1d = "sideways"
            elif e_short > e_long * 1.01:
                trend_1d = "uptrend"
            elif e_short < e_long * 0.99:
                trend_1d = "downtrend"
            else:
                trend_1d = "sideways"
        else:
            trend_1d = "sideways"

        # Bullish vs Bearish score (net_score -100 to +100)
        bullish = 0
        bearish = 0
        # RSI 40-50 / 50-60: chỉ cộng khi trend aligned (pullback trong uptrend / bounce trong downtrend)
        if 40 <= rsi_1h <= 50 and trend_1d == "uptrend":
            bullish += 5
        elif 50 < rsi_1h <= 60 and trend_1d == "downtrend":
            bearish += 5
        if rsi_1h < 40:
            bullish += 20  # Oversold = long
        if rsi_1h > 70:
            bearish += 20  # Overbought = short
        momentum_bullish = False
        momentum_bearish = False
        if style == "scalp":
            # 5m pullback timing: CHỈ cộng khi aligned với trend 4h
            if rsi_4h < 40 and trend_1d == "uptrend":
                bullish += 10
            if rsi_4h > 70 and trend_1d == "downtrend":
                bearish += 10
            # Scalp: BỎ EMA cross + MACD (lagging), thêm RSI momentum + candle body
            # RSI momentum: 2 nến đã đóng liên tiếp tăng + delta > 2.0 (dùng iloc[-2,-3,-4])
            rsi_series = ta.rsi(df_fast["close"], length=14)
            if rsi_series is not None and len(rsi_series) >= 4:
                r0, r1, r2 = float(rsi_series.iloc[-2]), float(rsi_series.iloc[-3]), float(rsi_series.iloc[-4])
                if rsi_1h < 45 and r0 > r1 and r1 > r2 and (r0 - r2) > 2.0:
                    bullish += 15
                    momentum_bullish = True
                if rsi_1h > 55 and r0 < r1 and r1 < r2 and (r2 - r0) > 2.0:
                    bearish += 15
                    momentum_bearish = True
            # Candle body: dùng nến đã đóng (iloc[-2]) — nến chưa đóng không đáng tin
            if len(df_fast) >= 2:
                last_o, last_h, last_l, last_c = (
                    float(df_fast["open"].iloc[-2]), float(df_fast["high"].iloc[-2]),
                    float(df_fast["low"].iloc[-2]), float(df_fast["close"].iloc[-2]),
                )
                candle_range = last_h - last_l if last_h > last_l else 0.0001
                body_pct = abs(last_c - last_o) / candle_range * 100
                if body_pct > 50:  # Body > 50% range = strong candle
                    if last_c > last_o:
                        bullish += 10
                    else:
                        bearish += 10
        else:
            # Swing: giữ EMA cross + MACD (weight thấp hơn)
            if ema_cross_bullish:
                bullish += 15
            if ema_cross_bearish:
                bearish += 15
            if macd_bullish:
                bullish += 20
            if macd_bearish:
                bearish += 20
        if volume_spike and len(df_fast) >= 2:
            # Volume direction từ nến đã đóng (iloc[-2]) — nhất quán với candle body
            prev_close = float(df_fast["close"].iloc[-2])
            prev_open = float(df_fast["open"].iloc[-2])
            if prev_close > prev_open:
                bullish += 10
            else:
                bearish += 10
        if trend_1d == "uptrend":
            bullish += 10
        if trend_1d == "downtrend":
            bearish += 10

        net_score = max(-100, min(100, bullish - bearish))
        direction_bias = "LONG" if net_score > 10 else ("SHORT" if net_score < -10 else "NEUTRAL")
        score = max(0, min(100, net_score + 50))  # Legacy: map -100..100 to 0..100

        # ATR: swing=1h, scalp=5m (tight SL/TP)
        atr14 = ta.atr(df_atr["high"], df_atr["low"], df_atr["close"], length=14)
        atr50 = ta.atr(df_atr["high"], df_atr["low"], df_atr["close"], length=50)
        atr_value = float(atr14.iloc[-1]) if atr14 is not None and not atr14.empty else 0.0
        atr50_val = float(atr50.iloc[-1]) if atr50 is not None and not atr50.empty else atr_value
        atr_ratio = atr_value / atr50_val if atr50_val > 0 else 0.0
        current_price_1h = float(df_fast["close"].iloc[-1])
        atr_pct = atr_value / current_price_1h * 100 if current_price_1h > 0 else 0.0

        # ADX: swing=4h, scalp=1h. pandas-ta: ADX_14, DMP_14, DMN_14
        adx_df = ta.adx(df_adx["high"], df_adx["low"], df_adx["close"], length=14)
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

        # Regime: scalp dùng BB + ATR từ 1h (df_adx) — nhất quán với ADX. Swing giữ nguyên.
        bb_width_regime = bb_width
        atr_ratio_regime = atr_ratio
        if style == "scalp":
            bb_1h = ta.bbands(df_adx["close"], length=20)
            if bb_1h is not None and not bb_1h.empty:
                bbm = float(bb_1h.iloc[-1, 1])
                if bbm > 0:
                    bb_width_regime = float((bb_1h.iloc[-1, 2] - bb_1h.iloc[-1, 0]) / bbm)
            atr14_1h = ta.atr(df_adx["high"], df_adx["low"], df_adx["close"], length=14)
            atr50_1h = ta.atr(df_adx["high"], df_adx["low"], df_adx["close"], length=50)
            if atr14_1h is not None and not atr14_1h.empty and atr50_1h is not None and not atr50_1h.empty:
                a50 = float(atr50_1h.iloc[-1])
                if a50 > 0:
                    atr_ratio_regime = float(atr14_1h.iloc[-1]) / a50

        current_price = float(df_fast["close"].iloc[-1])
        chop_index = _calc_chop_index(df_fast, 14)
        return TechnicalSignal(
            rsi_1h=rsi_1h,
            rsi_4h=rsi_4h,
            ema_cross_bullish=ema_cross_bullish,
            macd_bullish=macd_bullish,
            volume_spike=volume_spike,
            volume_ratio=volume_ratio,
            volume_trend_up=volume_trend_up,
            bb_squeeze=bb_squeeze,
            support_level=recent_low,
            resistance_level=recent_high,
            trend_1d=trend_1d,
            score=score,
            net_score=net_score,
            direction_bias=direction_bias,
            momentum_bullish=momentum_bullish,
            momentum_bearish=momentum_bearish,
            atr_value=atr_value,
            atr_pct=atr_pct,
            atr_ratio=atr_ratio,
            adx=adx_val,
            plus_di=plus_di_val,
            minus_di=minus_di_val,
            bb_width=bb_width,
            bb_width_regime=bb_width_regime,
            atr_ratio_regime=atr_ratio_regime,
            current_price=current_price,
            swing_low=swing_low,
            swing_high=swing_high,
            ema9_just_crossed_up=ema9_just_crossed_up,
            ema9_just_crossed_down=ema9_just_crossed_down,
            ema9_crossed_recent_up=ema9_crossed_recent_up,
            ema9_crossed_recent_down=ema9_crossed_recent_down,
            vwap=vwap_val,
            vwap_distance_pct=vwap_distance_pct,
            chop_index=chop_index,
        )

    async def close(self):
        await self._client.aclose()


def get_opportunity_pairs(
    tickers: list[dict],
    futures_symbols: set[str] | None = None,
    funding_map: dict[str, float] | None = None,
    min_volatility_pct: float = 5.0,
    max_volatility_pct: float = 25.0,
    min_quote_volume_usd: float = 5_000_000,
    max_pairs_per_scan: int = 30,
    core_pairs: list[str] | None = None,
    blacklist: list[str] | None = None,
    allowed_pairs: list[str] | None = None,
    use_whitelist: bool = False,
    confluence_min_score: int = 1,
    funding_extreme_threshold: float = 0.001,
    symbols_in_cooldown: set[str] | None = None,
    scan_states: dict[str, dict] | None = None,
    hysteresis_entry_pct: float = 5.0,
    hysteresis_exit_pct: float = 3.0,
) -> list[str]:
    """
    Lọc cặp có tín hiệu bất thường (opportunity).
    Confluence: +1 volatility, +1 volume spike, +1 funding extreme.
    Hysteresis: vào khi |change| >= entry, ra khi |change| < exit.
    """
    core_pairs = core_pairs or []
    blacklist = blacklist or []
    blacklist_set = set(blacklist)
    symbols_in_cooldown = symbols_in_cooldown or set()
    scan_states = scan_states or {}

    def _safe_float(val, default: float = 0.0) -> float:
        if val is None:
            return default
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    # Median quoteVolume cho volume spike
    usdt_volumes = [
        _safe_float(t.get("quoteVolume"))
        for t in tickers
        if (t.get("symbol") or "").endswith("USDT") and _safe_float(t.get("quoteVolume")) > 0
    ]
    median_volume = float(sorted(usdt_volumes)[len(usdt_volumes) // 2]) if usdt_volumes else 0

    # 1. Filter cơ bản + confluence + hysteresis
    candidates = []
    for t in tickers:
        symbol = t.get("symbol") or ""
        if not symbol.endswith("USDT"):
            continue
        if not symbol.isascii():
            continue  # Skip non-ASCII symbols (e.g. 龙虾USDT)
        if symbol in blacklist_set:
            continue
        if symbol in symbols_in_cooldown:
            continue
        qv = _safe_float(t.get("quoteVolume"))
        if qv < min_quote_volume_usd:
            continue
        pct = _safe_float(t.get("priceChangePercent"))
        abs_pct = abs(pct)

        # Hysteresis: entry vs exit threshold
        state = scan_states.get(symbol, {})
        in_opp = bool(state.get("in_opportunity", 0))
        if in_opp:
            if abs_pct < hysteresis_exit_pct:
                continue  # Exit list
        else:
            if abs_pct < hysteresis_entry_pct:
                continue  # Chưa đủ để vào

        if not (min_volatility_pct <= abs_pct <= max_volatility_pct):
            continue

        # Confluence score
        score = 0
        if min_volatility_pct <= abs_pct <= max_volatility_pct:
            score += 1
        if median_volume > 0 and qv >= 2 * median_volume:
            score += 1
        if funding_map and abs(funding_map.get(symbol, 0)) >= funding_extreme_threshold:
            score += 1
        if score < confluence_min_score:
            continue

        last_price = _safe_float(t.get("lastPrice"))
        high_price = _safe_float(t.get("highPrice"))
        low_price = _safe_float(t.get("lowPrice"))
        candidates.append({
            "symbol": symbol,
            "priceChangePercent": pct,
            "quoteVolume": qv,
            "lastPrice": last_price,
            "highPrice": high_price,
            "lowPrice": low_price,
        })

    # 2. Futures filter
    if futures_symbols:
        candidates = [c for c in candidates if c["symbol"] in futures_symbols]

    # 3. Whitelist mode
    if use_whitelist and allowed_pairs:
        allowed_set = set(allowed_pairs)
        candidates = [c for c in candidates if c["symbol"] in allowed_set]

    # 4. Tách LONG / SHORT candidates, filter "chưa ở đỉnh/đáy"
    long_candidates = []
    short_candidates = []
    for c in candidates:
        pct = c["priceChangePercent"]
        last = c.get("lastPrice", 0)
        high = c.get("highPrice", 0)
        low = c.get("lowPrice", 0)

        if pct >= 3.0:
            # Đang tăng nhưng chưa ở đỉnh 24h (còn room)
            if high > 0 and last < high * 0.95:
                long_candidates.append(c)
        elif pct <= -3.0:
            # Đang giảm nhưng chưa ở đáy 24h (còn room short)
            if low > 0 and last > low * 1.05:
                short_candidates.append(c)

    # Sort riêng từng nhóm
    long_candidates.sort(key=lambda x: x["priceChangePercent"], reverse=True)
    short_candidates.sort(key=lambda x: x["priceChangePercent"])  # ascending (âm nhất lên đầu)

    # Lấy đều 2 nhóm, fill shortage từ phía kia nếu một bên thiếu
    half = max_pairs_per_scan // 2
    long_picked = long_candidates[:half]
    short_picked = short_candidates[:half]
    shortage = half - len(long_picked)
    if shortage > 0:
        short_picked = short_candidates[: half + shortage]
    shortage = half - len(short_picked)
    if shortage > 0:
        long_picked = long_candidates[: half + shortage]
    capped = [c["symbol"] for c in long_picked] + [c["symbol"] for c in short_picked]

    # 5. Add core_pairs ở đầu, dedupe, core không tính vào cap
    result = []
    seen = set()
    for p in core_pairs:
        if p and p not in seen:
            result.append(p)
            seen.add(p)
    for p in capped:
        if p not in seen:
            result.append(p)
            seen.add(p)

    return result


def classify_regime(
    adx: float,
    plus_di: float,
    minus_di: float,
    bb_width: float,
    atr_ratio: float,
) -> str:
    """
    Regime từ ADX + BB Width + ATR ratio.
    Returns: trending_up | trending_down | ranging | volatile | trending_volatile
    trending_volatile = best scalp: strong trend + high volatility → wider SL (1.2)
    """
    if atr_ratio > 1.5 and adx > 25:
        return "trending_volatile"  # Strong trend + momentum
    if atr_ratio > 1.5:
        return "volatile"  # Choppy, dangerous
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
    style: str = "swing",
    rr_ratio: float | None = None,
    swing_low: float = 0.0,
    swing_high: float = 0.0,
) -> tuple[float, float, float] | None:
    """
    Rule-based entry/SL/TP.
    swing: ATR mult 1.5 (trending) / 1.2 (ranging), R:R 1:2
    scalp: SL từ swing structure (tránh bị sweep), fallback ATR nếu structure quá xa
    Returns (entry, sl, tp) hoặc None nếu setup không hợp lệ (scalp structure quá xa).
    """
    if style == "scalp":
        rr = rr_ratio if rr_ratio and rr_ratio > 0 else 1.5
        if direction == "LONG":
            entry = current_price - 0.2 * atr_value
            # SL từ swing low thay vì ATR flat — tránh bị sweep
            if swing_low > 0:
                sl = swing_low - 0.1 * atr_value
                if sl >= entry:
                    logger.info("SL >= entry (swing_low quá cao) — structure invalid, reject")
                    return None
                if entry - sl > 2.0 * atr_value:
                    logger.info(
                        f"SL structure quá xa ({entry - sl:.2f} > 2×ATR {2 * atr_value:.2f}), reject"
                    )
                    return None  # Setup không có structure rõ, skip
            else:
                mult = 1.2 if regime == "trending_volatile" else (1.0 if regime in ("trending_up", "trending_down") else 0.8)
                sl = entry - mult * atr_value
        else:
            entry = current_price + 0.2 * atr_value
            if swing_high > 0:
                sl = swing_high + 0.1 * atr_value
                if sl <= entry:
                    logger.info("SL <= entry (swing_high quá thấp) — structure invalid, reject")
                    return None
                if sl - entry > 2.0 * atr_value:
                    logger.info(
                        f"SL structure quá xa ({sl - entry:.2f} > 2×ATR {2 * atr_value:.2f}), reject"
                    )
                    return None
            else:
                mult = 1.2 if regime == "trending_volatile" else (1.0 if regime in ("trending_up", "trending_down") else 0.8)
                sl = entry + mult * atr_value
        tp = entry + rr * (entry - sl) if direction == "LONG" else entry - rr * (sl - entry)
    else:
        mult = 1.5 if regime in ("trending_up", "trending_down") else 1.2
        rr = rr_ratio if rr_ratio and rr_ratio > 0 else 2.0
        entry = current_price
        if direction == "LONG":
            sl = entry - mult * atr_value
            tp = entry + rr * (entry - sl)
        else:
            sl = entry + mult * atr_value
            tp = entry - rr * (sl - entry)
    return entry, sl, tp


class WhaleDataFetcher:
    """
    Whale signals từ 3 nguồn free, không cần key:
    1. Binance aggTrades     - large trades trên Binance
    2. Binance orderbook     - large bid/ask walls
    3. Mempool.space         - BTC on-chain (BTC pairs only)
    """

    FUTURES_BASE = "https://fapi.binance.com/fapi/v1"

    def __init__(self):
        self._client = _create_client()
        self.base = self.FUTURES_BASE

    async def get_whale_transactions(
        self,
        symbol: str = "BTCUSDT",
        min_usd: int = WHALE_MIN_USD,
        hours_back: int = 4,
    ) -> WhaleSignal:
        coin = symbol.lower().replace("usdt", "")

        tasks = [
            self._fetch_binance_large_trades(symbol, min_usd, hours_back),
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
        self, symbol: str, min_usd: float, hours_back: int = 4
    ) -> list[dict]:
        """
        Public endpoint, không cần key.
        isBuyerMaker=True  → seller chủ động → sell pressure
        isBuyerMaker=False → buyer chủ động → buy pressure
        limit=1000 = 1000 trades mới nhất (scalp-relevant). Không dùng startTime —
        Binance với startTime trả về trades cũ nhất trong window, không phải mới nhất.
        """
        try:
            price_resp, trades_resp = await asyncio.gather(
                _http_get_with_retry(
                    self._client,
                    f"{self.base}/ticker/price",
                    params={"symbol": symbol},
                ),
                _http_get_with_retry(
                    self._client,
                    f"{self.base}/aggTrades",
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
                    f"{self.base}/depth",
                    params={"symbol": symbol, "limit": 100},
                ),
                _http_get_with_retry(
                    self._client,
                    f"{self.base}/ticker/price",
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
                    f"{self.base}/ticker/price",
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
