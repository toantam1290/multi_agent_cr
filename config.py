"""
config.py - Tất cả config tập trung một chỗ
"""
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass
class TradingConfig:
    max_position_pct: float = float(os.getenv("MAX_POSITION_PCT", "0.02"))
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.01"))
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    # Phải khớp với RESEARCH_SYSTEM_PROMPT (Claude recommend >= 75)
    min_confidence: int = int(os.getenv("MIN_CONFIDENCE", "75"))
    min_risk_reward: float = float(os.getenv("MIN_RISK_REWARD", "2.0"))
    approval_timeout_sec: int = int(os.getenv("APPROVAL_TIMEOUT_SEC", "300"))
    # Scalp override: confidence cao hơn (setup nhanh), timeout ngắn (setup fade nhanh)
    scalp_min_confidence: int = int(os.getenv("SCALP_MIN_CONFIDENCE", "80"))
    scalp_approval_timeout_sec: int = int(os.getenv("SCALP_APPROVAL_TIMEOUT_SEC", "120"))
    scalp_risk_reward_ratio: float = float(os.getenv("SCALP_RISK_REWARD_RATIO", "1.5"))
    paper_trading: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"
    paper_balance_usdt: float = float(os.getenv("PAPER_BALANCE_USDT", "10000"))


@dataclass
class BinanceConfig:
    api_key: str = os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    testnet: bool = os.getenv("BINANCE_TESTNET", "true").lower() == "true"


@dataclass
class TelegramConfig:
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")


def _parse_list_env(key: str, default: str) -> list[str]:
    raw = os.getenv(key, default)
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass
class ScanConfig:
    """Opportunity screening config (SCAN_MODE=opportunity)."""
    scan_mode: str = os.getenv("SCAN_MODE", "fixed")
    opportunity_volatility_pct: float = float(os.getenv("OPPORTUNITY_VOLATILITY_PCT", "5.0"))
    opportunity_volatility_max_pct: float = float(os.getenv("OPPORTUNITY_VOLATILITY_MAX_PCT", "25.0"))
    min_quote_volume_usd: float = float(os.getenv("MIN_QUOTE_VOLUME_USD", "5000000"))
    max_pairs_per_scan: int = int(os.getenv("MAX_PAIRS_PER_SCAN", "30"))
    core_pairs: list[str] = None
    scan_blacklist: list[str] = None
    opportunity_use_whitelist: bool = os.getenv("OPPORTUNITY_USE_WHITELIST", "false").lower() == "true"
    scan_dry_run: bool = os.getenv("SCAN_DRY_RUN", "false").lower() == "true"
    market_regime_mode: str = os.getenv("MARKET_REGIME_MODE", "auto")
    market_regime: str = os.getenv("MARKET_REGIME", "sideways")
    # Confluence: sideways -> min 2, trend -> min 1
    confluence_min_score: int = 1  # Overridden by regime
    # Cooldown: nghỉ N cycle sau khi scan (cycle ~15 phút)
    cooldown_cycles: int = int(os.getenv("COOLDOWN_CYCLES", "2"))
    cycle_interval_sec: int = int(os.getenv("CYCLE_INTERVAL_SEC", "900"))
    # Hysteresis: entry khi |change| >= entry, exit khi |change| < exit
    hysteresis_entry_pct: float = float(os.getenv("HYSTERESIS_ENTRY_PCT", "5.0"))
    hysteresis_exit_pct: float = float(os.getenv("HYSTERESIS_EXIT_PCT", "3.0"))
    funding_extreme_threshold: float = float(os.getenv("FUNDING_EXTREME_THRESHOLD", "0.001"))  # 0.1%
    # swing (1h/4h/1d) | scalp (15m/1h). Auto: opportunity->scalp, fixed->swing. Override: TRADING_STYLE
    trading_style: str = (os.getenv("TRADING_STYLE", "").strip().lower() or None)
    # Scan interval (phút): scalp=5 nhanh, swing=15. Override: SCAN_INTERVAL_MIN
    scan_interval_min: int = 0  # 0 = auto từ trading_style
    # Position monitor (phút): scalp=1, swing=2. Override: POSITION_MONITOR_INTERVAL_MIN
    position_monitor_interval_min: int = 0  # 0 = auto
    # RSI filter: scalp nới hơn (50/50), swing chặt (45/55). Override: SCALP_RSI_LONG_MAX, SCALP_RSI_SHORT_MIN
    scalp_rsi_long_max: float = float(os.getenv("SCALP_RSI_LONG_MAX", "50"))
    scalp_rsi_short_min: float = float(os.getenv("SCALP_RSI_SHORT_MIN", "50"))
    # Funding: LONG cần funding < max, SHORT cần funding > min
    funding_long_max_pct: float = float(os.getenv("FUNDING_LONG_MAX_PCT", "0.03"))
    funding_short_min_pct: float = float(os.getenv("FUNDING_SHORT_MIN_PCT", "-0.03"))  # Symmetric: block SHORT chỉ khi funding cực âm
    # 1h range min (high-low)/close % — scalp cần coin đang active, không phải 24h đã move xong
    scalp_1h_range_min_pct: float = float(os.getenv("SCALP_1H_RANGE_MIN_PCT", "0.5"))
    # Scalp active hours UTC (ví dụ "8-16" = 8h-16h UTC). Để trống = 24/7
    scalp_active_hours_utc: str = os.getenv("SCALP_ACTIVE_HOURS_UTC", "").strip()
    # Session filter: dead_zone skip, asia=core only, london+ny=all. SCALP_SESSION_FILTER=false để tắt
    scalp_session_filter: bool = os.getenv("SCALP_SESSION_FILTER", "true").lower() == "true"
    # Whale data: scalp cần context ngắn (1h), swing 4h. Override: SCALP_WHALE_HOURS
    scalp_whale_hours: int = int(os.getenv("SCALP_WHALE_HOURS", "1"))
    # RELAX_FILTER=true: nới filter để test pipeline (net_score 5/-5, bỏ volume/momentum). Chỉ dùng khi test!
    relax_filter: bool = os.getenv("RELAX_FILTER", "false").lower() == "true"

    def __post_init__(self):
        if self.trading_style is None or self.trading_style not in ("swing", "scalp"):
            self.trading_style = "scalp" if self.scan_mode == "opportunity" else "swing"
        if self.scan_interval_min <= 0:
            self.scan_interval_min = int(os.getenv("SCAN_INTERVAL_MIN", "5" if self.trading_style == "scalp" else "15"))
        if self.position_monitor_interval_min <= 0:
            self.position_monitor_interval_min = int(os.getenv("POSITION_MONITOR_INTERVAL_MIN", "1" if self.trading_style == "scalp" else "2"))
        if self.max_pairs_per_scan <= 0:
            self.max_pairs_per_scan = 15 if self.trading_style == "scalp" else 30
        if self.core_pairs is None:
            self.core_pairs = _parse_list_env("CORE_PAIRS", "BTCUSDT,ETHUSDT")
        if self.scan_blacklist is None:
            self.scan_blacklist = _parse_list_env(
                "SCAN_BLACKLIST", "USDCUSDT,BUSDUSDT,FDUSDUSDT,TUSDUSDT,DAIUSDT"
            )


