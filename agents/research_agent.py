"""
agents/research_agent.py - Claude Sonnet pre-mortem risk assessment + rule-based flow
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Optional
import httpx
from anthropic import AsyncAnthropic
from loguru import logger

from config import cfg, ALLOWED_PAIRS, get_effective_min_confidence
from models import (
    TradingSignal, Direction,
    TechnicalSignal, WhaleSignal, SentimentSignal, DerivativesSignal,
    SignalStatus
)
from utils.market_data import (
    BinanceDataFetcher, WhaleDataFetcher, FearGreedFetcher,
    get_opportunity_pairs,
    classify_regime, calc_entry_sl_tp,
)
from database import Database

# Estimated cost per Claude call (Sonnet) for budget tracking
CLAUDE_ESTIMATED_COST_PER_CALL = 0.005

RESEARCH_SYSTEM_PROMPT_BASE = """
Bạn là risk assessor cho trading setup. KHÔNG predict giá. Nhiệm vụ: stress-test thesis.

Bạn nhận setup đã tính sẵn (entry, SL, TP). Trả lời:
1. Top 3 risks có thể invalidate trade này?
2. Cách nào trade này mất tiền nhiều nhất?
3. Có news/event trong 24h tới làm setup unreliable không?

Cuối cùng: PROCEED (ok để trade) / WAIT (chờ điều kiện tốt hơn) / AVOID (không trade).

OUTPUT FORMAT (JSON):
{
  "verdict": "PROCEED" | "WAIT" | "AVOID",
  "top_3_risks": ["risk1", "risk2", "risk3"],
  "most_likely_failure": "mô tả ngắn",
  "reasoning": "Giải thích ngắn (max 150 chars)",
  "confidence": 0-100
}

Nếu verdict = PROCEED: confidence >= 75 (swing) hoặc >= 80 (scalp). Nếu WAIT/AVOID: confidence < 75.
KHÔNG thêm text ngoài JSON.
"""


def _get_system_prompt(style: str) -> str:
    """Scalp: thêm hướng dẫn horizon ngắn, confidence 80."""
    base = RESEARCH_SYSTEM_PROMPT_BASE
    if style == "scalp":
        return base + """

