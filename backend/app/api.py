"""Dashboard API plus the browser (SmallWebRTC) call routes."""

import asyncio
import json
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from app import calls, seed, store, telephony
from app.bot import run_bot
from app.config import settings

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


def _check_callable(conn, customer_id: str):
    c = _customer_or_404(conn, customer_id)
    if c["debits_blocked"]:
        raise HTTPException(409, "Debits are blocked for this customer (fraud flag). Do not call.")
    if calls.has_active_call(conn, customer_id):
        raise HTTPException(409, "A call is already in progress for this customer.")
    return c


@router.get("/api/customers")
async def list_customers():
    rows = store.get_conn().execute(
        "SELECT cu.*, "
        "(SELECT count(*) FROM attention_items a WHERE a.customer_id=cu.id AND a.resolved=0) AS open_attention_count, "
        "(SELECT outcome FROM calls c WHERE c.customer_id=cu.id AND c.outcome IS NOT NULL "
        " ORDER BY c.id DESC LIMIT 1) AS last_outcome "
        "FROM customers cu ORDER BY CAST(substr(cu.id,2) AS INTEGER)"
    ).fetchall()
    keys = ["id", "name", "plan", "amount_due", "due_date", "failure_reason", "persona",
            "expected_outcome", "status", "open_attention_count", "last_outcome"]
    return [{**{k: r[k] for k in keys}, "debits_blocked": bool(r["debits_blocked"])} for r in rows]


@router.get("/api/numbers")
async def numbers():
    return {"default": settings.default_to_number, "numbers": _verified()}


class StartCall(BaseModel):
    customer_id: str
    to_number: str


@router.post("/api/calls")
async def start_call(body: StartCall):
    conn = store.get_conn()
    _check_callable(conn, body.customer_id)
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


@router.post("/api/browser-session")
async def browser_session(body: BrowserSession, request: Request):
    conn = store.get_conn()
    _check_callable(conn, body.customer_id)
    store.pending_browser_call = calls.create_call(conn, body.customer_id, "browser", "browser")
    return {"url": str(request.base_url).rstrip("/") + "/client/"}


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
    store.pending_browser_call = None
    return {"ok": True}


# ---- Browser call: Pipecat prebuilt client (/client) talks to /start and /sessions/{id}/api/offer ----

@router.get("/status")
async def status():
    return {"status": "ready", "transports": ["webrtc"]}


@router.post("/start")
async def start_session():
    return {"sessionId": str(uuid.uuid4())}


async def _handle_offer(req: SmallWebRTCRequest):
    async def on_connection(connection: SmallWebRTCConnection):
        call_id, store.pending_browser_call = store.pending_browser_call, None
        if call_id is None:
            logger.error("browser offer with no pending browser call; click 'Talk in browser' first")
            return
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        task = asyncio.create_task(run_bot(transport, call_id, sample_rate=16000))
        _bot_tasks.add(task)
        task.add_done_callback(_bot_tasks.discard)

    return await _webrtc.handle_web_request(req, on_connection)


@router.post("/api/offer")
async def offer(request: Request):
    data = await request.json()
    return await _handle_offer(SmallWebRTCRequest.from_dict(data))


@router.post("/sessions/{session_id}/api/offer")
async def session_offer(session_id: str, request: Request):
    data = await request.json()
    return await _handle_offer(SmallWebRTCRequest.from_dict(data))


async def _patch(request: Request):
    data = await request.json()
    await _webrtc.handle_patch_request(
        SmallWebRTCPatchRequest(
            pc_id=data["pc_id"], candidates=[IceCandidate(**c) for c in data.get("candidates", [])]
        )
    )
    return {"status": "success"}


async def _patch_session(session_id: str, request: Request):
    return await _patch(request)


router.add_api_route("/api/offer", _patch, methods=["PATCH"])
router.add_api_route("/sessions/{session_id}/api/offer", _patch_session, methods=["PATCH"])
