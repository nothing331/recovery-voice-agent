from datetime import date, timedelta

import pytest

from app import db, seed
from app.tools import TOOL_SCHEMAS, CallSession

TODAY = date(2026, 11, 18)


def iso(n):
    return (TODAY + timedelta(days=n)).isoformat()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    seed.reset(c)
    return c


def make(conn, cid):
    conn.execute(
        "INSERT INTO calls(customer_id,to_number,channel,status) VALUES(?,?,?,?)",
        (cid, "+910000000000", "phone", "in_progress"),
    )
    conn.commit()
    call_id = conn.execute("SELECT max(id) m FROM calls").fetchone()["m"]
    return CallSession(conn, call_id, cid, TODAY)


def status(conn, cid):
    return conn.execute("SELECT status FROM customers WHERE id=?", (cid,)).fetchone()["status"]


def test_seed_ten(conn):
    assert conn.execute("SELECT count(*) c FROM customers").fetchone()["c"] == 10


def test_locked_before_identity(conn):
    s = make(conn, "c1")
    for r in (s.retry_debit(), s.schedule_debit(iso(1)), s.take_partial_payment(100, iso(1))):
        assert r == {"error": "identity_not_confirmed", "message": r["message"]}


def test_confirm_returns_details_then_deny_locks(conn):
    s = make(conn, "c1")
    r = s.confirm_identity(True)
    assert {"plan", "amount_due", "due_date", "failure_reason_plain"} <= set(r)
    s.confirm_identity(False)
    assert s.retry_debit()["error"] == "identity_not_confirmed"
    assert conn.execute("SELECT identity_result FROM calls WHERE id=?", (s.call_id,)).fetchone()[0] == "denied"


def test_retry_success_and_once(conn):
    s = make(conn, "c1")
    s.confirm_identity(True)
    assert s.retry_debit()["success"] is True
    assert status(conn, "c1") == "paid"
    assert s.retry_debit()["error"] == "already_retried"


def test_retry_failure(conn):
    s = make(conn, "c2")
    s.confirm_identity(True)
    r = s.retry_debit()
    assert r["success"] is False and r["reason"] and r["message"]
    assert status(conn, "c2") == "due"


def test_schedule(conn):
    s = make(conn, "c2")
    s.confirm_identity(True)
    assert s.schedule_debit(iso(3))["success"] is True
    assert status(conn, "c2") == "scheduled"
    assert conn.execute("SELECT count(*) FROM payment_attempts WHERE kind='scheduled'").fetchone()[0] == 1


def test_schedule_out_of_range(conn):
    s = make(conn, "c2")
    s.confirm_identity(True)
    r = s.schedule_debit(iso(6))
    assert r["error"] == "date_out_of_range" and r["max_date"] == iso(5) and "extension_over_5_days" in r["message"]
    assert s.schedule_debit(iso(-1))["error"] == "date_out_of_range"
    assert s.schedule_debit("soon")["error"] == "invalid_date"
    assert status(conn, "c2") == "due"


def test_schedule_bad_amount(conn):
    s = make(conn, "c2")
    s.confirm_identity(True)
    assert s.schedule_debit(iso(1), 0)["error"] == "invalid_amount"


def test_partial_errors(conn):
    s = make(conn, "c3")
    due = s.confirm_identity(True)["amount_due"]
    assert s.take_partial_payment(0, iso(2))["error"] == "invalid_amount"
    assert s.take_partial_payment(-5, iso(2))["error"] == "invalid_amount"
    assert s.take_partial_payment(due, iso(2))["error"] == "invalid_amount"
    assert s.take_partial_payment(100, iso(7))["error"] == "date_out_of_range"
    assert status(conn, "c3") == "due"


