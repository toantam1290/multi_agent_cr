"""
config.py - Tất cả config tập trung một chỗ
"""
import os
from dataclasses import dataclass
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


@dataclass
class AppConfig:
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_daily_budget_usd: float = float(os.getenv("ANTHROPIC_DAILY_BUDGET_USD", "0.75"))
    trading: TradingConfig = None
    binance: BinanceConfig = None
    telegram: TelegramConfig = None

    def __post_init__(self):
        self.trading = TradingConfig()
        self.binance = BinanceConfig()
        self.telegram = TelegramConfig()

    def validate(self):
        errors = []
        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY chưa set")
        if not self.telegram.bot_token:
            errors.append("TELEGRAM_BOT_TOKEN chưa set")
        if not self.telegram.chat_id:
            errors.append("TELEGRAM_CHAT_ID chưa set")
        if not self.trading.paper_trading:
            if not self.binance.api_key:
                errors.append("BINANCE_API_KEY chưa set (bắt buộc khi paper_trading=false)")
        if errors:
            raise ValueError(f"Config errors:\n" + "\n".join(f"  - {e}" for e in errors))
        return True


# Singleton
cfg = AppConfig()

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

# Database
DB_PATH = "data/trading.db"
