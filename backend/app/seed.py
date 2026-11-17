import json
from pathlib import Path

DATA = Path(__file__).parent / "data" / "customers.json"
COLS = ["id", "name", "phone", "plan", "amount_due", "due_date", "failure_reason", "past_attempts",
        "persona", "retry_result", "partial_result", "expected_outcome"]


def reset(conn) -> None:
    for t in ("attention_items", "payment_attempts", "calls", "customers"):
        conn.execute(f"DELETE FROM {t}")
    for c in json.loads(DATA.read_text()):
        conn.execute(
            f"INSERT INTO customers({','.join(COLS)},status,debits_blocked) VALUES({','.join('?' * len(COLS))},'due',0)",
            [c[k] for k in COLS],
        )
    conn.commit()
