from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db import DEFAULT_DB_PATH, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id INTEGER NOT NULL,
    complexity_bucket TEXT NOT NULL,
    signal TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


class FeedbackStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(SCHEMA)

    def record(self, audit_id: int, complexity_bucket: str, signal: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO feedback (audit_id, complexity_bucket, signal, created_at) "
                "VALUES (?, ?, ?, ?)",
                (audit_id, complexity_bucket, signal, now_iso()),
            )

    def get_bias(self, complexity_bucket: str) -> tuple[int, int]:
        with sqlite3.connect(self._db_path) as conn:
            escalate = conn.execute(
                "SELECT COUNT(*) FROM feedback WHERE complexity_bucket = ? AND signal = 'escalate'",
                (complexity_bucket,),
            ).fetchone()[0]
            downgrade_ok = conn.execute(
                "SELECT COUNT(*) FROM feedback WHERE complexity_bucket = ? AND signal = 'downgrade_ok'",
                (complexity_bucket,),
            ).fetchone()[0]
        return escalate, downgrade_ok
