from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db import DEFAULT_DB_PATH, now_iso

MAX_HISTORY_MESSAGES = 20

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
        """Most recent turns only.

        Every turn re-sends this history as prompt tokens, so an uncapped
        conversation makes per-turn cost grow with conversation length -- the
        window keeps a long chat from quietly becoming the most expensive thing
        the router serves.
        """
        with sqlite3.connect(self._db_path) as conn:
            owner = conn.execute(
                "SELECT user_id FROM conversation_messages "
                "WHERE conversation_id = ? ORDER BY id ASC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if owner is None:
                return []
            if owner[0] != user_id:
                raise ConversationAccessError(conversation_id)
            rows = conn.execute(
                "SELECT role, content FROM conversation_messages "
                "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (conversation_id, MAX_HISTORY_MESSAGES),
            ).fetchall()
        return [{"role": role, "content": content} for role, content in reversed(rows)]

    def append(self, conversation_id: str, user_id: str, role: str, content: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO conversation_messages (conversation_id, user_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversation_id, user_id, role, content, now_iso()),
            )
