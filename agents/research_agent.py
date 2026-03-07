"""
agents/research_agent.py - Claude Sonnet pre-mortem risk assessment + rule-based flow
"""
import asyncio
import json
import uuid
from typing import Optional
from anthropic import AsyncAnthropic
from loguru import logger

from config import cfg, ALLOWED_PAIRS
from models import (
    TradingSignal, Direction,
    TechnicalSignal, WhaleSignal, SentimentSignal, DerivativesSignal,
    SignalStatus
)
from utils.market_data import (
    BinanceDataFetcher, WhaleDataFetcher, FearGreedFetcher,
    classify_regime, calc_entry_sl_tp,
)
from database import Database

# Estimated cost per Claude call (Sonnet) for budget tracking
CLAUDE_ESTIMATED_COST_PER_CALL = 0.005

RESEARCH_SYSTEM_PROMPT = """
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

Nếu verdict = PROCEED: confidence >= 75. Nếu WAIT/AVOID: confidence < 75.
KHÔNG thêm text ngoài JSON.
"""


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
        logger.info("ResearchAgent initialized")

    def _rule_based_filter(
        self,
        technical: TechnicalSignal,
        derivatives: DerivativesSignal,
    ) -> Optional[str]:
        """
        Rule-based Gate. Returns LONG | SHORT | None.
        Claude chỉ được gọi khi filter trả về non-None.
        """
        funding_pct = derivatives.funding_rate * 100  # 0.0001 → 0.01%
        # LONG: trend_1d != downtrend, RSI < 45, funding < 0.05%, net_score > 10
        if (
            technical.trend_1d != "downtrend"
            and technical.rsi_1h < 45
            and funding_pct < 0.05
            and technical.net_score > 10
        ):
            return "LONG"
        # SHORT: trend_1d != uptrend, RSI > 55, funding > 0.05%, net_score < -10
        if (
            technical.trend_1d != "uptrend"
            and technical.rsi_1h > 55
            and funding_pct > 0.05
            and technical.net_score < -10
        ):
            return "SHORT"
        return None

    async def analyze_pair(self, pair: str) -> Optional[TradingSignal]:
        """
        Phân tích một cặp tiền → trả về TradingSignal nếu có cơ hội.
        Flow: gather → rule-based filter → regime → calc entry/SL/TP → Claude risk assessment.
        """
        logger.info(f"Analyzing {pair}...")

        try:
            # 1. Thu thập data song song (thêm derivatives)
            current_price, technical, whale_data, sentiment, derivatives = await asyncio.gather(
                self.binance.get_current_price(pair),
                self.binance.compute_technical_signal(pair),
                self.whale.get_whale_transactions(pair),
                self.fear_greed.get(),
                self.binance.get_derivatives_signal(pair),
            )
            logger.info(
                f"{pair} | Price: ${current_price:,.2f} | "
                f"Tech: {technical.net_score} | Whale: {whale_data.score} | "
                f"Funding: {derivatives.funding_rate*100:.3f}% | F&G: {sentiment.fear_greed_index}"
            )

            # 2. Rule-based filter — không pass thì skip Claude
            direction = self._rule_based_filter(technical, derivatives)
            if direction is None:
                logger.info(f"{pair}: Rule-based filter → No trade")
                self.db.log("research_agent", "INFO", f"Filter rejected {pair}", {"pair": pair})
                return None

            # 3. Regime (ADX + BB + ATR)
            regime = classify_regime(
                technical.adx, technical.plus_di, technical.minus_di,
                technical.bb_width, technical.atr_ratio,
            )

            if not (technical.atr_value > 0):
                logger.warning(f"{pair}: ATR invalid ({technical.atr_value}), skipping")
                return None

            # 4. Position size check TRƯỚC Claude (tránh lãng phí budget khi available=0)
            available = await self._get_available_balance()
            position_size = min(
                available * cfg.trading.max_position_pct,
                available * 0.4,  # Hard cap 40% available cho 1 trade
            )
            if position_size < 10:
                logger.info(f"{pair}: Position size too small (${position_size:.2f}), skipping (pre-Claude)")
                return None

            # 5. Calc entry/SL/TP (rule-based, ATR)
            entry, sl, tp = calc_entry_sl_tp(
                direction, current_price, technical.atr_value, regime
            )

            # 6. Claude risk assessment (pre-mortem)
            analysis = await self._claude_analyze(
                pair, current_price, technical, whale_data, sentiment, derivatives,
                direction, entry, sl, tp, regime,
            )

            if not analysis or not analysis.get("should_trade"):
                verdict = analysis.get("verdict", "N/A") if analysis else "budget/error"
                confidence = analysis.get("confidence", 0) if analysis else 0
                logger.info(f"{pair}: Claude → {verdict} (confidence: {confidence})")
                self.db.log("research_agent", "INFO",
                            f"No opportunity for {pair}",
                            {"confidence": confidence})
                return None

            # 7. Build signal
            confidence = int(analysis["confidence"])
            if confidence < cfg.trading.min_confidence:
                logger.info(f"{pair}: Confidence {confidence} < {cfg.trading.min_confidence}, skipping")
                return None

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
            return signal

        except Exception as e:
            err_detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.exception(f"Error analyzing {pair}: {err_detail}")
            self.db.log("research_agent", "ERROR", f"Analysis failed for {pair}", {"error": err_detail})
            return None

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

            user_prompt = f"""
Pre-mortem risk assessment cho {pair}:

PROPOSED SETUP ({direction}):
- Entry: ${entry:,.2f}
- Stop Loss: ${sl:,.2f} (-{risk_pct:.1f}%)
- Take Profit: ${tp:,.2f} (+{reward_pct:.1f}%)
- Regime: {regime}

DATA:
- Price: ${current_price:,.2f} | Trend 1D: {technical.trend_1d}
- RSI 1h: {technical.rsi_1h:.1f} | RSI 4h: {technical.rsi_4h:.1f}
- Tech net_score: {technical.net_score} | Whale: {whale.score}/100
- Funding: {derivatives.funding_rate*100:.3f}% | Basis: {derivatives.basis_pct:.2f}%
- F&G: {sentiment.fear_greed_index}/100 | Net flow: ${whale.net_flow/1e6:.1f}M

1. Top 3 risks invalidate trade?
2. Most likely failure mode?
3. News/event 24h?
→ Verdict: PROCEED / WAIT / AVOID
"""

            try:
                response = await self.client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=500,
                    system=RESEARCH_SYSTEM_PROMPT,
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
                result["confidence"] = result.get("confidence", 75)
                result["reasoning"] = result.get("reasoning", "")
                return result

            except json.JSONDecodeError as e:
                logger.error(f"Claude returned invalid JSON: {e}")
                return None
            except Exception as e:
                logger.error(f"Claude API error: {e}")
                return None

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
        """Scan toàn bộ ALLOWED_PAIRS, return signals có confidence >= min"""
        logger.info(f"Starting full market scan ({len(ALLOWED_PAIRS)} pairs)...")

        if self.db.get_today_spend() >= cfg.anthropic_daily_budget_usd:
            logger.warning("Daily budget exceeded, skipping full scan")
            return []

        # Chạy song song để giảm latency (1 pair lỗi không block các pair khác)
        results = await asyncio.gather(
            *[self.analyze_pair(pair) for pair in ALLOWED_PAIRS],
            return_exceptions=True,
        )

        signals = []
        for pair, result in zip(ALLOWED_PAIRS, results):
            if isinstance(result, Exception):
                logger.error(f"{pair} scan failed: {result}")
                self.db.log("research_agent", "ERROR", f"Scan failed: {result}", {"pair": pair})
                continue
            signal = result
            if signal:  # analyze_pair đã filter confidence >= min_conf
                signals.append(signal)

        logger.info(f"Scan complete: {len(signals)}/{len(ALLOWED_PAIRS)} pairs with signals")
        return signals

    async def close(self):
        await self.binance.close()
        await self.whale.close()
        await self.fear_greed.close()
