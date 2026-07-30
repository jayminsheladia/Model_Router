from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from app.db import DEFAULT_DB_PATH
from app.schemas import Tier

DEFAULT_SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "users.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    team TEXT NOT NULL,
    monthly_budget_usd REAL NOT NULL,
    max_tier TEXT NOT NULL,
    allowed_projects TEXT NOT NULL,
    project_tier_overrides TEXT NOT NULL
)
"""


class User(BaseModel):
    user_id: str
    team: str
    monthly_budget_usd: float
    max_tier: Tier
    allowed_projects: list[str] = []
    project_tier_overrides: dict[str, Tier] = {}


class IdentityRegistry:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH, seed_path: Path = DEFAULT_SEED_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(SCHEMA)
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                self._seed(conn, seed_path)

    def _seed(self, conn: sqlite3.Connection, seed_path: Path) -> None:
        raw = json.loads(seed_path.read_text())
        for user_id, fields in raw.items():
            conn.execute(
                "INSERT INTO users (user_id, team, monthly_budget_usd, max_tier, "
                "allowed_projects, project_tier_overrides) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    fields["team"],
                    fields["monthly_budget_usd"],
                    fields["max_tier"],
                    json.dumps(fields.get("allowed_projects", [])),
                    json.dumps(fields.get("project_tier_overrides", {})),
                ),
            )

    def get_user(self, user_id: str) -> Optional[User]:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT user_id, team, monthly_budget_usd, max_tier, "
                "allowed_projects, project_tier_overrides FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    def list_users(self) -> list[User]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT user_id, team, monthly_budget_usd, max_tier, "
                "allowed_projects, project_tier_overrides FROM users ORDER BY user_id"
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    @staticmethod
    def _row_to_user(row: tuple) -> User:
        return User(
            user_id=row[0],
            team=row[1],
            monthly_budget_usd=row[2],
            max_tier=row[3],
            allowed_projects=json.loads(row[4]),
            project_tier_overrides=json.loads(row[5]),
        )
