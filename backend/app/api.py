"""Dashboard API plus the browser (SmallWebRTC) call routes."""

import asyncio
import json
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from app import calls, ice, seed, store, telephony
from app.bot import run_bot
from app.config import settings
from app.payments import INSTRUMENT_STATUSES

router = APIRouter()
_bot_tasks: set[asyncio.Task] = set()
_webrtc = SmallWebRTCRequestHandler()


def _verified() -> list[str]:
    nums = list(settings.verified_numbers)
    if settings.default_to_number and settings.default_to_number not in nums:
        nums.insert(0, settings.default_to_number)
    return nums


def _customer_or_404(conn, customer_id: str):
    c = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not c:
        raise HTTPException(404, "Unknown customer")
    return c


def _check_free_limits(conn) -> None:
    """Keep usage inside free tiers: one call at a time and a monthly minutes cap."""
    if calls.live_call_count(conn) >= settings.max_concurrent_calls:
        raise HTTPException(409, "Another call is in progress. Wait for it to end.")
    used = calls.minutes_used_this_month(conn)
    if used >= settings.monthly_call_minutes_cap:
        logger.warning(f"monthly call-minute cap reached: {used:.0f}/{settings.monthly_call_minutes_cap}")
        raise HTTPException(429, f"Free-tier guard: this month's call limit "
                                 f"({settings.monthly_call_minutes_cap} min) is used up.")


def _check_callable(conn, customer_id: str, override_do_not_call: bool = False):
    c = _customer_or_404(conn, customer_id)
    if c["debits_blocked"]:
        raise HTTPException(409, "Debits are blocked for this customer (fraud flag). Do not call.")
    if calls.has_active_call(conn, customer_id):
        raise HTTPException(409, "A call is already in progress for this customer.")
    _check_free_limits(conn)
    dnc_open = conn.execute(
        "SELECT 1 FROM attention_items WHERE customer_id=? AND reason='do_not_call_request' AND resolved=0 LIMIT 1",
        (customer_id,)).fetchone()
    if dnc_open:
        if not override_do_not_call:
            raise HTTPException(409, {"code": "do_not_call_request_open",
                                      "message": "This customer asked not to be called. Confirm to call anyway."})
        logger.info("do_not_call override used for customer {}", customer_id)
    return c


def _scenario_result(conn, r, edited: bool) -> tuple[str | None, list[str]]:
    """Compare the customer's latest finished call with the scenario's expected outcome and flag.
    Returns (result, flags on that call); result is None when not run yet or the account was edited."""
    last = conn.execute(
        "SELECT id, outcome FROM calls WHERE customer_id=? AND outcome IS NOT NULL ORDER BY id DESC LIMIT 1",
        (r["id"],)).fetchone()
    if not last:
        return None, []
    flags = [f[0] for f in conn.execute(
        "SELECT DISTINCT reason FROM attention_items WHERE call_id=? ORDER BY reason", (last["id"],))]
    if edited or not r["expected_outcome"]:
        return None, flags
    ok = last["outcome"] == r["expected_outcome"] and (not r["expected_flag"] or r["expected_flag"] in flags)
    return ("pass" if ok else "fail"), flags


@router.get("/api/customers")
async def list_customers():
    conn = store.get_conn()
    rows = conn.execute(
        "SELECT cu.*, "
        "(SELECT count(*) FROM attention_items a WHERE a.customer_id=cu.id AND a.resolved=0) AS open_attention_count, "
        "(SELECT group_concat(DISTINCT a.reason) FROM attention_items a "
        " WHERE a.customer_id=cu.id AND a.resolved=0) AS open_reasons, "
        "(SELECT outcome FROM calls c WHERE c.customer_id=cu.id AND c.outcome IS NOT NULL "
        " ORDER BY c.id DESC LIMIT 1) AS last_outcome "
        "FROM customers cu ORDER BY CAST(substr(cu.id,2) AS INTEGER)"
    ).fetchall()
    keys = ["id", "name", "plan", "amount_due", "due_date", "failure_reason", "scenario", "scenario_tests",
            "persona", "expected_outcome", "expected_flag", "balance", "instrument_status", "status",
            "open_attention_count", "last_outcome"]
    defaults = seed.defaults()
    out = []
    for r in rows:
        d = defaults.get(r["id"], {})
        edited = bool(r["account_edited"])  # set by the dashboard only; payments lowering the balance don't count
        result, flags = _scenario_result(conn, r, edited)
        out.append({**{k: r[k] for k in keys}, "debits_blocked": bool(r["debits_blocked"]),
                    "open_attention_reasons": (r["open_reasons"] or "").split(",") if r["open_reasons"] else [],
                    "default_balance": d.get("balance"), "default_instrument_status": d.get("instrument_status"),
                    "account_edited": edited, "last_call_flags": flags, "result": result})
    return out


