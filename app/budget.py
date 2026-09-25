from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from app.db import DEFAULT_DB_PATH, month_start_iso, now_iso
from app.identity import User
from app.schemas import BudgetState

SOFT_LIMIT_RATIO = 0.8

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    created_at TEXT NOT NULL,
    pending INTEGER NOT NULL DEFAULT 0
)
"""


class BudgetLedger:
    """Per-user monthly spend, tracked with reservations.

    Spend is summed over the current calendar month only, so limits reset each
    month rather than accumulating for the lifetime of the database.

    A request reserves its worst-case cost *before* the model call and settles
    the actual cost after. Without that, two concurrent requests would both read
    the same pre-spend total, both pass the limit check, and together overshoot
    the cap -- the reservation makes in-flight spend visible to other requests.
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(usage)")}
            if "pending" not in columns:
                conn.execute("ALTER TABLE usage ADD COLUMN pending INTEGER NOT NULL DEFAULT 0")

    def _spent_this_month(self, conn: sqlite3.Connection, user_id: str) -> float:
        return conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE user_id = ? AND created_at >= ?",
            (user_id, month_start_iso()),
        ).fetchone()[0]

    def check(self, user: User) -> BudgetState:
        with sqlite3.connect(self._db_path) as conn:
            spent = self._spent_this_month(conn, user.user_id)
        limit = user.monthly_budget_usd
        return BudgetState(
            spent_usd=spent,
            limit_usd=limit,
            over_soft_limit=spent >= limit * SOFT_LIMIT_RATIO,
            over_hard_limit=spent >= limit,
        )

    def reserve(self, user: User, max_cost_usd: float) -> Optional[int]:
        """Claim worst-case budget for an in-flight request.

        Returns a reservation id, or None if the user is already at their limit.
        BEGIN IMMEDIATE takes the write lock up front so concurrent reservations
        for the same user serialize against each other.
        """
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            spent = self._spent_this_month(conn, user.user_id)
            if spent >= user.monthly_budget_usd:
                conn.execute("ROLLBACK")
                return None
            cursor = conn.execute(
                "INSERT INTO usage (user_id, cost_usd, created_at, pending) VALUES (?, ?, ?, 1)",
                (user.user_id, max_cost_usd, now_iso()),
            )
            reservation_id = cursor.lastrowid
            conn.execute("COMMIT")
            return reservation_id
        finally:
            conn.close()

    def settle(self, reservation_id: int, actual_cost_usd: float) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE usage SET cost_usd = ?, pending = 0 WHERE id = ?",
                (actual_cost_usd, reservation_id),
            )

    def release(self, reservation_id: int) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM usage WHERE id = ?", (reservation_id,))

    def record(self, user: User, cost_usd: float) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO usage (user_id, cost_usd, created_at, pending) VALUES (?, ?, ?, 0)",
                (user.user_id, cost_usd, now_iso()),
            )
