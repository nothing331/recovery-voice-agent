"""Process-wide DB connection and small in-memory call registries."""

from app import db, seed
from app.config import settings

_conn = None

# Calls for which Plivo machine detection reported an answering machine.
voicemail_calls: set[int] = set()
# call_id -> callback registered by a live bot, used when detection arrives mid-call.
voicemail_hooks: dict[int, object] = {}
# The browser call waiting for its WebRTC offer.
pending_browser_call: int | None = None


def get_conn():
    global _conn
    if _conn is None:
        c = db.connect(settings.db_path)
        db.init_schema(c)
        if c.execute("SELECT count(*) n FROM customers").fetchone()["n"] == 0:
            seed.reset(c)
        _conn = c
    return _conn


def set_conn(conn) -> None:
    global _conn
    _conn = conn
