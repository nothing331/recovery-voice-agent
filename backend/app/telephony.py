"""Plivo outbound call placement, answer XML, hangup/machine callbacks, media WebSocket."""

from urllib.parse import urlparse

import plivo
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from plivo.utils import validate_v3_signature
from xml.sax.saxutils import escape
from loguru import logger

from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.plivo import PlivoFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from app import calls, store
from app.bot import run_bot
from app.config import settings

router = APIRouter()


def _host() -> str:
    return urlparse(settings.public_base_url).netloc or settings.public_base_url


def place_call(to_number: str, call_id: int) -> str:
    """Place an outbound Plivo call; returns the Plivo request uuid."""
    client = plivo.RestClient(settings.plivo_auth_id, settings.plivo_auth_token)
    base = settings.public_base_url.rstrip("/")
    resp = client.calls.create(
        from_=settings.plivo_from_number,
        to_=to_number,
        answer_url=f"{base}/plivo/answer?call_id={call_id}",
        answer_method="POST",
        hangup_url=f"{base}/plivo/hangup?call_id={call_id}",
        hangup_method="POST",
        machine_detection="true",
        machine_detection_url=f"{base}/plivo/machine?call_id={call_id}",
        machine_detection_method="POST",
    )
    return resp.request_uuid


def _public_url(request: Request) -> str:
    """The URL Plivo signed: PUBLIC_BASE_URL + path + raw query (not the internal request URL)."""
    url = settings.public_base_url.rstrip("/") + request.url.path
    return f"{url}?{request.url.query}" if request.url.query else url


async def verify_plivo(request: Request) -> None:
    """Dependency: reject callbacks without a valid X-Plivo-Signature-V3 (403)."""
    if not settings.plivo_verify_signatures:
        return
    sig = request.headers.get("x-plivo-signature-v3")
    nonce = request.headers.get("x-plivo-signature-v3-nonce")
    if not (sig and nonce and settings.plivo_auth_token and settings.public_base_url):
        raise HTTPException(403, "Missing Plivo signature")
    params = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        ok = validate_v3_signature(
            request.method, _public_url(request), nonce, settings.plivo_auth_token, sig, params
        )
    except Exception:
        ok = False
    if not ok:
        logger.warning(f"plivo signature rejected for {request.url.path}")
        raise HTTPException(403, "Invalid Plivo signature")


@router.post("/plivo/answer", dependencies=[Depends(verify_plivo)])
async def plivo_answer(call_id: int | None = None):
    if call_id is None:
        raise HTTPException(400, "call_id required")
    token = store.new_ws_token(call_id)
    url = escape(f"wss://{_host()}/plivo/ws?call_id={call_id}&token={token}")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        '  <Stream bidirectional="true" keepCallAlive="true" '
        'contentType="audio/x-mulaw;rate=8000">'
        f"{url}"
        "</Stream>\n"
        "</Response>"
    )
    return Response(content=xml, media_type="application/xml")


NOT_ANSWERED_CAUSES = ("no answer", "busy", "reject", "cancel", "unallocated", "timeout", "unreachable")


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def hangup_was_unanswered(form: dict) -> bool:
    """Plivo hangup payload: CallStatus, HangupCause(Name), AnswerTime, Duration, BillDuration."""
    status = str(form.get("CallStatus", "")).strip().lower()
    if status in calls.UNANSWERED:
        return True
    cause = f"{form.get('HangupCauseName', '')} {form.get('HangupCause', '')}".lower()
    if status in ("", "completed") and any(c in cause for c in NOT_ANSWERED_CAUSES):
        return True
    if status == "completed":
        answered = str(form.get("AnswerTime", "")).strip() != ""
        talked = _num(form.get("Duration")) > 0 or _num(form.get("BillDuration")) > 0
        return not (answered and talked)
    return False


def apply_hangup(conn, call_id: int, form: dict) -> str:
    """Idempotent hangup handling; returns what was done (for logs/tests)."""
    row = conn.execute("SELECT status FROM calls WHERE id=?", (call_id,)).fetchone()
    if not row:
        return "unknown_call"
    if row["status"] not in ("queued", "ringing", "in_progress"):
        return "ignored_finished"  # repeated or late delivery
    if hangup_was_unanswered(form):
        if row["status"] == "in_progress":
            return "ignored_in_progress"  # media stream is live; the bot finalizes
        calls.mark_unanswered(conn, call_id)
        return "no_answer"
    if row["status"] in ("queued", "ringing"):  # answered but the bot never took over
        calls.finalize_call(conn, call_id, [])
        return "finalized"
    return "left_to_bot"


@router.post("/plivo/hangup", dependencies=[Depends(verify_plivo)])
async def plivo_hangup(request: Request, call_id: int | None = None):
    form = dict(await request.form())
    logger.info(f"plivo hangup call_id={call_id} {form.get('CallStatus')} {form.get('HangupCauseName')}")
    if call_id:
        apply_hangup(store.get_conn(), call_id, form)
    return Response(status_code=204)


@router.post("/plivo/machine", dependencies=[Depends(verify_plivo)])
async def plivo_machine(request: Request, call_id: int | None = None):
    form = dict(await request.form())
    logger.info(f"plivo machine detection call_id={call_id} {form}")
    if call_id and str(form.get("Machine", "")).lower() == "true":
        row = store.get_conn().execute("SELECT status FROM calls WHERE id=?", (call_id,)).fetchone()
        if not row or row["status"] not in calls.ACTIVE:
            return Response(status_code=204)  # late result for a finished call
        hook = store.voicemail_hooks.get(call_id)
        if hook:  # bot already running: leave the voicemail line now
            await hook()
        else:  # detection finished before the stream connected
            store.voicemail_calls.add(call_id)
    return Response(status_code=204)


@router.websocket("/plivo/ws")
async def plivo_ws(websocket: WebSocket):
    raw = websocket.query_params.get("call_id")
    call_id = int(raw) if raw and raw.isdigit() else None
    row = (
        store.get_conn().execute("SELECT status FROM calls WHERE id=?", (call_id,)).fetchone()
        if call_id is not None else None
    )
    if not store.check_ws_token(call_id, websocket.query_params.get("token")) or not row \
            or row["status"] not in calls.ACTIVE:
        logger.warning(f"plivo ws rejected for call_id={call_id}")
        await websocket.close(code=1008)
        return
    await websocket.accept()
    transport_type, call_data = await parse_telephony_websocket(websocket)
    if transport_type != "plivo":
        logger.error(f"unexpected transport {transport_type}")
        await websocket.close()
        return
    serializer = PlivoFrameSerializer(
        stream_id=call_data["stream_id"],
        call_id=call_data["call_id"],
        auth_id=settings.plivo_auth_id,
        auth_token=settings.plivo_auth_token,
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )
    try:
        await run_bot(transport, call_id)
    finally:
        store.ws_tokens.pop(call_id, None)
