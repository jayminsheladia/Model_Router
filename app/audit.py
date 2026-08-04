from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from app.db import DEFAULT_DB_PATH, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    user_id TEXT NOT NULL,
    project TEXT,
    prompt_snippet TEXT NOT NULL,
    classifier_json TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    final_tier TEXT,
    reason TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    latency_ms REAL NOT NULL,
    budget_json TEXT NOT NULL,
    conversation_id TEXT
)
"""

COLUMNS = [
    "id", "timestamp", "user_id", "project", "prompt_snippet", "classifier_json",
    "allowed", "final_tier", "reason", "cost_usd", "latency_ms", "budget_json",
    "conversation_id",
]


class AuditLogger:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(SCHEMA)

    def record(self, entry: dict) -> int:
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO audit_log (timestamp, user_id, project, prompt_snippet, "
                "classifier_json, allowed, final_tier, reason, cost_usd, latency_ms, budget_json, "
                "conversation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now_iso(),
                    entry["user_id"],
                    entry.get("project"),
                    entry["prompt_snippet"],
                    json.dumps(entry["classifier"]),
                    int(entry["allowed"]),
                    entry["final_tier"],
                    entry["reason"],
                    entry["cost_usd"],
                    entry["latency_ms"],
                    json.dumps(entry["budget"]),
                    entry.get("conversation_id"),
                ),
            )
            return cursor.lastrowid

    def get(self, audit_id: int) -> Optional[dict]:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM audit_log WHERE id = ?", (audit_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def read_recent(self, n: int = 20) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM audit_log ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row: tuple) -> dict:
        record = dict(zip(COLUMNS, row))
        record["classifier"] = json.loads(record.pop("classifier_json"))
        record["budget"] = json.loads(record.pop("budget_json"))
        record["allowed"] = bool(record["allowed"])
        return record
