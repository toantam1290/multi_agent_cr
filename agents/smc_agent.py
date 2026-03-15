"""
agents/smc_agent.py - Standalone SMC Agent

Chạy hoàn toàn độc lập. Tích hợp crypto confluence:
  - Funding Rate, Open Interest, CVD (xem docs/027-crypto-confluence-smc.md)
  - OHLCV data đa timeframe

Flow:
  Fetch multi-TF + deriv + CVD song song → SMCStrategy → SMCSetup
  → Confluence adjust confidence → TradingSignal → Telegram alert
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from loguru import logger

from config import cfg, ALLOWED_PAIRS, get_effective_min_confidence
from database import Database
from models import TradingSignal, Direction, SignalStatus, TechnicalSignal, WhaleSignal, SentimentSignal
from utils.market_data import BinanceDataFetcher, FearGreedFetcher, get_opportunity_pairs
from utils.smc_strategy import SMCStrategy, SMCSetup
from utils.crypto_confluence import interpret_funding, interpret_oi, interpret_cvd


class SMCAgent:
    """
    Standalone SMC scanner.
    Tích hợp vào TradingOrchestrator như một job riêng biệt (every 5 min scalp / 15 min swing).
    """

    def __init__(self, db: Database, telegram=None):
        self.db = db
        self.telegram = telegram
        self.binance = BinanceDataFetcher()
        self.fear_greed = FearGreedFetcher()
        self.strategy = SMCStrategy(
            self.binance,
            sl_buffer_pct=0.003,   # Match backtest config (was 0.005 constructor default)
            min_rr_tp1=1.8,        # Match backtest config (was 1.5 constructor default)
        )

        self._pair_semaphore = asyncio.Semaphore(3)

    async def scan_pair(self, symbol: str) -> Optional[TradingSignal]:
        """
        Phân tích 1 pair bằng SMC + crypto confluence (Funding, OI, CVD).
        Trả về TradingSignal nếu có setup hợp lệ, None nếu không.
        """
        async with self._pair_semaphore:
            try:
                style = cfg.scan.trading_style or "scalp"

                # Fetch SMC + derivatives + CVD + F&G song song (futures cho khớp với funding/OI)
                setup_task = self.strategy.analyze(symbol, style=style)
                deriv_task = self.binance.get_derivatives_signal(symbol)
                cvd_task = self.binance.get_cvd_signal(symbol, limit=500, use_futures=True)
                stats_task = self.binance.get_24h_stats(symbol, use_futures=True)
                fg_task = self.fear_greed.get()

                setup, deriv, cvd_data, stats_24h, sentiment = await asyncio.gather(
                    setup_task, deriv_task, cvd_task, stats_task, fg_task,
                    return_exceptions=True,
                )

                if isinstance(setup, Exception) or setup is None or not setup.valid:
                    return None

                if isinstance(sentiment, Exception):
                    sentiment = SentimentSignal(fear_greed_index=50, fear_greed_label="Neutral")
                fg = sentiment.fear_greed_index if hasattr(sentiment, "fear_greed_index") else 50

                # Pair cooldown — tránh duplicate signal cùng pair trong 30 phút
                if self.db.had_recent_signal_for_pair(symbol, cooldown_sec=1800):
                    logger.info(f"SMCAgent {symbol}: Cooldown active (< 30m since last signal), skip")
                    return None

                # ob_entry_only — bpr(21%WR) và sweep(36%WR) là negative edge trên backtest
                if setup.entry_model != "ob_entry":
                    logger.info(f"SMCAgent {symbol}: skip {setup.entry_model} (ob_entry_only)")
                    return None

                if isinstance(deriv, Exception):
                    deriv = None
                if isinstance(cvd_data, Exception):
                    cvd_data = None
                if isinstance(stats_24h, Exception):
                    stats_24h = None

                base_confidence = setup.confidence
                confidence = base_confidence
                confluence_notes = []
                adjustments = []
                weights = []

                # Lớp 1: Funding Rate (chỉ khi fetch OK — tránh magic number)
                if deriv and deriv.fetch_ok:
                    fr_adj, fr_note = interpret_funding(deriv.funding_rate, setup.direction)
                    adjustments.append(fr_adj)
                    weights.append(0.4)
                    confluence_notes.append(fr_note)

                # Lớp 2: Open Interest (cần deriv.fetch_ok — OI từ cùng API)
                if deriv and deriv.fetch_ok and stats_24h and deriv.oi_change_pct != 0:
                    oi_adj, oi_note = interpret_oi(
                        deriv.oi_change_pct,
                        stats_24h.get("price_change_pct", 0),
                        setup.direction,
                    )
                    adjustments.append(oi_adj)
                    weights.append(0.3)
                    confluence_notes.append(oi_note)

                # Lớp 3: CVD
                if cvd_data:
                    ltf = setup.ltf_signal
                    in_ob = ltf.price_in_ob if ltf else False
                    in_fvg = ltf.price_in_fvg if ltf else False
                    cvd_adj, cvd_note = interpret_cvd(cvd_data, setup.direction, in_ob, in_fvg)
                    adjustments.append(cvd_adj)
                    weights.append(0.3)
                    confluence_notes.append(cvd_note)

                # Weighted average of point adjustments — additive, not multiplicative
                # Disabled by default để khớp backtest (backtest không có lớp này)
                # Bật: EXTRA_SCALP_FILTERS=true
                if adjustments and cfg.scan.use_extra_scalp_filters:
                    total_w = sum(weights)
                    raw_adj = sum(a * w for a, w in zip(adjustments, weights)) / total_w
                    capped_adj = max(-15, min(15, raw_adj))
                    confidence = int(confidence + capped_adj)

                confidence = max(0, min(100, confidence))
                min_conf = get_effective_min_confidence()
                if confidence < min_conf:
                    logger.info(
                        f"SMCAgent {symbol}: rejected after confluence adj "
                        f"(base={base_confidence} → {confidence} < {min_conf}) | {' | '.join(confluence_notes)}"
                    )
                    return None

                setup.confidence = confidence
                if confluence_notes:
                    setup.reasoning += " | Confluence: " + " | ".join(confluence_notes)

                # Funding hard block — chỉ block khi funding CỰC ĐOAN (>0.10%)
                # Funding elevated (0.05%-0.10%) đã được xử lý bởi soft penalty trong crypto_confluence
                if deriv and deriv.fetch_ok:
                    fr = deriv.funding_rate
                    if setup.direction == "LONG" and fr > 0.001:  # >0.10% = cực đoan
                        logger.info(f"SMCAgent {symbol}: funding={fr:.4%} > 0.10% → skip LONG (extreme)")
                        return None
                    if setup.direction == "SHORT" and fr < -0.001:  # <-0.10% = cực đoan
                        logger.info(f"SMCAgent {symbol}: funding={fr:.4%} < -0.10% → skip SHORT (extreme)")
                        return None

                # F&G extreme — block LONG khi Greed (overbought), block SHORT khi Fear (oversold)
                # Disabled by default (EXTRA_SCALP_FILTERS=false) để khớp với backtest
                if style == "scalp" and cfg.scan.use_extra_scalp_filters:
                    if setup.direction == "LONG" and fg > 75:
                        logger.info(f"SMCAgent {symbol}: F&G={fg} (Extreme Greed) → skip LONG scalp")
                        return None
                    if setup.direction == "SHORT" and fg < 25:
                        logger.info(f"SMCAgent {symbol}: F&G={fg} (Extreme Fear) → skip SHORT scalp")
                        return None

                available = await self._get_available_balance()
                position_size = min(
                    available * cfg.trading.max_position_pct,
                    available * 0.4,
                )
                if position_size < 10:
                    logger.info(f"SMCAgent {symbol}: position too small (${position_size:.2f})")
                    return None

                signal = self._build_signal(setup, position_size)
                if signal is None:
                    return None

                self.db.save_signal(signal)
                self.db.log(
                    "smc_agent", "INFO",
                    f"SMC signal: {symbol} {setup.direction} model={setup.entry_model} quality={setup.entry_model_quality}",
                    {
                        "signal_id": signal.id,
                        "entry_model": setup.entry_model,
                        "quality": setup.entry_model_quality,
                        "confidence": setup.confidence,
                        "htf_bias": setup.htf_bias,
                        "ltf_trigger": setup.ltf_trigger,
                    },
                )
                logger.success(
                    f"SMC Signal: {symbol} {setup.direction} "
                    f"entry={setup.entry:.2f} sl={setup.sl:.2f} "
                    f"tp1={setup.tp1:.2f} tp2={setup.tp2:.2f} "
                    f"model={setup.entry_model} quality={setup.entry_model_quality} "
                    f"conf={base_confidence}→{confidence} RR={setup.risk_reward_tp1:.1f}/{setup.risk_reward_tp2:.1f}"
                )
                return signal

            except Exception as e:
                logger.warning(f"SMCAgent {symbol}: {type(e).__name__}: {e}")
                return None

    async def _get_pairs_to_scan(self) -> list[str]:
        """Lấy danh sách pairs: fixed → ALLOWED_PAIRS, opportunity → dynamic screening."""
        sc = cfg.scan
        if sc.scan_mode != "opportunity":
            return list(ALLOWED_PAIRS)

        from datetime import datetime, timezone

        # Scalp active hours filter
        if sc.trading_style == "scalp" and sc.scalp_active_hours_utc:
            parts = sc.scalp_active_hours_utc.split("-")
            if len(parts) == 2:
                h = datetime.now(timezone.utc).hour
                if not (int(parts[0]) <= h < int(parts[1])):
                    logger.info(f"SMC: outside active hours ({sc.scalp_active_hours_utc} UTC), skipping")
                    return []

        tickers, premium_data = await asyncio.gather(
            self.binance.get_all_tickers_24hr(),
            self.binance.get_premium_index_full(),
        )
        if not tickers:
            logger.warning("SMC: tickers failed, fallback to ALLOWED_PAIRS")
            return list(ALLOWED_PAIRS)

        futures_symbols = set(p["symbol"] for p in premium_data) if premium_data else set()
        funding_map = (
            {p["symbol"]: float(p.get("lastFundingRate") or 0) for p in premium_data}
            if premium_data else {}
        )

        # Confluence min từ BTC 24h
        confluence_min = 2 if sc.market_regime == "sideways" else 1
        if sc.market_regime_mode == "auto":
            btc_ticker = next((t for t in tickers if t.get("symbol") == "BTCUSDT"), None)
            if btc_ticker:
                btc_pct = abs(float(btc_ticker.get("priceChangePercent") or 0))
                confluence_min = 2 if btc_pct < 2.0 else 1

        # Cooldown
        scan_states = self.db.get_all_scan_states()
        now_ts = datetime.now(timezone.utc)
        cutoff = now_ts.timestamp() - sc.cooldown_cycles * sc.cycle_interval_sec

        def _parse_ts(ts_str: str) -> float:
            s = (ts_str or "").replace("Z", "+00:00")
            if not s:
                return 0.0
            try:
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except Exception:
                return 0.0

        symbols_in_cooldown = {
            s for s, st in scan_states.items()
            if st.get("last_scanned_at") and _parse_ts(st["last_scanned_at"]) > cutoff
        }

        pairs = get_opportunity_pairs(
            tickers,
            futures_symbols=futures_symbols or None,
            funding_map=funding_map or None,
            min_volatility_pct=sc.opportunity_volatility_pct,
            max_volatility_pct=sc.opportunity_volatility_max_pct,
            min_quote_volume_usd=sc.min_quote_volume_usd,
            max_pairs_per_scan=sc.max_pairs_per_scan,
            core_pairs=sc.core_pairs,
            blacklist=sc.scan_blacklist,
            allowed_pairs=ALLOWED_PAIRS if sc.opportunity_use_whitelist else None,
            use_whitelist=sc.opportunity_use_whitelist,
            confluence_min_score=confluence_min,
            funding_extreme_threshold=sc.funding_extreme_threshold,
            symbols_in_cooldown=symbols_in_cooldown,
            scan_states=scan_states,
            hysteresis_entry_pct=sc.hysteresis_entry_pct,
            hysteresis_exit_pct=sc.hysteresis_exit_pct,
        )
        logger.info(f"SMC opportunity scan: {len(pairs)} pairs")
        return pairs

    async def run_full_scan(self) -> list[TradingSignal]:
        """Scan pairs — fixed: ALLOWED_PAIRS, opportunity: dynamic screening."""
        pairs_to_scan = await self._get_pairs_to_scan()
        if not pairs_to_scan:
            logger.info("SMC: no pairs to scan")
            return []

        # Session filter (scalp)
        sc = cfg.scan
        if sc.trading_style == "scalp" and sc.scalp_session_filter:
            from datetime import datetime, timezone
            hour_utc = datetime.now(timezone.utc).hour
            session = "london" if 8 <= hour_utc < 13 else ("ny_overlap" if 13 <= hour_utc < 20 else ("asia" if hour_utc < 8 else "dead_zone"))
            if session == "dead_zone":
                logger.info("SMC: dead_zone session, skipping scan")
                return []
            if session == "asia":
                pairs_to_scan = [p for p in pairs_to_scan if p in sc.core_pairs]
                if not pairs_to_scan:
                    logger.info("SMC: asia session, no core pairs in list")
                    return []

        logger.info(f"SMC scan starting — {len(pairs_to_scan)} pairs ({sc.scan_mode})")

        tasks = [self.scan_pair(pair) for pair in pairs_to_scan]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals = []
        for pair, r in zip(pairs_to_scan, results):
            if isinstance(r, Exception):
                logger.debug(f"SMC scan {pair} exception: {r}")
            elif isinstance(r, TradingSignal):
                signals.append(r)

        logger.info(f"SMC scan done — {len(signals)} setup(s) found")
        return signals

    def _build_signal(self, setup: SMCSetup, position_size: float) -> Optional[TradingSignal]:
        """Chuyển SMCSetup thành TradingSignal."""
        try:
            if setup.direction == "LONG":
                if not (setup.sl < setup.entry < setup.tp1 < setup.tp2):
                    logger.warning(
                        f"SMCAgent {setup.symbol}: invalid LONG levels "
                        f"sl={setup.sl} entry={setup.entry} tp1={setup.tp1} tp2={setup.tp2}"
                    )
                    return None
            else:
                if not (setup.tp2 < setup.tp1 < setup.entry < setup.sl):
                    logger.warning(
                        f"SMCAgent {setup.symbol}: invalid SHORT levels "
                        f"sl={setup.sl} entry={setup.entry} tp1={setup.tp1} tp2={setup.tp2}"
                    )
                    return None

            smc_dict = {
                "source": "smc_standalone",
                "entry_model": setup.entry_model,
                "entry_model_quality": setup.entry_model_quality,
                "htf_bias": setup.htf_bias,
                "mtf_bias": setup.mtf_bias,
                "ltf_trigger": setup.ltf_trigger,
                "draw_on_liquidity": setup.draw_on_liquidity,
                "tp2": setup.tp2,
                "risk_reward_tp2": setup.risk_reward_tp2,
                "ob_zone_low": getattr(setup, 'ob_zone_low', None),
                "ob_zone_high": getattr(setup, 'ob_zone_high', None),
                "reasoning": setup.reasoning,
                "summary": (
                    setup.ltf_signal.summary[:200]
                    if setup.ltf_signal else ""
                ),
            }

            reasoning = (
                f"[SMC Standalone] {setup.entry_model.upper()} ({setup.entry_model_quality}) | "
                f"HTF={setup.htf_bias} LTF trigger={setup.ltf_trigger} | "
                f"DOL={setup.draw_on_liquidity:.2f} | "
                f"{setup.reasoning}"
            )

            return TradingSignal(
                id=str(uuid.uuid4()),
                pair=setup.symbol,
                direction=Direction(setup.direction),
                entry_price=setup.entry,
                stop_loss=setup.sl,
                take_profit=setup.tp1,
                position_size_usdt=position_size,
                technical=self._dummy_technical(),
                whale=self._dummy_whale(),
                sentiment=self._dummy_sentiment(),
                confidence=setup.confidence,
                reasoning=reasoning,
                risk_reward=setup.risk_reward_tp1,
                status=SignalStatus.PENDING,
                regime="smc_driven",
                model_version="smc_strategy_v2",
                smc=smc_dict,
            )
        except Exception as e:
            logger.warning(f"SMCAgent _build_signal {setup.symbol}: {e}")
            return None

    async def _get_available_balance(self) -> float:
        """Lấy available balance (trừ locked capital + cumulative losses)."""
        cumulative_pnl = self.db.get_cumulative_pnl()
        if cfg.trading.paper_trading:
            total = cfg.trading.paper_balance_usdt + cumulative_pnl
        else:
            total = 10000.0 + cumulative_pnl  # TODO: Fetch base from Binance
        open_trades = self.db.get_open_trades()
        locked = sum(t["position_size_usdt"] for t in open_trades)
        return max(0.0, total - locked)

    def _dummy_technical(self) -> TechnicalSignal:
        """TechnicalSignal placeholder — SMC không dùng technical indicators."""
        return TechnicalSignal(
            rsi_1h=50.0,
            rsi_4h=50.0,
            ema_cross_bullish=False,
            macd_bullish=False,
            volume_spike=False,
            bb_squeeze=False,
            trend_1d="unknown",
        )

    def _dummy_whale(self) -> WhaleSignal:
        return WhaleSignal(
            large_transfers_count=0,
            large_transfers_usd=0.0,
            exchange_inflow_usd=0.0,
            exchange_outflow_usd=0.0,
            net_flow=0.0,
            score=0,
        )

    def _dummy_sentiment(self) -> SentimentSignal:
        return SentimentSignal(fear_greed_index=50, fear_greed_label="Neutral")

    async def close(self):
        await self.binance.close()
        await self.fear_greed.close()
