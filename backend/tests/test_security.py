import pytest
from fastapi.testclient import TestClient

from app import db, seed, store, telephony
from app.config import settings
from app.main import app

NUM = "+919000000001"
PUBLIC = "https://abc.trycloudflare.com"
TOKEN = "auth-token-xyz"


@pytest.fixture
def client(monkeypatch):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    seed.reset(conn)
    store.set_conn(conn)
    store.clear_browser_state()
    store.ws_tokens.clear()
    monkeypatch.setattr(settings, "verified_numbers", [NUM])
    monkeypatch.setattr(settings, "public_base_url", PUBLIC)
    monkeypatch.setattr(settings, "plivo_auth_token", TOKEN)
    monkeypatch.setattr(settings, "plivo_verify_signatures", True)
    c = TestClient(app)
    c.conn = conn
    yield c
    store.set_conn(None)


def sign(path_query, params, nonce="n123"):
    from plivo.utils.signature_v3 import construct_post_url, get_signature_v3

    url = construct_post_url(PUBLIC + path_query, dict(params)).decode()
    sig = get_signature_v3(TOKEN.encode(), url, nonce).decode()
    return {"X-Plivo-Signature-V3": sig, "X-Plivo-Signature-V3-Nonce": nonce}


# ---- tunnel guard ----

def test_tunnel_only_reaches_plivo(client):
    for hdr in ({"cf-connecting-ip": "1.2.3.4"}, {"cf-ray": "abc"}, {"host": "abc.trycloudflare.com"}):
        assert client.get("/api/customers", headers=hdr).status_code == 404
        assert client.post("/api/demo/reset", headers=hdr).status_code == 404
        assert client.get("/health", headers=hdr).status_code == 404
        assert client.post("/dev/test-call", json={"to_number": "+1"}, headers=hdr).status_code == 404
    assert client.get("/api/customers").status_code == 200
    assert client.get("/api/customers", headers={"host": "localhost:8000"}).status_code == 200


def test_tunnel_allows_plivo_paths(client):
    r = client.post("/plivo/hangup?call_id=1", headers={"cf-ray": "x"}, data={"CallStatus": "completed"})
    assert r.status_code == 403  # reached the handler (signature check), not the 404 guard


