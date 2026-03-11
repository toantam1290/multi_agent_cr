"""
optimization/change_registry.py — Change Registry

Ghi lại mỗi thay đổi trong improvement loop: component, reasoning, metrics before/after.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import json
import sqlite3

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "optimization_log.db"


@dataclass
class ChangeRecord:
    iteration: int
    component: str
    change_type: str
    old_value: Any
    new_value: Any
    reasoning: str
    metrics_before: Dict[str, float]
    metrics_after: Dict[str, float]
    improved: bool


class ChangeRegistry:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY,
                iteration INTEGER,
                component TEXT,
                change_type TEXT,
                old_value TEXT,
                new_value TEXT,
                reasoning TEXT,
                metrics_before TEXT,
                metrics_after TEXT,
                improved INTEGER,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def log(self, record: ChangeRecord) -> None:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """INSERT INTO changes (iteration, component, change_type, old_value, new_value,
               reasoning, metrics_before, metrics_after, improved)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.iteration,
                record.component,
                record.change_type,
                str(record.old_value),
                str(record.new_value),
                record.reasoning,
                json.dumps(record.metrics_before),
                json.dumps(record.metrics_after),
                1 if record.improved else 0,
            ),
        )
        conn.commit()
        conn.close()
