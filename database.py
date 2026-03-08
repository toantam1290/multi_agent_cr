"""
database.py - SQLite local database để lưu signals, trades, logs
"""
import json
import sqlite3
import threading
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional
from loguru import logger
from models import TradingSignal, Trade, SignalStatus


class Database:
    def __init__(self, db_path: str = "data/trading.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._write_lock = threading.RLock()  # Reentrant (ensure_daily_stats_row gọi từ add_anthropic_spend)
        self._create_tables()
        self.migrate()
        logger.info(f"Database initialized: {db_path}")

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                pair TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                position_size_usdt REAL NOT NULL,
                confidence INTEGER NOT NULL,
                reasoning TEXT,
                risk_reward REAL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                approved_at TEXT,
                executed_at TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL,
                pair TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                quantity REAL NOT NULL,
                position_size_usdt REAL NOT NULL,
                binance_order_id TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                exit_price REAL,
                pnl_usdt REAL,
                pnl_pct REAL,
                is_paper INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                total_signals INTEGER DEFAULT 0,
                approved_signals INTEGER DEFAULT 0,
                executed_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                pnl_usdt REAL DEFAULT 0.0,
                pnl_pct REAL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS agent_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                agent TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                data TEXT
            );

            CREATE TABLE IF NOT EXISTS scan_state (
                symbol TEXT PRIMARY KEY,
                last_scanned_at TEXT,
                last_seen_volatility REAL,
                in_opportunity INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
            CREATE INDEX IF NOT EXISTS idx_signals_pair ON signals(pair);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_pair ON trades(pair);
        """)
        self.conn.commit()

    def migrate(self):
        """Idempotent migration — add new columns if not exist."""
        migrations = [
            "ALTER TABLE daily_stats ADD COLUMN anthropic_spend_usd REAL DEFAULT 0.0",
            "ALTER TABLE signals ADD COLUMN regime TEXT",
            "ALTER TABLE signals ADD COLUMN net_score INTEGER",
            "ALTER TABLE signals ADD COLUMN model_version TEXT",
            "ALTER TABLE trades ADD COLUMN fees_usdt REAL",
            "ALTER TABLE signals ADD COLUMN cancel_reason TEXT",
            "ALTER TABLE trades ADD COLUMN sl_trailing_state TEXT DEFAULT 'original'",
        ]
        with self._write_lock:
            for sql in migrations:
                try:
                    self.conn.execute(sql)
                    self.conn.commit()
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
        logger.debug("Database migration complete")

    def expire_stale_pending_signals(self, timeout_sec: int):
        """Expire PENDING signals cũ hơn timeout (gọi khi start, recovery sau restart)."""
        with self._write_lock:
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_sec)).isoformat()
            cur = self.conn.execute(
                "UPDATE signals SET status = 'SKIPPED', cancel_reason = 'timeout' WHERE status = 'PENDING' AND created_at < ?",
                (cutoff,),
            )
            self.conn.commit()
            if cur.rowcount > 0:
                logger.info(f"Expired {cur.rowcount} stale PENDING signals")

    def ensure_daily_stats_row(self, for_date: date = None):
        """Ensure daily_stats has row for date (needed for budget cap on first run)."""
        with self._write_lock:
            d = (for_date or datetime.now(timezone.utc).date()).isoformat()
            self.conn.execute(
                "INSERT OR IGNORE INTO daily_stats (date, total_signals, approved_signals, executed_trades, winning_trades, pnl_usdt, pnl_pct) VALUES (?, 0, 0, 0, 0, 0.0, 0.0)",
                (d,),
            )
            self.conn.commit()

    def get_today_spend(self) -> float:
        """Get anthropic API spend for today (for budget cap)."""
        self.ensure_daily_stats_row()
        d = datetime.now(timezone.utc).date().isoformat()
        try:
            row = self.conn.execute(
                "SELECT anthropic_spend_usd FROM daily_stats WHERE date = ?", (d,)
            ).fetchone()
        except sqlite3.OperationalError:
            return 0.0  # Column not yet migrated
        if row is None:
            return 0.0
        return float(row["anthropic_spend_usd"] or 0.0)

    def add_anthropic_spend(self, amount: float):
        """Add to today's anthropic spend (after Claude call)."""
        with self._write_lock:
            self.ensure_daily_stats_row()
            d = datetime.now(timezone.utc).date().isoformat()
            try:
                self.conn.execute(
                    "UPDATE daily_stats SET anthropic_spend_usd = COALESCE(anthropic_spend_usd, 0) + ? WHERE date = ?",
                    (amount, d),
                )
                self.conn.commit()
            except sqlite3.OperationalError as e:
                if "no such column" in str(e).lower():
                    logger.warning("anthropic_spend_usd column not yet migrated")
                else:
                    raise

    # ─── Signals ────────────────────────────────────────────────────────────

    def save_signal(self, signal: TradingSignal):
        with self._write_lock:
            regime_val = getattr(signal, "regime", None)
            model_val = getattr(signal, "model_version", None) or "claude-sonnet-4-6"
            net_score_val = signal.technical.net_score
            self.conn.execute("""
            INSERT OR REPLACE INTO signals
            (id, created_at, pair, direction, entry_price, stop_loss,
             take_profit, position_size_usdt, confidence, reasoning,
             risk_reward, status, approved_at, executed_at, raw_json, regime, model_version, net_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal.id,
                signal.created_at.isoformat(),
                signal.pair,
                signal.direction.value,
                signal.entry_price,
                signal.stop_loss,
                signal.take_profit,
                signal.position_size_usdt,
                signal.confidence,
                signal.reasoning,
                signal.risk_reward,
                signal.status.value,
                signal.approved_at.isoformat() if signal.approved_at else None,
                signal.executed_at.isoformat() if signal.executed_at else None,
                signal.model_dump_json(),
                regime_val,
                model_val,
                net_score_val,
            ))
            self.conn.commit()
            self._inc_daily_stat("total_signals")

    def update_signal_status(
        self, signal_id: str, status: SignalStatus, cancel_reason: str | None = None
    ):
        extra = {}
        if status == SignalStatus.APPROVED:
            extra["approved_at"] = datetime.now(timezone.utc).isoformat()
        elif status == SignalStatus.EXECUTED:
            extra["executed_at"] = datetime.now(timezone.utc).isoformat()
        if cancel_reason is not None:
            extra["cancel_reason"] = cancel_reason

        set_clause = "status = ?"
        params = [status.value]
        for col, val in extra.items():
            set_clause += f", {col} = ?"
            params.append(val)
        params.append(signal_id)

        with self._write_lock:
            self.conn.execute(
                f"UPDATE signals SET {set_clause} WHERE id = ?", params
            )
            self.conn.commit()
            if status == SignalStatus.APPROVED:
                self._inc_daily_stat("approved_signals")

    def _inc_daily_stat(self, column: str, amount: int = 1):
        """Increment daily_stats column for today."""
        self.ensure_daily_stats_row()
        d = datetime.now(timezone.utc).date().isoformat()
        try:
            self.conn.execute(
                f"UPDATE daily_stats SET {column} = COALESCE({column}, 0) + ? WHERE date = ?",
                (amount, d),
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # Column may not exist

    def get_pending_signals(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT raw_json FROM signals WHERE status = 'PENDING' ORDER BY created_at DESC"
        ).fetchall()
        return [json.loads(r["raw_json"]) for r in rows]

    def get_signal_by_short_id(self, short_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT raw_json FROM signals WHERE id LIKE ?", (f"{short_id}%",)
        ).fetchone()
        return json.loads(row["raw_json"]) if row else None

    def get_recent_signals(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT raw_json FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(r["raw_json"]) for r in rows]

    # ─── Trades ─────────────────────────────────────────────────────────────

    def save_trade(self, trade: Trade):
        with self._write_lock:
            sl_state = getattr(trade, "sl_trailing_state", "original")
            self.conn.execute("""
            INSERT OR REPLACE INTO trades
            (id, signal_id, pair, direction, entry_price, stop_loss,
             take_profit, quantity, position_size_usdt, binance_order_id,
             status, opened_at, closed_at, exit_price, pnl_usdt, pnl_pct, fees_usdt, is_paper, sl_trailing_state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.id, trade.signal_id, trade.pair, trade.direction.value,
            trade.entry_price, trade.stop_loss, trade.take_profit,
            trade.quantity, trade.position_size_usdt, trade.binance_order_id,
            trade.status.value, trade.opened_at.isoformat(),
            trade.closed_at.isoformat() if trade.closed_at else None,
            trade.exit_price, trade.pnl_usdt, trade.pnl_pct,
            trade.fees_usdt,
            1 if trade.is_paper else 0,
            sl_state,
        ))
            self.conn.commit()
            self._inc_daily_stat("executed_trades")

    def update_trade_sl(self, trade_id: str, new_sl: float, sl_trailing_state: str):
        """Update stop_loss và sl_trailing_state (trail stop)."""
        with self._write_lock:
            self.conn.execute(
                "UPDATE trades SET stop_loss=?, sl_trailing_state=? WHERE id=? AND status='OPEN'",
                (new_sl, sl_trailing_state, trade_id),
            )
            self.conn.commit()

    def close_trade(
        self,
        trade_id: str,
        status: str,
        closed_at: str,
        exit_price: float,
        pnl_usdt: float,
        pnl_pct: float,
        fees_usdt: float,
    ):
        """Close trade (merge status + fees in single UPDATE, with lock). Idempotent: double-close safe."""
        with self._write_lock:
            cur = self.conn.execute("""
                UPDATE trades SET status=?, closed_at=?, exit_price=?, pnl_usdt=?, pnl_pct=?, fees_usdt=?
                WHERE id=? AND status='OPEN'
            """, (status, closed_at, exit_price, pnl_usdt, pnl_pct, fees_usdt, trade_id))
            self.conn.commit()
            if cur.rowcount == 0:
                return  # Already closed (race: monitor vs circuit breaker)
            # Update daily_stats aggregates
            self.ensure_daily_stats_row()
            d = datetime.now(timezone.utc).date().isoformat()
            try:
                if pnl_usdt > 0:
                    self.conn.execute(
                        "UPDATE daily_stats SET winning_trades = COALESCE(winning_trades, 0) + 1 WHERE date = ?",
                        (d,),
                    )
                self.conn.execute(
                    "UPDATE daily_stats SET pnl_usdt = COALESCE(pnl_usdt, 0) + ? WHERE date = ?",
                    (pnl_usdt, d),
                )
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

    def get_open_trades(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_daily_pnl(self, for_date: date = None) -> float:
        d = (for_date or datetime.now(timezone.utc).date()).isoformat()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl_usdt), 0) as total FROM trades WHERE closed_at LIKE ? AND status != 'OPEN'",
            (f"{d}%",)
        ).fetchone()
        return row["total"] if row else 0.0

    def get_stats(self) -> dict:
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                SUM(COALESCE(pnl_usdt, 0)) as total_pnl,
                AVG(CASE WHEN pnl_usdt IS NOT NULL THEN pnl_pct END) as avg_pnl_pct
            FROM trades WHERE status != 'OPEN'
        """).fetchone()
        total = row["total"] or 0
        wins = row["wins"] or 0
        return {
            "total_trades": total,
            "winning_trades": wins,
            "win_rate": (wins / total * 100) if total > 0 else 0,
            "total_pnl_usdt": row["total_pnl"] or 0,
            "avg_pnl_pct": row["avg_pnl_pct"] or 0,
        }

    def get_recent_trades(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE status != 'OPEN' ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_performance(self, last_n: int = 20) -> dict:
        """Rolling performance trên N trades gần nhất. Dùng cho dynamic confluence."""
        rows = self.conn.execute(
            """
            SELECT pnl_pct, pnl_usdt
            FROM trades
            WHERE status IN ('TOOK_PROFIT', 'STOPPED')
            ORDER BY closed_at DESC LIMIT ?
            """,
            (last_n,),
        ).fetchall()
        if len(rows) < 5:
            return {"win_rate": None, "avg_rr": None, "sample_size": len(rows)}
        wins = [r for r in rows if (r["pnl_usdt"] or 0) > 0]
        losses = [r for r in rows if (r["pnl_usdt"] or 0) <= 0]
        win_rate = len(wins) / len(rows)
        avg_win = sum(r["pnl_pct"] or 0 for r in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(r["pnl_pct"] or 0 for r in losses) / len(losses)) if losses else 1
        return {
            "win_rate": win_rate,
            "avg_rr": avg_win / avg_loss if avg_loss > 0 else 0,
            "sample_size": len(rows),
        }

    # ─── Agent Logs ──────────────────────────────────────────────────────────

    def log(self, agent: str, level: str, message: str, data: dict = None):
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO agent_logs (timestamp, agent, level, message, data) VALUES (?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), agent, level, message,
                 json.dumps(data) if data else None)
            )
            self.conn.commit()

    def get_recent_logs(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT timestamp, agent, level, message, data FROM agent_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ─── Scan state (cooldown/hysteresis) ───────────────────────────────────

    def get_scan_state(self, symbol: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT symbol, last_scanned_at, last_seen_volatility, in_opportunity, updated_at FROM scan_state WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        return dict(row) if row else None

    def get_all_scan_states(self) -> dict[str, dict]:
        """Return {symbol: {last_scanned_at, last_seen_volatility, in_opportunity}}."""
        rows = self.conn.execute(
            "SELECT symbol, last_scanned_at, last_seen_volatility, in_opportunity FROM scan_state"
        ).fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def upsert_scan_state(
        self,
        symbol: str,
        last_scanned_at: Optional[str],
        last_seen_volatility: float,
        in_opportunity: bool,
    ):
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock:
            self.conn.execute(
                """
                INSERT INTO scan_state (symbol, last_scanned_at, last_seen_volatility, in_opportunity, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    last_scanned_at = excluded.last_scanned_at,
                    last_seen_volatility = excluded.last_seen_volatility,
                    in_opportunity = excluded.in_opportunity,
                    updated_at = excluded.updated_at
                """,
                (symbol, last_scanned_at, last_seen_volatility, 1 if in_opportunity else 0, now),
            )
            self.conn.commit()

    def close(self):
        self.conn.close()