@dataclass
class AppConfig:
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_daily_budget_usd: float = float(os.getenv("ANTHROPIC_DAILY_BUDGET_USD", "0.75"))
    trading: TradingConfig = None
    binance: BinanceConfig = None
    telegram: TelegramConfig = None
    scan: ScanConfig = None
    skip_telegram: bool = os.getenv("SKIP_TELEGRAM", "false").lower() == "true"

    def __post_init__(self):
        self.trading = TradingConfig()
        self.binance = BinanceConfig()
        self.telegram = TelegramConfig()
        self.scan = ScanConfig()

    def validate(self):
        errors = []
        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY chưa set")
        if not self.skip_telegram:
            if not self.telegram.bot_token:
                errors.append("TELEGRAM_BOT_TOKEN chưa set")
            if not self.telegram.chat_id:
                errors.append("TELEGRAM_CHAT_ID chưa set")
        if not self.trading.paper_trading:
            if not self.binance.api_key:
                errors.append("BINANCE_API_KEY chưa set (bắt buộc khi paper_trading=false)")
        # Opportunity screening validation
        self._validate_scan(errors)
        if errors:
            raise ValueError(f"Config errors:\n" + "\n".join(f"  - {e}" for e in errors))
        return True

    def _validate_scan(self, errors: list):
        s = self.scan
        if s.scan_mode not in ("fixed", "opportunity"):
            errors.append(f"SCAN_MODE phải là 'fixed' hoặc 'opportunity', hiện tại: {s.scan_mode}")
        if s.scan_mode == "opportunity":
            if s.opportunity_volatility_pct >= s.opportunity_volatility_max_pct:
                errors.append(
                    f"OPPORTUNITY_VOLATILITY_PCT ({s.opportunity_volatility_pct}) "
                    f"phải nhỏ hơn OPPORTUNITY_VOLATILITY_MAX_PCT ({s.opportunity_volatility_max_pct})"
                )
            if s.max_pairs_per_scan <= 0:
                errors.append(f"MAX_PAIRS_PER_SCAN phải > 0, hiện tại: {s.max_pairs_per_scan}")
            overlap = set(s.core_pairs) & set(s.scan_blacklist)
            if overlap:
                errors.append(f"CORE_PAIRS không được nằm trong SCAN_BLACKLIST: {overlap}")
        if s.market_regime_mode == "manual" and s.market_regime not in ("sideways", "trend"):
            errors.append(f"MARKET_REGIME phải là 'sideways' hoặc 'trend', hiện tại: {s.market_regime}")
        if s.trading_style not in ("swing", "scalp"):
            errors.append(f"TRADING_STYLE phải là 'swing' hoặc 'scalp', hiện tại: {s.trading_style}")


# Singleton
cfg = AppConfig()


def get_effective_min_confidence() -> int:
    """Scalp: cao hơn (80). Swing: 75."""
    return cfg.trading.scalp_min_confidence if cfg.scan.trading_style == "scalp" else cfg.trading.min_confidence


def get_effective_approval_timeout_sec() -> int:
    """Scalp: ngắn (120s). Swing: 300s."""
    return cfg.trading.scalp_approval_timeout_sec if cfg.scan.trading_style == "scalp" else cfg.trading.approval_timeout_sec


def get_effective_min_risk_reward() -> float:
    """Scalp: 1.5. Swing: 2.0."""
    return cfg.trading.scalp_risk_reward_ratio if cfg.scan.trading_style == "scalp" else cfg.trading.min_risk_reward

# Các cặp tiền được phép trade (đọc từ env, Stage 1: BTC+ETH, paper: 6 pairs)
ALLOWED_PAIRS = [
    p.strip() for p in os.getenv(
        "ALLOWED_PAIRS",
        "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT",
    ).split(",") if p.strip()
]

# Intervals để phân tích
ANALYSIS_INTERVALS = ["15m", "1h", "4h", "1d"]

# Whale threshold (USD)
WHALE_MIN_USD = 1_000_000

# Database — dùng absolute path để Web UI + scripts luôn trỏ đúng DB
_config_dir = Path(__file__).resolve().parent
DB_PATH = str(_config_dir / "data" / "trading.db")