def test_dashboard_password_guards_everything_but_plivo(client, monkeypatch):
    import base64

    monkeypatch.setattr(settings, "dashboard_password", "s3cret")

    def basic(pw):
        return {"authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}

    # Any host, including a spoofed localhost, needs the password.
    for hdr in ({}, {"host": "localhost:8000"}, {"host": "x.hf.space"}, basic("wrong"), {"authorization": "Basic !!"}):
        r = client.get("/api/customers", headers=hdr)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    ok = {"host": "x.hf.space", **basic("s3cret")}
    assert client.get("/api/customers", headers=ok).status_code == 200
    assert client.get("/health", headers={"host": "x.hf.space"}).status_code == 200  # host health checks
    # Plivo stays reachable without the password (its own signature check applies).
    r = client.post("/plivo/hangup?call_id=1", headers={"host": "x.hf.space"}, data={"CallStatus": "completed"})
    assert r.status_code == 403


def test_public_base_url_falls_back_to_render_url(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert Settings(_env_file=None).public_base_url == "https://x.onrender.com"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tunnel.example")
    assert Settings(_env_file=None).public_base_url == "https://tunnel.example"


def test_dev_test_call_removed(client):
    # 405 when the built dashboard is mounted at / (StaticFiles is GET-only); either way, no endpoint.
    assert client.post("/dev/test-call", json={"to_number": "+1"}).status_code in (404, 405)


# ---- signatures ----

def test_answer_rejects_missing_and_bad_signature(client):
    assert client.post("/plivo/answer?call_id=1").status_code == 403
    h = sign("/plivo/answer?call_id=1", {})
    h["X-Plivo-Signature-V3"] = "bogus"
    assert client.post("/plivo/answer?call_id=1", headers=h).status_code == 403


def test_answer_valid_signature_returns_tokened_stream(client):
    r = client.post("/plivo/answer?call_id=7", headers=sign("/plivo/answer?call_id=7", {}))
    assert r.status_code == 200
    assert f"/plivo/ws?call_id=7&amp;token={store.ws_tokens[7]}" in r.text


def test_hangup_signature_covers_form_params(client):
    cid = _call(client, "ringing")
    form = {"CallStatus": "busy", "CallUUID": "u1"}
    assert client.post(f"/plivo/hangup?call_id={cid}", data={**form, "CallStatus": "completed"},
                       headers=sign(f"/plivo/hangup?call_id={cid}", form)).status_code == 403
    assert client.post(f"/plivo/hangup?call_id={cid}", data=form,
                       headers=sign(f"/plivo/hangup?call_id={cid}", form)).status_code == 204
    assert _row(client, cid)["outcome"] == "no_answer"


def test_signature_flag_disables_check(client, monkeypatch):
    monkeypatch.setattr(settings, "plivo_verify_signatures", False)
    assert client.post("/plivo/answer?call_id=3").status_code == 200


# ---- websocket token ----

def test_ws_rejects_bad_token_and_inactive_call(client):
    from starlette.websockets import WebSocketDisconnect

    cid = _call(client, "ringing")
    for q in (f"call_id={cid}", f"call_id={cid}&token=nope", "token=x"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/plivo/ws?{q}"):
                pass
    token = store.new_ws_token(cid)
    client.conn.execute("UPDATE calls SET status='completed' WHERE id=?", (cid,))
    client.conn.commit()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/plivo/ws?call_id={cid}&token={token}"):
            pass


# ---- browser sessions ----

def _offer(client, sid, **extra):
    return client.post(f"/sessions/{sid}/api/offer", json={"sdp": "x", "type": "offer", **extra})


def _start(client, url):
    sid_token = url.split("sid=")[1]
    return client.post("/start", headers={"referer": f"http://localhost:8000/client/?sid={sid_token}"})


def test_two_tabs_get_their_own_case(client):
    u1 = client.post("/api/browser-session", json={"customer_id": "c1"}).json()["url"]
    u2 = client.post("/api/browser-session", json={"customer_id": "c2"}).json()["url"]
    s2 = _start(client, u2).json()["sessionId"]
    s1 = _start(client, u1).json()["sessionId"]
    assert store.browser_sessions[s1]["call_id"] != store.browser_sessions[s2]["call_id"]
    rows = {r["id"]: r["customer_id"] for r in client.conn.execute("SELECT id, customer_id FROM calls")}
    assert rows[store.browser_sessions[s1]["call_id"]] == "c1"
    assert rows[store.browser_sessions[s2]["call_id"]] == "c2"


def test_start_requires_valid_single_use_token(client):
    assert client.post("/start").status_code == 403
    assert client.post("/start", headers={"referer": "http://localhost:8000/client/?sid=bad"}).status_code == 403
    u = client.post("/api/browser-session", json={"customer_id": "c1"}).json()["url"]
    assert _start(client, u).status_code == 200
    assert _start(client, u).status_code == 403


def test_offer_unknown_expired_and_reused_sessions(client, monkeypatch):
    assert _offer(client, "nope").status_code == 404
    u = client.post("/api/browser-session", json={"customer_id": "c1"}).json()["url"]
    sid = _start(client, u).json()["sessionId"]
    monkeypatch.setattr(store, "_now", lambda: 10**9)  # past the 5 minute expiry
    assert _offer(client, sid).status_code == 404
    monkeypatch.undo()
    u = client.post("/api/browser-session", json={"customer_id": "c2"}).json()["url"]
    sid = _start(client, u).json()["sessionId"]
    store.browser_sessions[sid]["used"] = True
    assert _offer(client, sid).status_code == 409
    assert client.post("/api/offer", json={}).status_code in (404, 405)


def test_token_expires(client, monkeypatch):
    u = client.post("/api/browser-session", json={"customer_id": "c1"}).json()["url"]
    t = store._now()
    monkeypatch.setattr(store, "_now", lambda: t + store.BROWSER_SESSION_TTL + 1)
    assert _start(client, u).status_code == 403


# ---- hangup / machine idempotency ----

def _call(client, status="ringing"):
    cid = client.conn.execute(
        "INSERT INTO calls(customer_id,to_number,channel,status,started_at) VALUES('c1',?,'phone',?,'t')",
        (NUM, status),
    ).lastrowid
    client.conn.commit()
    return cid


def _row(client, cid):
    return client.conn.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()


@pytest.fixture
def nosig(client, monkeypatch):
    monkeypatch.setattr(settings, "plivo_verify_signatures", False)
    return client


@pytest.mark.parametrize("payload", [
    {"CallStatus": "busy", "HangupCauseName": "Busy Line"},
    {"CallStatus": "no-answer", "HangupCause": "NO_ANSWER"},
    {"CallStatus": "timeout"},
    {"CallStatus": "cancel", "HangupCauseName": "Originator Cancel"},
    {"CallStatus": "failed"},
    {"CallStatus": "completed", "AnswerTime": "", "Duration": "0", "BillDuration": "0"},
    {"CallStatus": "completed", "HangupCauseName": "No Answer", "Duration": "30"},
])
def test_unanswered_payloads_are_no_answer(nosig, payload):
    cid = _call(nosig)
    assert nosig.post(f"/plivo/hangup?call_id={cid}", data=payload).status_code == 204
    r = _row(nosig, cid)
    assert r["outcome"] == "no_answer" and r["status"] == "completed"


def test_answered_completed_is_not_no_answer(nosig):
    cid = _call(nosig, "in_progress")
    nosig.post(f"/plivo/hangup?call_id={cid}", data={
        "CallStatus": "completed", "HangupCauseName": "Normal Hangup",
        "AnswerTime": "2026-11-18 10:00:00", "Duration": "42", "BillDuration": "42"})
    r = _row(nosig, cid)
    assert r["outcome"] is None and r["status"] == "in_progress"  # the bot finalizes


def test_answered_but_bot_never_connected_is_incomplete(nosig):
    cid = _call(nosig, "ringing")
    nosig.post(f"/plivo/hangup?call_id={cid}", data={
        "CallStatus": "completed", "AnswerTime": "2026-11-18 10:00:00", "Duration": "5"})
    r = _row(nosig, cid)
    assert r["status"] == "completed" and r["outcome"] == "incomplete"


def test_repeated_and_late_hangups_do_not_change_finished_call(nosig):
    cid = _call(nosig)
    nosig.post(f"/plivo/hangup?call_id={cid}", data={"CallStatus": "busy"})
    nosig.post(f"/plivo/hangup?call_id={cid}", data={"CallStatus": "busy"})
    nosig.conn.execute("UPDATE calls SET outcome='promise_made', summary='kept' WHERE id=?", (cid,))
    nosig.conn.commit()
    nosig.post(f"/plivo/hangup?call_id={cid}", data={"CallStatus": "no-answer"})
    nosig.post(f"/plivo/hangup?call_id={cid}", data={"CallStatus": "completed", "Duration": "0"})
    r = _row(nosig, cid)
    assert r["outcome"] == "promise_made" and r["summary"] == "kept"


def test_late_unanswered_hangup_ignored_while_in_progress(nosig):
    cid = _call(nosig, "in_progress")
    nosig.post(f"/plivo/hangup?call_id={cid}", data={"CallStatus": "completed", "Duration": "0"})
    assert _row(nosig, cid)["status"] == "in_progress"


def test_late_machine_result_does_not_touch_finished_call(nosig):
    cid = _call(nosig, "completed")
    nosig.conn.execute("UPDATE calls SET outcome='promise_made' WHERE id=?", (cid,))
    nosig.conn.commit()
    called = []

    async def hook():
        called.append(1)

    store.voicemail_hooks[cid] = hook
    assert nosig.post(f"/plivo/machine?call_id={cid}", data={"Machine": "true"}).status_code == 204
    assert not called and cid not in store.voicemail_calls
    assert _row(nosig, cid)["outcome"] == "promise_made"
    store.voicemail_hooks.pop(cid)


def test_machine_result_queues_or_hooks_for_active_call(nosig):
    cid = _call(nosig, "ringing")
    nosig.post(f"/plivo/machine?call_id={cid}", data={"Machine": "true"})
    assert cid in store.voicemail_calls
    store.voicemail_calls.discard(cid)
    nosig.post(f"/plivo/machine?call_id={cid}", data={"Machine": "false"})
    assert cid not in store.voicemail_calls


# ---- ICE / TURN servers for browser calls ----

def test_start_returns_ice_config_from_static_servers(client, monkeypatch):
    monkeypatch.setattr(settings, "ice_servers",
                        '[{"urls": ["turn:t.example:3478?transport=udp", "turn:t.example:53"], '
                        '"username": "u", "credential": "p"}]')
    u = client.post("/api/browser-session", json={"customer_id": "c1"}).json()["url"]
    body = _start(client, u).json()
    assert body["iceConfig"]["iceServers"] == [
        {"urls": ["turn:t.example:3478?transport=udp"], "username": "u", "credential": "p"}]


def test_start_without_ice_servers_has_no_ice_config(client):
    u = client.post("/api/browser-session", json={"customer_id": "c1"}).json()["url"]
    assert "iceConfig" not in _start(client, u).json()


def test_cloudflare_turn_credentials_are_cached(monkeypatch):
    import asyncio

    import httpx

    from app import ice

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(201, json={"iceServers": [
            {"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"]},
            {"urls": ["turn:turn.cloudflare.com:3478?transport=udp"], "username": "cu", "credential": "cp"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(ice.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(settings, "cloudflare_turn_key_id", "kid")
    monkeypatch.setattr(settings, "cloudflare_turn_api_token", "tok")
    monkeypatch.setitem(ice._cf_cache, "servers", None)
    first = asyncio.run(ice.ice_servers())
    second = asyncio.run(ice.ice_servers())
    assert first == second and len(calls) == 1
    assert calls[0].headers["authorization"] == "Bearer tok" and "/keys/kid/" in str(calls[0].url)
    assert first[0] == {"urls": ["stun:stun.cloudflare.com:3478"]}
    assert first[1]["username"] == "cu"


# ---- LiveKit browser calls ----

@pytest.fixture
def livekit(client, monkeypatch):
    from app import livekit_call

    started = []

    async def fake_run(call_id, room):
        started.append((call_id, room))

    monkeypatch.setattr(livekit_call, "_run", fake_run)
    monkeypatch.setattr(settings, "livekit_url", "wss://demo.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_api_key", "APIkey")
    monkeypatch.setattr(settings, "livekit_api_secret", "s" * 32)
    client.started = started
    return client


def test_browser_session_uses_talk_page_when_livekit_configured(livekit):
    url = livekit.post("/api/browser-session", json={"customer_id": "c1"}).json()["url"]
    assert "/talk/?sid=" in url
    assert livekit.get("/talk/").status_code == 200


def test_livekit_join_is_single_use_and_scoped_to_one_room(livekit):
    import jwt

    url = livekit.post("/api/browser-session", json={"customer_id": "c1"}).json()["url"]
    sid = url.split("sid=")[1]
    r = livekit.post("/api/livekit/join", json={"sid": sid})
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == "wss://demo.livekit.cloud"
    claims = jwt.decode(body["token"], "s" * 32, algorithms=["HS256"])
    room = claims["video"]["room"]
    assert claims["video"]["roomJoin"] is True and room.startswith("call-")
    assert claims["sub"] != "agent"
    assert livekit.post("/api/livekit/join", json={"sid": sid}).status_code == 403
    assert livekit.post("/api/livekit/join", json={"sid": "bogus"}).status_code == 403


def test_livekit_join_404_when_not_configured(client):
    url = client.post("/api/browser-session", json={"customer_id": "c1"}).json()["url"]
    assert "/client/?sid=" in url
    assert client.post("/api/livekit/join", json={"sid": "x"}).status_code == 404