QUAN TRỌNG khi setup là SCALP:
- Chỉ xét rủi ro trong 15–60 phút tới, KHÔNG over-penalize vì "market uncertain" dài hạn.
- Nếu PROCEED: confidence PHẢI >= 80 (scalp cần threshold cao hơn).
- Fear & Greed index là daily data — ít relevant cho scalp, đừng weight quá nặng.
"""
    return base


class ResearchAgent:
    """
    Research Agent - chạy mỗi 15 phút
    Thu thập data → rule-based filter → regime → calc entry/SL/TP → Claude Sonnet pre-mortem → TradingSignal
    """

    def __init__(self, db: Database):
        self.db = db
        self.client = AsyncAnthropic(api_key=cfg.anthropic_api_key)
        self.binance = BinanceDataFetcher()
        self.whale = WhaleDataFetcher()
        self.fear_greed = FearGreedFetcher()
        self._claude_semaphore = asyncio.Semaphore(2)  # Tối đa 2 Claude calls song song (tránh budget race)
        self._pair_semaphore = asyncio.Semaphore(5)  # Tối đa 5 pairs phân tích đồng thời (tránh Binance throttle)
        logger.info("ResearchAgent initialized")

    def _rule_based_filter(
        self,
        technical: TechnicalSignal,
        derivatives: DerivativesSignal,
        style: str = "swing",
        pair: str = "",
    ) -> Optional[str]:
        """
        Rule-based Gate. Returns LONG | SHORT | None.
        Claude chỉ được gọi khi filter trả về non-None.
        Scalp: RSI nới hơn (50/50). Swing: chặt (45/55).
        Core pairs: exempt volume filter (BTC/ETH thường volume_ratio ~1.0).
        RELAX_FILTER=true: nới net_score (5/-5), bỏ volume + momentum — để test pipeline.
        """
        funding_pct = derivatives.funding_rate * 100  # 0.0001 → 0.01%
        rsi_long_max = cfg.scan.scalp_rsi_long_max if style == "scalp" else 45
        rsi_short_min = cfg.scan.scalp_rsi_short_min if style == "scalp" else 55
        funding_long_max = cfg.scan.funding_long_max_pct
        funding_short_min = cfg.scan.funding_short_min_pct  # 0.005% default
        is_core = pair in (cfg.scan.core_pairs or [])
        relax = getattr(cfg.scan, "relax_filter", False)

        # Scalp: volume confirmation — core pairs exempt. RELAX: bỏ qua
        if style == "scalp" and not is_core and not relax:
            vol_ok = technical.volume_spike or technical.volume_ratio >= 1.2 or technical.volume_trend_up
            if not vol_ok:
                return None

        # net_score threshold: RELAX dùng 5/-5 thay vì 20/-20 (scalp) hoặc 10/-10 (swing)
        if relax:
            net_long_min, net_short_max = 5, -5
        else:
            net_long_min = 20 if style == "scalp" else 10
            net_short_max = -20 if style == "scalp" else -10

        # LONG: trend != downtrend, RSI < threshold, funding < max, net_score > threshold
        if (
            technical.trend_1d != "downtrend"
            and technical.rsi_1h < rsi_long_max
            and funding_pct < funding_long_max
            and technical.net_score > net_long_min
        ):
            if style == "scalp" and not relax and not technical.momentum_bullish:
                return None
            return "LONG"
        # SHORT: trend != uptrend, RSI > threshold, funding > min, net_score < threshold
        if (
            technical.trend_1d != "uptrend"
            and technical.rsi_1h > rsi_short_min
            and funding_pct > funding_short_min
            and technical.net_score < net_short_max
        ):
            if style == "scalp" and not relax and not technical.momentum_bearish:
                return None
            return "SHORT"
        return None

    async def analyze_pair(
        self,
        pair: str,
        prefetched_sentiment: Optional[SentimentSignal] = None,
        session: Optional[str] = None,
        min_confluence: int = 3,
    ) -> tuple[Optional[TradingSignal], dict]:
        """Wrapper: gate với _pair_semaphore, tối đa 5 pairs phân tích đồng thời."""
        async with self._pair_semaphore:
            return await self._analyze_pair_inner(pair, prefetched_sentiment, session, min_confluence)

    async def _analyze_pair_inner(
        self,
        pair: str,
        prefetched_sentiment: Optional[SentimentSignal] = None,
        session: Optional[str] = None,
        min_confluence: int = 3,
    ) -> tuple[Optional[TradingSignal], dict]:
        """
        Phân tích một cặp tiền → trả về (TradingSignal | None, metadata).
        metadata: {rule_passed, claude_proceed} cho observability.
        """
        logger.info(f"Analyzing {pair}...")
        meta = {"rule_passed": False, "claude_proceed": False, "ema9_rejected": False, "confluence_rejected": False}

        try:
            # 1. Thu thập data song song (Fear & Greed prefetch khi có)
            style = cfg.scan.trading_style
            whale_hours = cfg.scan.scalp_whale_hours if style == "scalp" else 4
            relax = getattr(cfg.scan, "relax_filter", False)  # Define sớm — dùng cho CVD, VWAP, EMA9, confluence

            # Swing: dùng technical.current_price (từ klines close), tiết kiệm 1 req/pair
            # Scalp: cần real-time tick vì entry pullback thay đổi nhanh trong 5 phút
            if prefetched_sentiment is not None:
                if style == "scalp":
                    current_price, technical, whale_data, derivatives, ob_data, cvd = await asyncio.gather(
                        self.binance.get_current_price(pair),
                        self.binance.compute_technical_signal(pair, style=style),
                        self.whale.get_whale_transactions(pair, hours_back=whale_hours),
                        self.binance.get_derivatives_signal(pair),
                        self.binance.get_orderbook_data(pair),
                        self.binance.get_cvd_signal(pair, limit=500),
                    )
                    spread_pct = ob_data["spread_pct"]
                    ob_imbalance = ob_data["imbalance"]
                else:
                    technical, whale_data, derivatives = await asyncio.gather(
                        self.binance.compute_technical_signal(pair, style=style),
                        self.whale.get_whale_transactions(pair, hours_back=whale_hours),
                        self.binance.get_derivatives_signal(pair),
                    )
                    current_price = technical.current_price
                    spread_pct = 0.0
                    ob_imbalance = 1.0
                    cvd = {"cvd_ratio": 0.5, "cvd_trend": "neutral"}
                sentiment = prefetched_sentiment
            else:
                if style == "scalp":
                    current_price, technical, whale_data, sentiment, derivatives, ob_data, cvd = await asyncio.gather(
                        self.binance.get_current_price(pair),
                        self.binance.compute_technical_signal(pair, style=style),
                        self.whale.get_whale_transactions(pair, hours_back=whale_hours),
                        self.fear_greed.get(),
                        self.binance.get_derivatives_signal(pair),
                        self.binance.get_orderbook_data(pair),
                        self.binance.get_cvd_signal(pair, limit=500),
                    )
                    spread_pct = ob_data["spread_pct"]
                    ob_imbalance = ob_data["imbalance"]
                else:
                    technical, whale_data, sentiment, derivatives = await asyncio.gather(
                        self.binance.compute_technical_signal(pair, style=style),
                        self.whale.get_whale_transactions(pair, hours_back=whale_hours),
                        self.fear_greed.get(),
                        self.binance.get_derivatives_signal(pair),
                    )
                    current_price = technical.current_price
                    spread_pct = 0.0
                    ob_imbalance = 1.0
                    cvd = {"cvd_ratio": 0.5, "cvd_trend": "neutral"}
            logger.info(
                f"{pair} | Price: ${current_price:,.2f} | "
                f"Tech: {technical.net_score} | Whale: {whale_data.score} | "
                f"Funding: {derivatives.funding_rate*100:.3f}% | F&G: {sentiment.fear_greed_index}"
            )

            # 1b. Scalp: spread check sớm — bỏ qua illiquid pair trước các bước tốn CPU
            if style == "scalp" and not relax and spread_pct > 0.05:
                logger.info(f"{pair}: Spread {spread_pct:.3f}% > 0.05%, skip")
                self.db.log("research_agent", "INFO", f"Skip {pair}: spread", {"pair": pair, "spread": spread_pct})
                return None, meta

            # 2. Rule-based filter — không pass thì skip Claude
            direction = self._rule_based_filter(technical, derivatives, style=style, pair=pair)
            if direction is None:
                logger.info(f"{pair}: Rule-based filter → No trade")
                self.db.log("research_agent", "INFO", f"Filter rejected {pair}", {"pair": pair})
                return None, meta

            meta["rule_passed"] = True

            # 2a. Scalp: CVD divergence — giá bullish nhưng seller đang dominate = fake move
            if style == "scalp" and not relax:
                if direction == "LONG" and cvd["cvd_ratio"] < 0.45:
                    logger.info(f"{pair}: CVD divergence — price bullish but sellers dominating ({cvd['cvd_ratio']:.2f}), skip")
                    self.db.log("research_agent", "INFO", f"Skip {pair}: CVD divergence", {"pair": pair, "cvd_ratio": cvd["cvd_ratio"]})
                    return None, meta
                if direction == "SHORT" and cvd["cvd_ratio"] > 0.55:
                    logger.info(f"{pair}: CVD divergence — price bearish but buyers dominating ({cvd['cvd_ratio']:.2f}), skip")
                    self.db.log("research_agent", "INFO", f"Skip {pair}: CVD divergence", {"pair": pair, "cvd_ratio": cvd["cvd_ratio"]})
                    return None, meta

            # 2b. Scalp: VWAP bias — LONG khi giá gần/dưới VWAP, SHORT khi gần/trên
            if style == "scalp" and not relax:
                vd = technical.vwap_distance_pct
                if direction == "LONG" and vd > 1.5:
                    logger.info(f"{pair}: Price {vd:.1f}% above VWAP — overextended, skip LONG")
                    return None, meta
                if direction == "SHORT" and vd < -1.5:
                    logger.info(f"{pair}: Price {vd:.1f}% below VWAP — overextended, skip SHORT")
                    return None, meta

            # 2c. Scalp: entry timing — cross EMA9 trong 3 nến gần nhất (nới hơn "just crossed")
            if style == "scalp" and not relax:
                timing_ok = (
                    (direction == "LONG" and technical.ema9_crossed_recent_up)
                    or (direction == "SHORT" and technical.ema9_crossed_recent_down)
                )
                if not timing_ok:
                    meta["ema9_rejected"] = True
                    logger.info(f"{pair}: Entry timing — chưa cross EMA9 trong 3 nến, skip")
                    self.db.log("research_agent", "INFO", f"Skip {pair}: timing", {"pair": pair})
                    return None, meta

            # 3. Regime (ADX + BB + ATR) — dùng bb_width_regime, atr_ratio_regime (scalp = 1h nhất quán)
            regime = classify_regime(
                technical.adx, technical.plus_di, technical.minus_di,
                technical.bb_width_regime, technical.atr_ratio_regime,
            )

            # Scalp: skip khi regime = volatile (non-trending choppy) — SL tight = nguy hiểm
            if style == "scalp" and regime == "volatile":
                logger.info(f"{pair}: Regime volatile (non-trending) → skip scalp")
                self.db.log("research_agent", "INFO", f"Skip scalp for {pair}: volatile regime", {"pair": pair})
                return None, meta

            # Chop Index: > 61.8 = choppy, skip (RELAX: bỏ qua)
            if style == "scalp" and not relax and technical.chop_index > 61.8:
                logger.info(f"{pair}: Chop index {technical.chop_index:.1f} > 61.8, skip")
                self.db.log("research_agent", "INFO", f"Skip {pair}: choppy", {"pair": pair, "chop_index": technical.chop_index})
                return None, meta

            if not (technical.atr_value > 0):
                logger.warning(f"{pair}: ATR invalid ({technical.atr_value}), skipping")
                return None, meta

            # 3b. Confluence check — ít nhất 3/6 yếu tố align trước khi gọi Claude (RELAX: bỏ qua)
            confluence_score = 0
            if direction == "LONG" and technical.trend_1d == "uptrend":
                confluence_score += 1
            if direction == "SHORT" and technical.trend_1d == "downtrend":
                confluence_score += 1
            if technical.volume_spike or technical.volume_trend_up:
                confluence_score += 1
            if direction == "LONG" and derivatives.funding_rate < 0.0002:
                confluence_score += 1
            if direction == "SHORT" and derivatives.funding_rate > 0.0002:
                confluence_score += 1
            if direction == "LONG" and whale_data.net_flow > 0:
                confluence_score += 1
            if direction == "SHORT" and whale_data.net_flow < 0:
                confluence_score += 1
            # OI + trend: chỉ cộng khi OI tăng VÀ trend aligned (fresh longs trong uptrend / shorts trong downtrend)
            if derivatives.oi_change_pct > 5 and (
                (direction == "LONG" and technical.trend_1d == "uptrend")
                or (direction == "SHORT" and technical.trend_1d == "downtrend")
            ):
                confluence_score += 1
            # CVD + orderbook imbalance (order flow)
            if direction == "LONG" and cvd["cvd_ratio"] > 0.55:
                confluence_score += 1
            if direction == "SHORT" and cvd["cvd_ratio"] < 0.45:
                confluence_score += 1
            if direction == "LONG" and cvd["cvd_trend"] == "accelerating_buy":
                confluence_score += 1
            if direction == "SHORT" and cvd["cvd_trend"] == "accelerating_sell":
                confluence_score += 1
            if direction == "LONG" and ob_imbalance > 1.5:
                confluence_score += 1
            if direction == "SHORT" and ob_imbalance < 0.7:
                confluence_score += 1
            # VWAP mean reversion
            if direction == "LONG" and -0.5 <= technical.vwap_distance_pct <= 0:
                confluence_score += 1
            if direction == "SHORT" and 0 <= technical.vwap_distance_pct <= 0.5:
                confluence_score += 1
            if not relax and confluence_score < min_confluence:
                meta["confluence_rejected"] = True
                logger.info(f"{pair}: Confluence {confluence_score}/9 < {min_confluence}, skip Claude")
                self.db.log("research_agent", "INFO", f"Confluence low {pair}", {"pair": pair, "score": confluence_score})
                return None, meta

            # 4. Position size check TRƯỚC Claude (tránh lãng phí budget khi available=0)
            available = await self._get_available_balance()
            position_size = min(
                available * cfg.trading.max_position_pct,
                available * 0.4,  # Hard cap 40% available cho 1 trade
            )
            if position_size < 10:
                logger.info(f"{pair}: Position size too small (${position_size:.2f}), skipping (pre-Claude)")
                return None, meta

            # 5. Calc entry/SL/TP (rule-based, ATR hoặc swing structure)
            rr_ratio = cfg.trading.scalp_risk_reward_ratio if style == "scalp" else None
            result = calc_entry_sl_tp(
                direction, current_price, technical.atr_value, regime,
                style=style, rr_ratio=rr_ratio,
                swing_low=technical.swing_low,
                swing_high=technical.swing_high,
            )
            if result is None:
                logger.info(f"{pair}: SL structure quá xa — setup không hợp lệ")
                self.db.log("research_agent", "INFO", f"Skip {pair}: structure", {"pair": pair})
                return None, meta
            entry, sl, tp = result

            # 6. Claude risk assessment (pre-mortem)
            analysis = await self._claude_analyze(
                pair, current_price, technical, whale_data, sentiment, derivatives,
                direction, entry, sl, tp, regime, style=style,
                confluence_score=confluence_score,
                spread_pct=spread_pct,
                cvd=cvd,
                ob_imbalance=ob_imbalance,
                session=session if style == "scalp" else None,
            )

            if not analysis or not analysis.get("should_trade"):
                verdict = analysis.get("verdict", "N/A") if analysis else "budget/error"
                confidence = analysis.get("confidence", 0) if analysis else 0
                meta["claude_proceed"] = bool(analysis and analysis.get("should_trade"))
                logger.info(f"{pair}: Claude → {verdict} (confidence: {confidence})")
                self.db.log("research_agent", "INFO",
                            f"No opportunity for {pair}",
                            {"confidence": confidence})
                return None, meta

            meta["claude_proceed"] = True

            # 7. Build signal
            confidence = int(analysis["confidence"])
            min_conf = get_effective_min_confidence()
            if confidence < min_conf:
                logger.info(f"{pair}: Confidence {confidence} < {min_conf}, skipping")
                return None, meta

            # Tính R:R
            if direction == "LONG":
                risk = entry - sl
                reward = tp - entry
            else:
                risk = sl - entry
                reward = entry - tp

            rr = reward / risk if risk > 0 else 0

            signal = TradingSignal(
                id=str(uuid.uuid4()),
                pair=pair,
                direction=Direction(direction),
                entry_price=entry,
                stop_loss=sl,
                take_profit=tp,
                position_size_usdt=position_size,
                technical=technical,
                whale=whale_data,
                sentiment=sentiment,
                confidence=confidence,
                reasoning=analysis.get("reasoning", ""),
                risk_reward=round(rr, 2),
                status=SignalStatus.PENDING,
                regime=regime,
                model_version="claude-sonnet-4-6",
            )

            self.db.save_signal(signal)
            self.db.log("research_agent", "INFO",
                        f"Signal created: {pair} {signal.direction.value} confidence={confidence}",
                        {"signal_id": signal.id})

            logger.success(
                f"Signal created: {pair} {signal.direction.value} "
                f"@ ${entry:,.2f} | SL: ${sl:,.2f} | TP: ${tp:,.2f} | "
                f"Confidence: {confidence} | R:R: 1:{rr:.1f}"
            )
            return signal, meta

        except Exception as e:
            err_detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.exception(f"Error analyzing {pair}: {err_detail}")
            self.db.log("research_agent", "ERROR", f"Analysis failed for {pair}", {"error": err_detail})
            return None, meta

    async def _claude_analyze(
        self,
        pair: str,
        current_price: float,
        technical: TechnicalSignal,
        whale: WhaleSignal,
        sentiment: SentimentSignal,
        derivatives: DerivativesSignal,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        regime: str,
        style: str = "swing",
        confluence_score: int = 0,
        spread_pct: float = 0.0,
        cvd: dict | None = None,
        ob_imbalance: float = 1.0,
        session: str | None = None,
    ) -> Optional[dict]:
        """Claude pre-mortem risk assessment. Entry/SL/TP đã tính sẵn (rule-based)."""

        async with self._claude_semaphore:
            # Budget cap — skip Claude nếu đã vượt daily limit
            today_spend = self.db.get_today_spend()
            if today_spend >= cfg.anthropic_daily_budget_usd:
                logger.warning(
                    f"API budget exceeded for today (${today_spend:.2f} >= ${cfg.anthropic_daily_budget_usd}), skipping Claude call for {pair}"
                )
                self.db.log(
                    "research_agent",
                    "WARNING",
                    "API budget exceeded, signal rejected",
                    {"today_spend": today_spend, "cap": cfg.anthropic_daily_budget_usd},
                )
                return None

            risk_pct = abs(entry - sl) / entry * 100 if entry > 0 else 0
            reward_pct = abs(tp - entry) / entry * 100 if entry > 0 else 0
            entry_gap_pct = (entry - current_price) / current_price * 100 if current_price > 0 else 0
            sl_atr_mult = abs(entry - sl) / technical.atr_value if technical.atr_value > 0 else 0

            rsi_label = "RSI 15m / RSI 5m (timing)" if style == "scalp" else "RSI 1h / RSI 4h"
            rsi_vals = f"{technical.rsi_1h:.1f} / {technical.rsi_4h:.1f}"

            ema9_timing = "yes" if (technical.ema9_crossed_recent_up or technical.ema9_crossed_recent_down) else "no"

            user_prompt = f"""