MAX_BALANCE = 10_000_000


class AccountUpdate(BaseModel):
    balance: int | None = None
    top_up: int | None = None
    instrument_status: str | None = None


@router.post("/api/customers/{customer_id}/account")
async def update_account(customer_id: str, body: AccountUpdate):
    """Edit the simulated bank behind a customer: top up or set the balance, change the card status.
    The agent never sees these values; they only decide whether a charge succeeds."""
    conn = store.get_conn()
    c = _customer_or_404(conn, customer_id)
    balance = c["balance"] or 0
    if body.balance is not None:
        balance = body.balance
    if body.top_up is not None:
        if body.top_up <= 0:
            raise HTTPException(400, "Top-up must be a positive amount.")
        balance += body.top_up
    if not 0 <= balance <= MAX_BALANCE:
        raise HTTPException(400, f"Balance must be between 0 and {MAX_BALANCE}.")
    instrument = body.instrument_status or c["instrument_status"]
    if instrument not in INSTRUMENT_STATUSES:
        raise HTTPException(400, f"Card status must be one of: {', '.join(INSTRUMENT_STATUSES)}.")
    d = seed.defaults().get(customer_id, {})
    edited = (balance, instrument) != (d.get("balance"), d.get("instrument_status"))
    conn.execute("UPDATE customers SET balance=?, instrument_status=?, account_edited=? WHERE id=?",
                 (balance, instrument, int(edited), customer_id))
    conn.commit()
    logger.info("account {} edited: balance={} instrument={}", customer_id, balance, instrument)
    return {"balance": balance, "instrument_status": instrument}


@router.get("/api/numbers")
async def numbers():
    return {"default": settings.default_to_number, "numbers": _verified()}


class StartCall(BaseModel):
    customer_id: str
    to_number: str
    override_do_not_call: bool = False


@router.post("/api/calls")
async def start_call(body: StartCall):
    conn = store.get_conn()
    _check_callable(conn, body.customer_id, body.override_do_not_call)
    if body.to_number not in _verified():
        raise HTTPException(400, "That number is not verified for calls.")
    call_id = calls.create_call(conn, body.customer_id, body.to_number, "phone")
    try:
        uuid_ = await asyncio.to_thread(telephony.place_call, body.to_number, call_id)
    except Exception as e:
        logger.exception("place_call failed")
        conn.execute("UPDATE calls SET status='failed', ended_at=? WHERE id=?", (calls.now_iso(), call_id))
        conn.commit()
        raise HTTPException(502, f"Could not place the call: {e}")
    conn.execute("UPDATE calls SET status='ringing', plivo_uuid=? WHERE id=? AND status='queued'",
                 (uuid_, call_id))
    conn.commit()
    return {"call_id": call_id}


class BrowserSession(BaseModel):
    customer_id: str
    override_do_not_call: bool = False


@router.post("/api/browser-session")
async def browser_session(body: BrowserSession, request: Request):
    conn = store.get_conn()
    _check_callable(conn, body.customer_id, body.override_do_not_call)
    call_id = calls.create_call(conn, body.customer_id, "browser", "browser")
    token = store.new_browser_token(call_id)
    page = "/talk/" if settings.livekit_enabled else "/client/"  # LiveKit room vs direct WebRTC
    return {"url": str(request.base_url).rstrip("/") + f"{page}?sid={token}"}


_CALL_COLS = ("c.id, c.customer_id, cu.name AS customer_name, c.to_number, c.channel, c.status, "
              "c.outcome, c.summary, c.started_at, c.ended_at, c.latency_json")


