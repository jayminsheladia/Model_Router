from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db import DEFAULT_DB_PATH, now_iso
from app.identity import User
from app.schemas import BudgetState

SOFT_LIMIT_RATIO = 0.8

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    created_at TEXT NOT NULL
)
"""


class BudgetLedger:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(SCHEMA)

    def check(self, user: User) -> BudgetState:
        with sqlite3.connect(self._db_path) as conn:
            spent = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE user_id = ?",
                (user.user_id,),
            ).fetchone()[0]
        limit = user.monthly_budget_usd
        return BudgetState(
            spent_usd=spent,
            limit_usd=limit,
            over_soft_limit=spent >= limit * SOFT_LIMIT_RATIO,
            over_hard_limit=spent >= limit,
        )

    def record(self, user: User, cost_usd: float) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO usage (user_id, cost_usd, created_at) VALUES (?, ?, ?)",
                (user.user_id, cost_usd, now_iso()),
            )
