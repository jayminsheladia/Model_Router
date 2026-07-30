from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "router.db"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