Pre-mortem risk assessment cho {pair} ({style.upper()}):

PROPOSED SETUP ({direction}):
- Entry: ${entry:,.2f}
- Stop Loss: ${sl:,.2f} (-{risk_pct:.1f}%)
- Take Profit: ${tp:,.2f} (+{reward_pct:.1f}%)
- Regime: {regime}
- Entry gap: {entry_gap_pct:+.2f}% from current (pullback limit — needs retrace to fill)
- SL distance: {sl_atr_mult:.1f}×ATR (swing structure based)

DATA:
- Price: ${current_price:,.2f} | Trend 4h: {technical.trend_1d}
- {rsi_label}: {rsi_vals}
- Tech net_score: {technical.net_score} | Whale: {whale.score}/100
{f'- momentum_triggered: true (scalp RSI 2-candle — required gate passed)' if style == 'scalp' else ''}
- Funding: {derivatives.funding_rate*100:.3f}% | Basis: {derivatives.basis_pct:.2f}%
- F&G: {sentiment.fear_greed_index}/100 | Net flow: ${whale.net_flow/1e6:.1f}M
- Confluence: {confluence_score}/9 | EMA9 crossed recent (3 nến): {ema9_timing}
- Spread: {spread_pct:.3f}% | OI change 24h: {derivatives.oi_change_pct:+.1f}%
- CVD ratio: {(cvd or {}).get('cvd_ratio', 0.5):.2f} | CVD trend: {(cvd or {}).get('cvd_trend', 'neutral')}
- Orderbook imbalance: {ob_imbalance:.2f} | VWAP distance: {technical.vwap_distance_pct:+.1f}%
{f'- Session: {session}' if session else ''}

