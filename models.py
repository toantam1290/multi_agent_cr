"""
models.py - Pydantic models cho toàn bộ hệ thống
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalStatus(str, Enum):
    PENDING = "PENDING"       # Chờ approve
    APPROVED = "APPROVED"     # Đã approve, chờ execute
    EXECUTED = "EXECUTED"     # Đã execute
    REJECTED = "REJECTED"     # Bị reject bởi Risk Manager
    SKIPPED = "SKIPPED"       # User skip hoặc timeout
    CANCELLED = "CANCELLED"   # Hủy vì điều kiện thay đổi


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    STOPPED = "STOPPED"   # Hit stop-loss
    TOOK_PROFIT = "TOOK_PROFIT"


# ─── Technical Signals ───────────────────────────────────────────────────────

class TechnicalSignal(BaseModel):
    rsi_1h: float
    rsi_4h: float
    ema_cross_bullish: bool    # EMA 9 cắt lên EMA 21
    macd_bullish: bool
    volume_spike: bool         # Volume > 2x average
    volume_ratio: float = 0.0  # current_volume / avg_volume (cho scalp volume confirmation)
    volume_trend_up: bool = False  # 3 nến đóng gần nhất volume tăng liên tiếp (early-in-move)
    bb_squeeze: bool           # Bollinger Band squeeze
    support_level: Optional[float] = None
    resistance_level: Optional[float] = None
    trend_1d: str              # "uptrend" | "downtrend" | "sideways"
    score: int = Field(0, ge=0, le=100)  # Legacy bullish score
    net_score: int = Field(0, ge=-100, le=100)  # -100 bearish .. +100 bullish
    direction_bias: str = "NEUTRAL"  # "LONG" | "SHORT" | "NEUTRAL"
    momentum_bullish: bool = False   # Scalp: RSI 2-candle momentum long
    momentum_bearish: bool = False    # Scalp: RSI 2-candle momentum short
    # Regime + ATR (Sprint 3)
    atr_value: float = 0.0      # ATR(14) 1h
    atr_pct: float = 0.0       # atr_value / price * 100
    atr_ratio: float = 0.0      # ATR14 / ATR50
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    bb_width: float = 0.0       # Bollinger bandwidth
    bb_width_regime: float = 0.0   # Cho classify_regime: scalp = 1h, swing = 1h
    atr_ratio_regime: float = 0.0  # Cho classify_regime: scalp = 1h, swing = 1h
    current_price: float = 0.0   # Close của nến cuối (df_fast) — swing dùng thay get_current_price
    # Scalp: swing structure cho SL, entry timing
    swing_low: float = 0.0       # Min low của 10 nến 5m gần nhất
    swing_high: float = 0.0      # Max high của 10 nến 5m gần nhất
    ema9_just_crossed_up: bool = False   # Close vừa cross lên EMA9 (legacy, quá strict)
    ema9_just_crossed_down: bool = False  # Close vừa cross xuống EMA9 (legacy)
    ema9_crossed_recent_up: bool = False   # Cross lên trong 3 nến gần nhất (nới hơn)
    ema9_crossed_recent_down: bool = False  # Cross xuống trong 3 nến gần nhất
    # Order flow (production scalping)
    vwap: float = 0.0                   # VWAP intraday
    vwap_distance_pct: float = 0.0       # % distance, dương = trên VWAP
    chop_index: float = 50.0             # < 38.2 trending, > 61.8 choppy


class WhaleSignal(BaseModel):
    large_transfers_count: int = 0
    large_transfers_usd: float = 0.0
    exchange_inflow_usd: float = 0.0   # BTC vào sàn → bearish
    exchange_outflow_usd: float = 0.0  # BTC rời sàn → bullish
    top_transfers: list[dict] = Field(default_factory=list)
    net_flow: float = 0.0              # outflow - inflow (dương = bullish)
    score: int = Field(0, ge=0, le=100)


class SentimentSignal(BaseModel):
    fear_greed_index: int = 50         # 0=extreme fear, 100=extreme greed
    fear_greed_label: str = "Neutral"
    score: int = Field(0, ge=0, le=100)


class ArbitrageSignal(BaseModel):
    pair: str
    binance_price: float
    spread_pct: float = 0.0
    funding_rate: float = 0.0          # Futures funding rate
    profitable: bool = False
    estimated_profit_pct: float = 0.0


class DerivativesSignal(BaseModel):
    """Funding, OI, basis từ Binance Futures — 1 factor multi-source"""
    funding_rate: float = 0.0          # 8h rate, e.g. 0.0001 = 0.01%
    funding_rate_annualized: float = 0.0
    open_interest_usdt: float = 0.0
    oi_change_pct: float = 0.0         # 24h OI change
    basis_pct: float = 0.0             # (mark - index) / index * 100
    signal: str = "NEUTRAL"            # LONG_SQUEEZE | SHORT_SQUEEZE | NEUTRAL
    score: int = Field(0, ge=-100, le=100)


# ─── Main Signal ─────────────────────────────────────────────────────────────

class TradingSignal(BaseModel):
    id: str                            # UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    pair: str
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size_usdt: float

    # Sub-signals
    technical: TechnicalSignal
    whale: WhaleSignal
    sentiment: SentimentSignal

    # Claude's analysis
    confidence: int = Field(0, ge=0, le=100)
    reasoning: str = ""                # Claude giải thích tại sao
    risk_reward: float = 0.0

    # Lifecycle
    status: SignalStatus = SignalStatus.PENDING
    approved_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    telegram_message_id: Optional[int] = None

    # Regime + model (Sprint 3)
    regime: Optional[str] = None       # trending_up | trending_down | ranging | volatile
    model_version: Optional[str] = None

    # SMC context (Smart Money Concepts) — snapshot serialized để lưu DB
    smc: Optional[dict] = None

    @property
    def risk_pct(self) -> float:
        """% rủi ro từ entry đến stop-loss"""
        if self.direction == Direction.LONG:
            return (self.entry_price - self.stop_loss) / self.entry_price * 100
        return (self.stop_loss - self.entry_price) / self.entry_price * 100

    @property
    def reward_pct(self) -> float:
        """% lợi nhuận từ entry đến take-profit"""
        if self.direction == Direction.LONG:
            return (self.take_profit - self.entry_price) / self.entry_price * 100
        return (self.entry_price - self.take_profit) / self.entry_price * 100

    def to_telegram_message(self) -> str:
        emoji = "🟢" if self.direction == Direction.LONG else "🔴"
        dir_text = "LONG ▲" if self.direction == Direction.LONG else "SHORT ▼"

        tech = self.technical
        whale = self.whale
        # Strip Markdown special chars (MarkdownV1 không hỗ trợ backslash escape)
        reasoning_safe = re.sub(r"[*_`\[\]()]", "", (self.reasoning or ""))[:300]

        return f"""
{emoji} *TRADING SIGNAL*
━━━━━━━━━━━━━━━━━━━━
📊 *{self.pair}* — {dir_text}
🎯 Confidence: *{self.confidence}/100*

