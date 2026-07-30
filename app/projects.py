from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from app.db import DEFAULT_DB_PATH

DEFAULT_SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "projects.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    authorized_users TEXT NOT NULL
)
"""


class Project(BaseModel):
    project_id: str
    authorized_users: list[str] = []


class ProjectRegistry:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH, seed_path: Path = DEFAULT_SEED_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(SCHEMA)
            if conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
                self._seed(conn, seed_path)

    def _seed(self, conn: sqlite3.Connection, seed_path: Path) -> None:
        if not seed_path.exists():
            return
        raw = json.loads(seed_path.read_text())
        for project_id, fields in raw.items():
            conn.execute(
                "INSERT INTO projects (project_id, authorized_users) VALUES (?, ?)",
                (project_id, json.dumps(fields.get("authorized_users", []))),
            )

    def get_project(self, project_id: str) -> Optional[Project]:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT project_id, authorized_users FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return Project(project_id=row[0], authorized_users=json.loads(row[1]))

    def list_projects(self) -> list[Project]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT project_id, authorized_users FROM projects ORDER BY project_id"
            ).fetchall()
        return [Project(project_id=row[0], authorized_users=json.loads(row[1])) for row in rows]
