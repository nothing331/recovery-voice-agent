import json

import pytest
from fastapi.testclient import TestClient

from app import api, calls, db, seed, store, telephony
from app.config import settings
from app.main import app

NUM = "+919000000001"


@pytest.fixture
def client(monkeypatch):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    seed.reset(conn)
    store.set_conn(conn)
    monkeypatch.setattr(settings, "verified_numbers", [NUM])
    monkeypatch.setattr(settings, "default_to_number", NUM)
    monkeypatch.setattr(settings, "plivo_verify_signatures", False)
    placed = []
    monkeypatch.setattr(telephony, "place_call", lambda to, cid: placed.append((to, cid)) or "uuid-1")
    c = TestClient(app)
    c.placed, c.conn = placed, conn
    yield c
    store.set_conn(None)


def call(client, cid="c1", num=NUM):
    return client.post("/api/calls", json={"customer_id": cid, "to_number": num})


def test_customers_and_numbers(client):
    cs = client.get("/api/customers").json()
    assert len(cs) == 10 and cs[0]["id"] == "c1" and cs[0]["status"] == "due"
    assert cs[0]["open_attention_count"] == 0 and cs[0]["last_outcome"] is None
    assert client.get("/api/numbers").json() == {"default": NUM, "numbers": [NUM]}


def test_start_call_ok(client):
    r = call(client)
    assert r.status_code == 200
    assert client.placed == [(NUM, r.json()["call_id"])]
    d = client.get(f"/api/calls/{r.json()['call_id']}").json()
    assert d["status"] == "ringing" and d["channel"] == "phone" and d["transcript"] == []


def test_blocked_customer_409(client):
    client.conn.execute("UPDATE customers SET debits_blocked=1, status='blocked' WHERE id='c1'")
    client.conn.commit()
    assert call(client).status_code == 409
    assert client.placed == []


def test_second_call_while_in_progress_409(client):
    assert call(client).status_code == 200
    client.conn.execute("UPDATE calls SET status='in_progress'")
    client.conn.commit()
    assert call(client).status_code == 409
    assert len(client.placed) == 1


def test_unverified_number_400(client):
    assert call(client, num="+911111111111").status_code == 400
    assert client.placed == []


def test_reset_restores_due(client):
    client.conn.execute("UPDATE customers SET status='paid'")
    client.conn.commit()
    assert client.post("/api/demo/reset").json() == {"ok": True}
    assert {c["status"] for c in client.get("/api/customers").json()} == {"due"}


def test_resolve_attention(client):
    client.conn.execute(
        "INSERT INTO attention_items(customer_id,call_id,reason,notes,created_at,resolved) "
        "VALUES('c2',NULL,'dispute','n','t',0)"
    )
    client.conn.commit()
    items = client.get("/api/attention?resolved=false").json()
    assert len(items) == 1 and items[0]["resolved"] is False and items[0]["customer_name"]
    assert client.post(f"/api/attention/{items[0]['id']}/resolve").json() == {"ok": True}
    assert client.conn.execute("SELECT resolved FROM attention_items").fetchone()["resolved"] == 1
    assert client.get("/api/attention?resolved=false").json() == []


def test_browser_session_url(client):
    r = client.post("/api/browser-session", json={"customer_id": "c1"})
    assert r.status_code == 200 and "/client/?sid=" in r.json()["url"]
    row = client.conn.execute("SELECT channel FROM calls").fetchone()
    assert row["channel"] == "browser" and store.browser_tokens


def test_hangup_unanswered_sets_no_answer(client):
    cid = call(client).json()["call_id"]
    client.post(f"/plivo/hangup?call_id={cid}", data={"CallStatus": "no-answer"})
    d = client.get(f"/api/calls/{cid}").json()
    assert d["outcome"] == "no_answer" and d["status"] == "completed"


def test_hangup_does_not_override_outcome(client):
    cid = call(client).json()["call_id"]
    client.conn.execute("UPDATE calls SET outcome='paid' WHERE id=?", (cid,))
    client.conn.commit()
    client.post(f"/plivo/hangup?call_id={cid}", data={"CallStatus": "busy"})
    assert client.get(f"/api/calls/{cid}").json()["outcome"] == "paid"


def test_machine_marks_voicemail(client):
    store.voicemail_calls.clear()
    cid = calls.create_call(client.conn, "c1", NUM, "phone")
    client.post(f"/plivo/machine?call_id={cid}", data={"Machine": "true"})
    assert cid in store.voicemail_calls
    store.voicemail_calls.clear()


