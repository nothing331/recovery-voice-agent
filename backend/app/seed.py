import json
from pathlib import Path

DATA = Path(__file__).parent / "data" / "customers.json"
COLS = ["id", "name", "phone", "plan", "amount_due", "due_date", "failure_reason", "past_attempts",
        "scenario", "scenario_tests", "persona", "balance", "instrument_status", "expected_outcome",
        "expected_flag"]
SCENARIO_COLS = [c for c in COLS if c not in ("id", "name", "phone", "plan", "amount_due", "due_date")]


def load() -> list[dict]:
    return json.loads(DATA.read_text())


def defaults() -> dict[str, dict]:
    """Seeded account state per customer, used to tell whether the dashboard edited it."""
    return {c["id"]: {"balance": c["balance"], "instrument_status": c["instrument_status"]} for c in load()}


def reset(conn) -> None:
    for t in ("attention_items", "payment_attempts", "calls", "customers"):
        conn.execute(f"DELETE FROM {t}")
    for c in load():
        conn.execute(
            f"INSERT INTO customers({','.join(COLS)},status,debits_blocked) VALUES({','.join('?' * len(COLS))},'due',0)",
            [c.get(k) for k in COLS],
        )
    conn.commit()


def backfill(conn) -> None:
    """Fill scenario and bank-state columns on a database created before they existed. Keeps call history."""
    for c in load():
        conn.execute(
            f"UPDATE customers SET {', '.join(f'{k}=?' for k in SCENARIO_COLS)} WHERE id=? AND balance IS NULL",
            [c.get(k) for k in SCENARIO_COLS] + [c["id"]],
        )
    conn.commit()
