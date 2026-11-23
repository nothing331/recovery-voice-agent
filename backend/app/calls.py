"""Call-row helpers shared by the API, telephony callbacks and the bot."""

import json
from datetime import datetime, timedelta, timezone

ACTIVE = ("queued", "ringing", "in_progress")
STALE_AFTER = timedelta(minutes=10)
UNANSWERED = {"busy", "timeout", "no-answer", "noanswer", "cancel", "failed", "rejected"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_call(conn, customer_id: str, to_number: str, channel: str) -> int:
    cur = conn.execute(
        "INSERT INTO calls(customer_id,to_number,channel,status,started_at) VALUES(?,?,?,?,?)",
        (customer_id, to_number, channel, "queued", now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def has_active_call(conn, customer_id: str) -> bool:
    cutoff = (datetime.now(timezone.utc) - STALE_AFTER).isoformat()
    row = conn.execute(
        f"SELECT 1 FROM calls WHERE customer_id=? AND status IN ({','.join('?' * len(ACTIVE))}) "
        "AND started_at > ? LIMIT 1",
        (customer_id, *ACTIVE, cutoff),
    ).fetchone()
    return row is not None


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def minutes_used_this_month(conn) -> float:
    """Call minutes since the start of this UTC month, counted generously: each call is at least
    one minute, and an unfinished call counts until now (capped at STALE_AFTER)."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = conn.execute(
        "SELECT started_at, ended_at, status FROM calls WHERE started_at >= ? AND status != 'failed'",
        (month_start.isoformat(),),
    ).fetchall()
    total = 0.0
    for r in rows:
        start = _parse(r["started_at"])
        if start is None:
            continue
        end = _parse(r["ended_at"]) or min(now, start + STALE_AFTER)
        total += max(1.0, (end - start).total_seconds() / 60)
    return total


def live_call_count(conn) -> int:
    """Calls actually using minutes (ringing or connected); an unopened browser link doesn't count."""
    cutoff = (datetime.now(timezone.utc) - STALE_AFTER).isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM calls WHERE status IN ('ringing','in_progress') AND started_at > ?", (cutoff,)
    ).fetchone()[0]


def derive_outcome(conn, call_id: int) -> str | None:
    """Financial outcome of a call, derived from its payment_attempts rows."""
    rows = conn.execute("SELECT kind, result FROM payment_attempts WHERE call_id=?", (call_id,)).fetchall()
    ok = {r["kind"] for r in rows if r["result"] == "success"}
    sched = any(r["kind"] == "scheduled" for r in rows)
    if "retry" in ok:
        return "paid"
    if "partial" in ok and sched:
        return "partial_paid_and_scheduled"
    if sched:
        return "scheduled"
    return None


def extract_transcript(messages: list) -> list[dict]:
    """Agent/customer turns only: drops system, tool and empty messages."""
    out = []
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else None
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        text = (content or "").strip() if isinstance(content, str) else ""
        if not text or text.startswith("[call connected]"):
            continue
        out.append({"role": "agent" if role == "assistant" else "customer", "text": text})
    return out


def finalize_call(conn, call_id: int, messages: list, latency: dict | None = None) -> None:
    """Idempotent: save transcript, default the outcome to incomplete, mark completed."""
    transcript = extract_transcript(messages)
    row = conn.execute("SELECT outcome FROM calls WHERE id=?", (call_id,)).fetchone()
    if row is not None and row["outcome"] is None:
        derived = derive_outcome(conn, call_id)
        if derived is None:
            flags = {r[0] for r in conn.execute("SELECT reason FROM attention_items WHERE call_id=?", (call_id,))}
            if flags == {"callback_requested"}:
                derived = "callback"
            else:
                derived = "escalated" if flags else "incomplete"
        conn.execute("UPDATE calls SET outcome=? WHERE id=?", (derived, call_id))
    conn.execute(
        "UPDATE calls SET transcript_json=?, status='completed', "
        "outcome=COALESCE(outcome,'incomplete'), ended_at=COALESCE(ended_at,?), "
        "latency_json=COALESCE(?,latency_json) WHERE id=?",
        (json.dumps(transcript), now_iso(), json.dumps(latency) if latency else None, call_id),
    )
    conn.commit()


def mark_unanswered(conn, call_id: int) -> None:
    conn.execute(
        "UPDATE calls SET status='completed', outcome=COALESCE(outcome,'no_answer'), "
        "ended_at=COALESCE(ended_at,?) WHERE id=? AND status!='in_progress'",
        (now_iso(), call_id),
    )
    conn.commit()
