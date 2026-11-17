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