💰 *Levels*
  Entry:       `${self.entry_price:,.2f}`
  Stop Loss:   `${self.stop_loss:,.2f}` (-{self.risk_pct:.1f}%)
  Take Profit: `${self.take_profit:,.2f}` (+{self.reward_pct:.1f}%)
  R:R Ratio:   `1:{self.risk_reward:.1f}`

📐 *Technical* (score: {tech.score}/100)
  RSI 1h/4h: `{tech.rsi_1h:.0f}` / `{tech.rsi_4h:.0f}`
  EMA Cross: `{'✅ Bullish' if tech.ema_cross_bullish else '❌ No'}`
  Volume Spike: `{'✅ Yes' if tech.volume_spike else '❌ No'}`
  Trend (1D): `{tech.trend_1d}`

🐋 *Whale Activity* (score: {whale.score}/100)
  Net Flow: `${whale.net_flow/1e6:.1f}M` {'(bullish 🟢)' if whale.net_flow > 0 else '(bearish 🔴)'}
  Large Txs: `{whale.large_transfers_count}`

😨 *Fear & Greed*: `{self.sentiment.fear_greed_index}` — {self.sentiment.fear_greed_label}

📝 *Analysis*
_{reasoning_safe}..._

━━━━━━━━━━━━━━━━━━━━
Size: `${self.position_size_usdt:,.0f} USDT`

Reply /approve {self.id[:8]} hoặc /skip {self.id[:8]}
⏰ Timeout: {self._approval_timeout_min()} phút
""".strip()

    def _approval_timeout_min(self) -> int:
        """Scalp: 2 phút, swing: 5 phút."""
        try:
            from config import get_effective_approval_timeout_sec
            return max(1, get_effective_approval_timeout_sec() // 60)
        except Exception:
            return 5


# ─── Trade (sau khi execute) ──────────────────────────────────────────────────

class Trade(BaseModel):
    id: str
    signal_id: str
    pair: str
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    position_size_usdt: float
    binance_order_id: Optional[str] = None
    status: TradeStatus = TradeStatus.OPEN
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl_usdt: Optional[float] = None
    pnl_pct: Optional[float] = None
    fees_usdt: Optional[float] = None  # Slippage + trading fee
    is_paper: bool = True
    sl_trailing_state: str = "original"  # original | breakeven | locked_50 (trail stop)


# ─── Portfolio State ──────────────────────────────────────────────────────────

class PortfolioState(BaseModel):
    total_usdt: float
    available_usdt: float
    open_trades: list[Trade] = Field(default_factory=list)
    daily_pnl_usdt: float = 0.0
    daily_pnl_pct: float = 0.0
    total_pnl_usdt: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0

    @property
    def open_position_count(self) -> int:
        return len(self.open_trades)