@router.get("/api/calls")
async def list_calls():
    rows = store.get_conn().execute(
        f"SELECT {_CALL_COLS} FROM calls c JOIN customers cu ON cu.id=c.customer_id ORDER BY c.id DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        lat = _latency(d.pop("latency_json"))
        d["avg_latency_ms"] = lat["response_ms"]["avg"] if lat and lat["response_ms"]["count"] else None
        out.append(d)
    return out


def _latency(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _attention(r) -> dict:
    d = dict(r)
    d["resolved"] = bool(d["resolved"])
    return d


@router.get("/api/calls/{call_id}")
async def get_call(call_id: int):
    conn = store.get_conn()
    r = conn.execute(
        f"SELECT {_CALL_COLS}, c.identity_result, c.promise_amount, c.promise_date, c.transcript_json "
        "FROM calls c JOIN customers cu ON cu.id=c.customer_id WHERE c.id=?",
        (call_id,),
    ).fetchone()
    if not r:
        raise HTTPException(404, "Unknown call")
    d = dict(r)
    d["transcript"] = json.loads(d.pop("transcript_json") or "[]")
    d["latency"] = _latency(d.pop("latency_json"))
    d["payment_attempts"] = [
        dict(a) for a in conn.execute("SELECT * FROM payment_attempts WHERE call_id=? ORDER BY id", (call_id,))
    ]
    d["attention_items"] = [
        _attention(a)
        for a in conn.execute(
            "SELECT a.*, cu.name AS customer_name FROM attention_items a "
            "JOIN customers cu ON cu.id=a.customer_id WHERE a.call_id=? ORDER BY a.id",
            (call_id,),
        )
    ]
    return d


@router.get("/api/attention")
async def list_attention(resolved: bool = False):
    rows = store.get_conn().execute(
        "SELECT a.id, a.customer_id, cu.name AS customer_name, a.call_id, a.reason, a.notes, "
        "a.created_at, a.resolved FROM attention_items a JOIN customers cu ON cu.id=a.customer_id "
        "WHERE a.resolved=? ORDER BY a.id DESC",
        (1 if resolved else 0,),
    ).fetchall()
    return [_attention(r) for r in rows]


@router.post("/api/attention/{item_id}/resolve")
async def resolve_attention(item_id: int):
    conn = store.get_conn()
    cur = conn.execute("UPDATE attention_items SET resolved=1, resolved_at=? WHERE id=?",
                       (calls.now_iso(), item_id))
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "Unknown attention item")
    return {"ok": True}


@router.post("/api/demo/reset")
async def demo_reset():
    seed.reset(store.get_conn())
    store.voicemail_calls.clear()
    store.clear_browser_state()
    return {"ok": True}


# ---- Browser call: Pipecat prebuilt client (/client) talks to /start and /sessions/{id}/api/offer ----

@router.get("/status")
async def status():
    return {"status": "ready", "transports": ["webrtc"]}


@router.post("/start")
async def start_session(request: Request):
    # The prebuilt client POSTs a fixed "/start"; the per-tab token rides on the page URL
    # (/client/?sid=...), which the browser sends as the Referer of that same-origin fetch.
    ref = request.headers.get("referer", "")
    token = (parse_qs(urlparse(ref).query).get("sid") or [None])[0]
    session_id = store.start_browser_session(token)
    if session_id is None:
        raise HTTPException(403, "Unknown or expired browser session. Click 'Talk in browser' again.")
    result = {"sessionId": session_id}
    # Both peers must use the same TURN relay: the bot's handler gets them, and so does the client.
    servers = await ice.ice_servers()
    if servers:
        _webrtc.update_ice_servers([IceServer(**s) for s in servers])
        result["iceConfig"] = {"iceServers": servers}
    return result


async def _handle_offer(session_id: str, req: SmallWebRTCRequest):
    sess = store.get_browser_session(session_id)
    if sess is None:
        raise HTTPException(404, "Unknown or expired browser session")
    if not req.pc_id:  # a new connection; renegotiation carries a pc_id
        if sess["used"]:
            raise HTTPException(409, "Browser session already used")
        sess["used"] = True
    call_id = sess["call_id"]

    async def on_connection(connection: SmallWebRTCConnection):
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        task = asyncio.create_task(run_bot(transport, call_id, sample_rate=16000))
        _bot_tasks.add(task)
        task.add_done_callback(_bot_tasks.discard)

    return await _webrtc.handle_web_request(req, on_connection)


@router.post("/sessions/{session_id}/api/offer")
async def session_offer(session_id: str, request: Request):
    data = await request.json()
    return await _handle_offer(session_id, SmallWebRTCRequest.from_dict(data))


async def _patch(request: Request):
    data = await request.json()
    await _webrtc.handle_patch_request(
        SmallWebRTCPatchRequest(
            pc_id=data["pc_id"], candidates=[IceCandidate(**c) for c in data.get("candidates", [])]
        )
    )
    return {"status": "success"}


async def _patch_session(session_id: str, request: Request):
    if store.get_browser_session(session_id) is None:
        raise HTTPException(404, "Unknown or expired browser session")
    return await _patch(request)


router.add_api_route("/sessions/{session_id}/api/offer", _patch_session, methods=["PATCH"])