1. Top 3 risks invalidate trade?
2. Most likely failure mode?
3. News/event 24h?
→ Verdict: PROCEED / WAIT / AVOID
"""

            min_conf = get_effective_min_confidence()
            try:
                response = await self.client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=800,  # 500 có thể truncate JSON
                    system=_get_system_prompt(style),
                    messages=[{"role": "user", "content": user_prompt}],
                )
                self.db.add_anthropic_spend(CLAUDE_ESTIMATED_COST_PER_CALL)  # Track ngay sau API (trước JSON parse)

                raw = response.content[0].text.strip()
                # Strip markdown nếu có
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                raw = raw.strip()

                result = json.loads(raw)

                # Map pre-mortem verdict to legacy format
                verdict = (result.get("verdict") or "").upper()
                if verdict != "PROCEED":
                    result["should_trade"] = False
                    result["confidence"] = result.get("confidence", 50)
                    return result
                result["should_trade"] = True
                result["entry_price"] = entry
                result["stop_loss"] = sl
                result["take_profit"] = tp
                result["confidence"] = result.get("confidence", min_conf)
                result["reasoning"] = result.get("reasoning", "")
                return result

            except json.JSONDecodeError as e:
                logger.error(f"Claude returned invalid JSON: {e}")
                return None
            except Exception as e:
                logger.error(f"Claude API error: {e}")
                return None

    def _is_in_scalp_active_hours(self, spec: str) -> bool:
        """spec = '8-16' → 8h-16h UTC. Để trống = always True."""
        if not spec or "-" not in spec:
            return True
        try:
            parts = spec.split("-")
            start_h = int(parts[0].strip())
            end_h = int(parts[1].strip())
            now = datetime.now(timezone.utc)
            h = now.hour
            if start_h <= end_h:
                return start_h <= h < end_h
            return h >= start_h or h < end_h  # e.g. 20-4 = overnight
        except (ValueError, IndexError):
            return True

    async def _filter_by_1h_range(
        self, pairs: list[str], min_range_pct: float
    ) -> list[str]:
        """Scalp: chỉ giữ pairs có range 1-2h gần nhất (high-low)/close >= min_range_pct (đang active)."""
        if not pairs or min_range_pct <= 0:
            return pairs
        sem = asyncio.Semaphore(10)  # Rate limit: max 10 klines calls đồng thời

        async def _fetch(p: str):
            async with sem:
                return await self.binance.get_klines(p, "1h", 4)  # Chỉ cần 2-4 candles

        tasks = [_fetch(p) for p in pairs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        kept = []
        for pair, df in zip(pairs, results):
            if isinstance(df, Exception) or df is None or len(df) < 2:
                kept.append(pair)  # Lỗi → giữ (fail open)
                continue
            # Last 1-2 candles (1-2 giờ gần nhất), không phải 24h
            high = float(df["high"].iloc[-2:].max())
            low = float(df["low"].iloc[-2:].min())
            close = float(df["close"].iloc[-1])
            if close <= 0:
                kept.append(pair)
                continue
            range_pct = (high - low) / close * 100
            if range_pct >= min_range_pct:
                kept.append(pair)
            else:
                logger.debug(f"Scalp 1-2h filter: {pair} range {range_pct:.2f}% < {min_range_pct}%")
        return kept

    async def _get_available_balance(self) -> float:
        """Lấy available balance (trừ locked capital trong open positions)"""
        if cfg.trading.paper_trading:
            total = cfg.trading.paper_balance_usdt
        else:
            total = 10000.0  # TODO: Fetch from Binance
        open_trades = self.db.get_open_trades()
        locked = sum(t["position_size_usdt"] for t in open_trades)
        return max(0.0, total - locked)

    async def run_full_scan(self) -> list[TradingSignal]:
        """Scan pairs — fixed mode: ALLOWED_PAIRS; opportunity mode: dynamic screening."""
        if self.db.get_today_spend() >= cfg.anthropic_daily_budget_usd:
            logger.warning("Daily budget exceeded, skipping full scan")
            return []

        sc = cfg.scan
        pairs_to_scan: list[str]
        fallback_used = False
        ticker_volatility_map: dict[str, float] = {}  # For scan_state upsert (opportunity mode)

        if sc.scan_mode == "opportunity":
            # Scalp time-of-day filter: chỉ scan trong giờ active (ví dụ 8-16 UTC)
            if sc.trading_style == "scalp" and sc.scalp_active_hours_utc:
                if not self._is_in_scalp_active_hours(sc.scalp_active_hours_utc):
                    logger.info(
                        f"Scalp: outside active hours ({sc.scalp_active_hours_utc} UTC), skipping scan"
                    )
                    return []
            tickers, premium_data = await asyncio.gather(
                self.binance.get_all_tickers_24hr(),
                self.binance.get_premium_index_full(),
            )
            if not tickers:
                logger.warning("get_all_tickers_24hr failed or empty, fallback to ALLOWED_PAIRS")
                fallback_used = True
                pairs_to_scan = ALLOWED_PAIRS
            else:
                futures_symbols = set(p["symbol"] for p in premium_data) if premium_data else set()
                funding_map = (
                    {p["symbol"]: float(p.get("lastFundingRate") or 0) for p in premium_data}
                    if premium_data
                    else {}
                )
                if not premium_data:
                    logger.warning("get_premium_index_full failed/empty, futures filter disabled")

                # Confluence min: auto từ BTC 24h | manual từ config
                confluence_min = 2 if sc.market_regime == "sideways" else 1
                if sc.market_regime_mode == "auto":
                    btc_ticker = next((t for t in tickers if t.get("symbol") == "BTCUSDT"), None)
                    if btc_ticker:
                        btc_pct = abs(float(btc_ticker.get("priceChangePercent") or 0))
                        confluence_min = 2 if btc_pct < 2.0 else 1

                # Cooldown + hysteresis
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

                pairs_to_scan = get_opportunity_pairs(
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
                # Scalp: filter by 1h range — chỉ scan coin đang active (không phải 24h đã move xong)
                # RELAX_FILTER: bỏ qua filter này để scan nhiều pair hơn
                if (
                    sc.trading_style == "scalp"
                    and pairs_to_scan
                    and sc.scalp_1h_range_min_pct > 0
                    and not getattr(sc, "relax_filter", False)
                ):
                    pairs_to_scan = await self._filter_by_1h_range(pairs_to_scan, sc.scalp_1h_range_min_pct)
                ticker_volatility_map = {
                    t["symbol"]: abs(float(t.get("priceChangePercent") or 0))
                    for t in tickers if t.get("symbol")
                }
                logger.info(
                    f"Opportunity scan: {len(pairs_to_scan)} pairs "
                    f"(volatility {sc.opportunity_volatility_pct}–{sc.opportunity_volatility_max_pct}%)"
                )
            if not pairs_to_scan and fallback_used and not ALLOWED_PAIRS:
                logger.error("Ticker fetch failed and ALLOWED_PAIRS empty, skipping cycle")
                return []
        else:
            pairs_to_scan = ALLOWED_PAIRS
            logger.info(f"Starting full market scan ({len(pairs_to_scan)} pairs, fixed mode)...")

        if not pairs_to_scan:
            logger.info("No pairs to scan")
            return []

        # Session (UTC): asia 0-8, london 8-13, ny_overlap 13-20, dead_zone 20-24 (sau US close)
        hour_utc = datetime.now(timezone.utc).hour
        session = "london" if 8 <= hour_utc < 13 else ("ny_overlap" if 13 <= hour_utc < 20 else ("asia" if hour_utc < 8 else "dead_zone"))

        # Scalp: Session filter — dead_zone skip, asia=core only, london+ny=all
        if sc.trading_style == "scalp" and sc.scalp_session_filter and not getattr(sc, "relax_filter", False):
            if session == "dead_zone":
                logger.info(f"Session={session} (UTC {hour_utc}h): skip scalp cycle")
                return []
            elif session == "asia":
                core = set(sc.core_pairs or ["BTCUSDT", "ETHUSDT"])
                pairs_to_scan = [p for p in pairs_to_scan if p in core]
                logger.info(f"Asia session: filtered to {len(pairs_to_scan)} core pairs only")
            if not pairs_to_scan:
                return []

        # Scalp: BTC volatility filter — khi BTC quá volatile, altcoin bị kéo theo, chỉ trade BTC/ETH
        if sc.trading_style == "scalp" and len(pairs_to_scan) > 0:
            try:
                btc_tech = await self.binance.compute_technical_signal("BTCUSDT", style="scalp")
                if btc_tech.atr_pct > 0.5:  # 0.5% chưa validated, bắt đầu conservative
                    core = set(sc.core_pairs or ["BTCUSDT", "ETHUSDT"])
                    filtered = [p for p in pairs_to_scan if p in core]
                    if filtered != pairs_to_scan:
                        logger.info(
                            f"BTC ATR% {btc_tech.atr_pct:.2f}% > 0.5% — chỉ scan BTC/ETH, "
                            f"{len(pairs_to_scan)} → {len(filtered)} pairs"
                        )
                        pairs_to_scan = filtered
            except Exception as e:
                logger.warning(f"BTC volatility filter failed: {e}, scan all pairs")

        # Dry-run: chỉ log, không analyze
        if sc.scan_dry_run:
            logger.info(f"DRY-RUN: would scan {pairs_to_scan}")
            self.db.log(
                "research_agent",
                "INFO",
                "Dry-run: pairs would be scanned",
                {"pairs": pairs_to_scan, "scan_mode": sc.scan_mode},
            )
            return []

        # News blackout (scalp): skip cycle nếu có High-impact event trong 30 phút
        if sc.trading_style == "scalp" and not getattr(sc, "relax_filter", False):
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
                    events = r.json()
                now = datetime.now(timezone.utc)
                for ev in events:
                    if ev.get("impact") != "High":
                        continue
                    try:
                        ev_time = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
                        if ev_time.tzinfo is None:
                            ev_time = ev_time.replace(tzinfo=timezone.utc)
                        if abs((now - ev_time).total_seconds()) < 1800:
                            logger.info(f"News blackout: {ev.get('title', '?')} — skip scalp cycle")
                            return []
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"News calendar fetch failed: {e}")

        # Fear & Greed: fetch 1 lần cho cả cycle (daily index, giống nhau mọi pair)
        try:
            shared_sentiment = await self.fear_greed.get()
            logger.info(f"Fear & Greed: {shared_sentiment.fear_greed_index} ({shared_sentiment.fear_greed_label})")
        except Exception as e:
            logger.warning(f"Fear & Greed pre-fetch failed: {e}, fallback fetch riêng từng pair")
            shared_sentiment = None

        # Dynamic confluence: thắt chặt khi win rate < 45% (last 20 trades)
        perf = self.db.get_recent_performance(20)
        min_confluence = 3
        if perf["win_rate"] is not None:
            if perf["win_rate"] < 0.45:
                min_confluence = 4
                logger.warning(f"Win rate {perf['win_rate']:.0%} < 45% (last {perf['sample_size']}), raising confluence to 4")

        # _pair_semaphore tự động chia batch 5 — tối đa 5 pairs đồng thời
        results = await asyncio.gather(
            *[self.analyze_pair(pair, prefetched_sentiment=shared_sentiment, session=session, min_confluence=min_confluence) for pair in pairs_to_scan],
            return_exceptions=True,
        )

        signals = []
        rule_based_passed = 0
        claude_passed = 0
        ema9_rejected = 0
        confluence_rejected = 0
        opportunity_candidates = len(pairs_to_scan) if sc.scan_mode == "opportunity" else 0

        for pair, result in zip(pairs_to_scan, results):
            if isinstance(result, Exception):
                logger.error(f"{pair} scan failed: {result}")
                self.db.log("research_agent", "ERROR", f"Scan failed: {result}", {"pair": pair})
                continue
            signal, meta = result
            if meta.get("rule_passed"):
                rule_based_passed += 1
            if meta.get("claude_proceed"):
                claude_passed += 1
            if meta.get("ema9_rejected"):
                ema9_rejected += 1
            if meta.get("confluence_rejected"):
                confluence_rejected += 1
            if signal:  # analyze_pair đã filter confidence >= min_conf
                signals.append(signal)

        # Observability: log funnel metrics mỗi cycle
        funnel = {
            "scan_mode": sc.scan_mode,
            "session": session if sc.trading_style == "scalp" else None,
            "opportunity_candidates": opportunity_candidates,
            "pairs_scanned": len(pairs_to_scan),
            "pairs_scanned_list": pairs_to_scan,  # Để UI hiển thị pair nào được scan
            "rule_based_passed": rule_based_passed,
            "ema9_rejected": ema9_rejected,
            "confluence_rejected": confluence_rejected,
            "claude_passed": claude_passed,
            "signals_generated": len(signals),
            "fallback_used": fallback_used,
        }
        self.db.log("research_agent", "INFO", "Scan cycle funnel", funnel)

        # Update scan_state (cooldown/hysteresis) khi opportunity mode
        if sc.scan_mode == "opportunity" and ticker_volatility_map:
            now_iso = datetime.now(timezone.utc).isoformat()
            for pair in pairs_to_scan:
                vol = ticker_volatility_map.get(pair, 0.0)
                self.db.upsert_scan_state(
                    symbol=pair,
                    last_scanned_at=now_iso,
                    last_seen_volatility=vol,
                    in_opportunity=True,
                )

        logger.info(f"Scan complete: {len(signals)}/{len(pairs_to_scan)} pairs with signals")
        return signals

    async def close(self):
        await self.binance.close()
        await self.whale.close()
        await self.fear_greed.close()