def test_finalize_saves_transcript_and_incomplete(client):
    cid = call(client).json()["call_id"]
    msgs = [
        {"role": "user", "content": "[call connected] go"},
        {"role": "assistant", "content": "Hello, am I speaking with Rohan Mehta?"},
        {"role": "user", "content": [{"type": "text", "text": "Yes speaking"}]},
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "{}"},
        {"role": "system", "content": "x"},
    ]
    calls.finalize_call(client.conn, cid, msgs)
    d = client.get(f"/api/calls/{cid}").json()
    assert d["outcome"] == "incomplete" and d["status"] == "completed"
    assert d["transcript"] == [
        {"role": "agent", "text": "Hello, am I speaking with Rohan Mehta?"},
        {"role": "customer", "text": "Yes speaking"},
    ]


def _dnc(client, resolved=0):
    client.conn.execute(
        "INSERT INTO attention_items(customer_id, call_id, reason, notes, created_at, resolved) "
        "VALUES('c1', NULL, 'do_not_call_request', 'stop', 'now', ?)", (resolved,))
    client.conn.commit()


def test_customers_open_attention_reasons(client):
    _dnc(client)
    cs = {c["id"]: c for c in client.get("/api/customers").json()}
    assert cs["c1"]["open_attention_reasons"] == ["do_not_call_request"]
    assert cs["c2"]["open_attention_reasons"] == []


def test_dnc_blocks_without_override(client):
    _dnc(client)
    for r in (call(client), client.post("/api/browser-session", json={"customer_id": "c1"})):
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "do_not_call_request_open"
    assert client.placed == []


def test_dnc_allowed_with_override(client):
    _dnc(client)
    r = client.post("/api/calls", json={"customer_id": "c1", "to_number": NUM, "override_do_not_call": True})
    assert r.status_code == 200
    client.conn.execute("UPDATE calls SET status='completed'")
    client.conn.commit()
    r = client.post("/api/browser-session", json={"customer_id": "c1", "override_do_not_call": True})
    assert r.status_code == 200


def test_resolved_dnc_does_not_block(client):
    _dnc(client, resolved=1)
    assert call(client).status_code == 200


def test_customers_scenario_fields(client):
    c = client.get("/api/customers").json()[1]
    assert c["scenario"] and c["persona"] and c["expected_outcome"] == "scheduled"
    assert c["balance"] == c["default_balance"] == 300 and c["instrument_status"] == "active"
    assert c["account_edited"] is False and c["result"] is None


def test_account_top_up_and_card_status(client):
    r = client.post("/api/customers/c2/account", json={"top_up": 1000})
    assert r.status_code == 200 and r.json()["balance"] == 1300
    r = client.post("/api/customers/c2/account", json={"instrument_status": "expired"})
    assert r.json() == {"balance": 1300, "instrument_status": "expired"}
    c = client.get("/api/customers").json()[1]
    assert c["account_edited"] is True
    assert client.post("/api/customers/c2/account", json={"balance": 300, "instrument_status": "active"}).status_code == 200
    assert client.get("/api/customers").json()[1]["account_edited"] is False


def test_account_validation(client):
    assert client.post("/api/customers/c2/account", json={"instrument_status": "stolen"}).status_code == 400
    assert client.post("/api/customers/c2/account", json={"balance": -1}).status_code == 400
    assert client.post("/api/customers/c2/account", json={"top_up": 0}).status_code == 400
    assert client.post("/api/customers/zz/account", json={"top_up": 5}).status_code == 404


def _finished_call(client, cid, outcome, flag=None):
    cur = client.conn.execute("INSERT INTO calls(customer_id,status,outcome) VALUES(?,'completed',?)", (cid, outcome))
    if flag:
        client.conn.execute("INSERT INTO attention_items(customer_id,call_id,reason,notes,created_at,resolved) "
                            "VALUES(?,?,?,'n','t',0)", (cid, cur.lastrowid, flag))
    client.conn.commit()


def test_scenario_pass_fail(client):
    _finished_call(client, "c1", "paid")
    _finished_call(client, "c4", "escalated", "dispute")  # expected needs_card_update
    _finished_call(client, "c7", "escalated", "fraud")
    cs = {c["id"]: c for c in client.get("/api/customers").json()}
    assert cs["c1"]["result"] == "pass"
    assert cs["c4"]["result"] == "fail" and cs["c4"]["last_call_flags"] == ["dispute"]
    assert cs["c7"]["result"] == "pass"
    client.post("/api/customers/c1/account", json={"top_up": 1})
    assert client.get("/api/customers").json()[0]["result"] is None


def test_payment_lowering_balance_is_not_an_edit(client):
    client.conn.execute("UPDATE customers SET balance=balance-999 WHERE id='c1'")
    _finished_call(client, "c1", "paid")
    c = client.get("/api/customers").json()[0]
    assert c["account_edited"] is False and c["result"] == "pass"
