from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db import DEFAULT_DB_PATH, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


class ConversationAccessError(Exception):
    def __init__(self, conversation_id: str):
        self.conversation_id = conversation_id
        super().__init__(f"Conversation '{conversation_id}' does not belong to this user")


class ConversationStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(SCHEMA)

    def get_history(self, conversation_id: str, user_id: str) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT role, content, user_id FROM conversation_messages "
                "WHERE conversation_id = ? ORDER BY id ASC",
                (conversation_id,),
            ).fetchall()
        if not rows:
            return []
        if rows[0][2] != user_id:
            raise ConversationAccessError(conversation_id)
        return [{"role": role, "content": content} for role, content, _owner in rows]

    def append(self, conversation_id: str, user_id: str, role: str, content: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO conversation_messages (conversation_id, user_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversation_id, user_id, role, content, now_iso()),
            )
