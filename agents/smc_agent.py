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
from utils.market_data import BinanceDataFetcher
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
        self.strategy = SMCStrategy(self.binance)

        self._pair_semaphore = asyncio.Semaphore(3)

    async def scan_pair(self, symbol: str) -> Optional[TradingSignal]:
        """
        Phân tích 1 pair bằng SMC + crypto confluence (Funding, OI, CVD).
        Trả về TradingSignal nếu có setup hợp lệ, None nếu không.
        """
        async with self._pair_semaphore:
            try:
                style = cfg.scan.trading_style or "scalp"

                # Fetch SMC + derivatives + CVD song song (futures cho khớp với funding/OI)
                setup_task = self.strategy.analyze(symbol, style=style)
                deriv_task = self.binance.get_derivatives_signal(symbol)
                cvd_task = self.binance.get_cvd_signal(symbol, limit=500, use_futures=True)
                stats_task = self.binance.get_24h_stats(symbol, use_futures=True)

                setup, deriv, cvd_data, stats_24h = await asyncio.gather(
                    setup_task, deriv_task, cvd_task, stats_task,
                    return_exceptions=True,
                )

                if isinstance(setup, Exception) or setup is None or not setup.valid:
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

                # Lớp 1: Funding Rate (chỉ khi fetch OK — tránh magic number)
                if deriv and deriv.fetch_ok:
                    fr_mult, fr_note = interpret_funding(deriv.funding_rate, setup.direction)
                    confidence = int(confidence * fr_mult)
                    confluence_notes.append(fr_note)

                # Lớp 2: Open Interest (cần deriv.fetch_ok — OI từ cùng API)
                if deriv and deriv.fetch_ok and stats_24h and deriv.oi_change_pct != 0:
                    oi_mult, oi_note = interpret_oi(
                        deriv.oi_change_pct,
                        stats_24h.get("price_change_pct", 0),
                        setup.direction,
                    )
                    confidence = int(confidence * oi_mult)
                    confluence_notes.append(oi_note)

                # Lớp 3: CVD
                if cvd_data:
                    ltf = setup.ltf_signal
                    in_ob = ltf.price_in_ob if ltf else False
                    in_fvg = ltf.price_in_fvg if ltf else False
                    cvd_mult, cvd_note = interpret_cvd(cvd_data, setup.direction, in_ob, in_fvg)
                    confidence = int(confidence * cvd_mult)
                    confluence_notes.append(cvd_note)

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

    async def run_full_scan(self) -> list[TradingSignal]:
        """Scan tất cả ALLOWED_PAIRS song song."""
        logger.info(f"SMC scan starting — {len(ALLOWED_PAIRS)} pairs")

        tasks = [self.scan_pair(pair) for pair in ALLOWED_PAIRS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals = []
        for pair, r in zip(ALLOWED_PAIRS, results):
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
        """Lấy available balance (trừ locked capital trong open positions). Giống ResearchAgent."""
        if cfg.trading.paper_trading:
            total = cfg.trading.paper_balance_usdt
        else:
            total = 10000.0  # TODO: Fetch from Binance
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
