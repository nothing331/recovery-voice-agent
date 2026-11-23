"""Process-wide DB connection and small in-memory call/session registries."""

import secrets
import time
import uuid

from app import db, seed
from app.config import settings

_conn = None

# Calls for which Plivo machine detection reported an answering machine.
voicemail_calls: set[int] = set()
# call_id -> callback registered by a live bot, used when detection arrives mid-call.
voicemail_hooks: dict[int, object] = {}
# call_id -> random token embedded in the Plivo Stream URL (the WebSocket is not signed by Plivo).
ws_tokens: dict[int, str] = {}

BROWSER_SESSION_TTL = 300  # seconds
# Client-page token (from POST /api/browser-session) -> (call_id, expires_at). Consumed by /start.
browser_tokens: dict[str, tuple[int, float]] = {}
# WebRTC session id (from /start) -> {"call_id", "expires", "used"}. Consumed by the first offer.
browser_sessions: dict[str, dict] = {}


def _now() -> float:
    return time.monotonic()


def new_ws_token(call_id: int) -> str:
    token = ws_tokens[call_id] = secrets.token_urlsafe(24)
    return token


def check_ws_token(call_id: int | None, token: str | None) -> bool:
    expected = ws_tokens.get(call_id) if call_id is not None else None
    return bool(expected and token and secrets.compare_digest(expected, token))


def new_browser_token(call_id: int) -> str:
    _purge()
    token = secrets.token_urlsafe(24)
    browser_tokens[token] = (call_id, _now() + BROWSER_SESSION_TTL)
    return token


def _purge() -> None:
    now = _now()
    for d in (browser_tokens, browser_sessions):
        for k in [k for k, v in d.items() if (v[1] if isinstance(v, tuple) else v["expires"]) < now]:
            del d[k]


def start_browser_session(token: str | None) -> str | None:
    """Single-use: trade a client-page token for a WebRTC session id bound to the same call."""
    _purge()
    entry = browser_tokens.pop(token, None) if token else None
    if not entry:
        return None
    sid = str(uuid.uuid4())
    browser_sessions[sid] = {"call_id": entry[0], "expires": entry[1], "used": False}
    return sid


def take_browser_token(token: str | None) -> int | None:
    """Single-use: trade a talk-page token for its call id (the LiveKit path has no WebRTC session)."""
    _purge()
    entry = browser_tokens.pop(token, None) if token else None
    return entry[0] if entry else None


def get_browser_session(session_id: str) -> dict | None:
    _purge()
    return browser_sessions.get(session_id)


def clear_browser_state() -> None:
    browser_tokens.clear()
    browser_sessions.clear()


def get_conn():
    global _conn
    if _conn is None:
        c = db.connect(settings.db_path)
        db.init_schema(c)
        if c.execute("SELECT count(*) n FROM customers").fetchone()["n"] == 0:
            seed.reset(c)
        else:
            seed.backfill(c)
        _conn = c
    return _conn


def set_conn(conn) -> None:
    global _conn
    _conn = conn