def test_partial_ok(conn):
    s = make(conn, "c3")
    due = s.confirm_identity(True)["amount_due"]
    r = s.take_partial_payment(due // 2, iso(2))
    assert r["success"] is True
    assert status(conn, "c3") == "partial_scheduled"
    assert conn.execute("SELECT count(*) FROM payment_attempts").fetchone()[0] == 2


def test_fraud_blocks(conn):
    s = make(conn, "c7")
    s.confirm_identity(True)
    assert s.flag_for_human("fraud", "never signed up")["success"] is True
    assert status(conn, "c7") == "blocked"
    assert s.retry_debit()["error"] == "debits_blocked"
    assert conn.execute("SELECT count(*) FROM attention_items").fetchone()[0] == 1


def test_flag_escalates_but_keeps_scheduled(conn):
    s = make(conn, "c2")
    s.confirm_identity(True)
    s.schedule_debit(iso(2))
    s.flag_for_human("dispute", "x")
    assert status(conn, "c2") == "scheduled"
    s2 = make(conn, "c6")
    s2.flag_for_human("extension_over_5_days", "needs 10 days")
    assert status(conn, "c6") == "escalated"


def test_flag_identity_denied_without_confirmation(conn):
    s = make(conn, "c10")
    assert s.flag_for_human("identity_denied", "spouse answered")["success"] is True


def test_flag_validation(conn):
    s = make(conn, "c1")
    assert s.flag_for_human("fraud", "  ")["error"] == "note_required"
    assert s.flag_for_human("bogus", "n")["error"] == "invalid_reason"


def test_end_call(conn):
    s = make(conn, "c1")
    assert s.end_call("nope", "s")["error"] == "invalid_outcome"
    s.confirm_identity(True)
    s.retry_debit()
    assert s.end_call("paid", "done")["success"] is True
    row = conn.execute("SELECT outcome, summary, ended_at FROM calls WHERE id=?", (s.call_id,)).fetchone()
    assert row["outcome"] == "paid" and row["ended_at"]


def test_schemas():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {"confirm_identity", "retry_debit", "schedule_debit", "take_partial_payment", "flag_for_human", "end_call"}
    assert all(t["parameters"]["type"] == "object" for t in TOOL_SCHEMAS)


# --- resolution gate, atomicity, derived outcome ---
import pytest as _pt
from app import calls as _calls


def attempts(conn, cid=None):
    return conn.execute("SELECT kind,amount,result FROM payment_attempts").fetchall()


def test_paid_case_cannot_be_charged_or_scheduled_again(conn):
    s = make(conn, "c1")
    s.confirm_identity(True)
    assert s.retry_debit()["success"] is True
    s2 = make(conn, "c1")
    s2.confirm_identity(True)
    for r in (s2.retry_debit(), s2.schedule_debit(iso(1)), s2.take_partial_payment(100, iso(1))):
        assert r["error"] == "already_resolved" and r["message"]
    assert len(attempts(conn)) == 1


def test_second_call_cannot_schedule_again(conn):
    s = make(conn, "c2")
    s.confirm_identity(True)
    assert s.schedule_debit(iso(2))["success"] is True
    s2 = make(conn, "c2")
    s2.confirm_identity(True)
    assert s2.schedule_debit(iso(3))["error"] == "already_resolved"
    assert s2.retry_debit()["error"] == "already_resolved"
    assert len(attempts(conn)) == 1


def test_partial_schedule_blocks_further_schedule_and_amount_capped(conn):
    s = make(conn, "c2")
    s.confirm_identity(True)
    assert s.schedule_debit(iso(2), 500)["success"] is True
    s2 = make(conn, "c2")
    s2.confirm_identity(True)
    assert s2.schedule_debit(iso(2), 500)["error"] == "already_resolved"


def test_schedule_amount_capped_to_remaining(conn):
    s = make(conn, "c3")
    s.confirm_identity(True)
    assert s.take_partial_payment(1000, iso(2))["success"] is True
    # fully covered now
    assert s.schedule_debit(iso(1))["error"] == "already_resolved"
    assert s.retry_debit()["error"] == "already_resolved"


def test_schedule_over_remaining_rejected(conn):
    s = make(conn, "c3")
    s.confirm_identity(True)
    # simulate a prior successful partial charge with no schedule yet
    conn.execute("INSERT INTO payment_attempts(customer_id,call_id,kind,amount,result) VALUES('c3',?, 'partial',1000,'success')", (s.call_id,))
    conn.commit()
    r = s.schedule_debit(iso(1), 1500)
    assert r["error"] == "invalid_amount"
    ok = s.schedule_debit(iso(1))
    assert ok["success"] is True and ok["amount"] == 999


def test_partial_atomic_rollback_when_schedule_fails(conn):
    s = make(conn, "c3")
    s.confirm_identity(True)

    def boom(*a, **k):
        raise RuntimeError("db down")
    s.payments.schedule = boom
    r = s.take_partial_payment(500, iso(2))
    assert "error" in r and r["error"] != "invalid_amount"
    assert attempts(conn) == []
    assert status(conn, "c3") == "due"
    assert conn.execute("SELECT promise_amount FROM calls WHERE id=?", (s.call_id,)).fetchone()[0] is None


def test_schedule_atomic_rollback(conn):
    s = make(conn, "c2")
    s.confirm_identity(True)
    s._promise = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    r = s.schedule_debit(iso(2))
    assert "error" in r
    assert attempts(conn) == [] and status(conn, "c2") == "due"


def test_end_call_outcome_derived_from_ledger(conn):
    s = make(conn, "c3")
    s.confirm_identity(True)
    s.take_partial_payment(500, iso(2))
    assert s.end_call("escalated", "x")["success"] is True
    row = conn.execute("SELECT outcome, disposition FROM calls WHERE id=?", (s.call_id,)).fetchone()
    assert row["outcome"] == "partial_paid_and_scheduled" and row["disposition"] == "escalated"


def test_end_call_mismatch(conn):
    s = make(conn, "c3")
    s.confirm_identity(True)
    r = s.end_call("paid", "x")
    assert r["error"] == "outcome_mismatch" and r["message"].startswith("Ledger shows")
    assert conn.execute("SELECT outcome FROM calls WHERE id=?", (s.call_id,)).fetchone()[0] is None
    s.schedule_debit(iso(1))
    r = s.end_call("paid", "x")
    assert r["error"] == "outcome_mismatch" and "scheduled" in r["message"]
    assert s.end_call("scheduled", "x")["success"] is True


def test_end_call_nonfinancial_stored(conn):
    s = make(conn, "c4")
    assert s.end_call("voicemail", "x")["success"] is True
    row = conn.execute("SELECT outcome, disposition FROM calls WHERE id=?", (s.call_id,)).fetchone()
    assert row["outcome"] == "voicemail" and row["disposition"] == "voicemail"


def test_finalize_derives_from_ledger_on_hangup(conn):
    s = make(conn, "c1")
    s.confirm_identity(True)
    s.retry_debit()
    _calls.finalize_call(conn, s.call_id, [])
    assert conn.execute("SELECT outcome FROM calls WHERE id=?", (s.call_id,)).fetchone()[0] == "paid"


def test_finalize_escalated_or_incomplete(conn):
    s = make(conn, "c6")
    s.flag_for_human("wants_human", "n")
    _calls.finalize_call(conn, s.call_id, [])
    assert conn.execute("SELECT outcome FROM calls WHERE id=?", (s.call_id,)).fetchone()[0] == "escalated"
    s2 = make(conn, "c5")
    _calls.finalize_call(conn, s2.call_id, [])
    assert conn.execute("SELECT outcome FROM calls WHERE id=?", (s2.call_id,)).fetchone()[0] == "incomplete"


def test_disposition_migration():
    import sqlite3
    c = db.connect(":memory:")
    c.executescript("CREATE TABLE calls(id INTEGER PRIMARY KEY, outcome TEXT, latency_json TEXT);")
    db.init_schema(c)
    assert "disposition" in {r[1] for r in c.execute("PRAGMA table_info(calls)")}
