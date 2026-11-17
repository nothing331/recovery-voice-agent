"""Plivo outbound call placement, answer XML, hangup/machine callbacks, media WebSocket."""

from urllib.parse import urlparse

import plivo
from fastapi import APIRouter, Form, Request, Response, WebSocket
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


@router.post("/plivo/answer")
async def plivo_answer(call_id: int | None = None):
    # call_id rides on the WebSocket URL query string; the WS handler reads it.
    qs = f"?call_id={call_id}" if call_id is not None else ""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        '  <Stream bidirectional="true" keepCallAlive="true" '
        'contentType="audio/x-mulaw;rate=8000">'
        f"wss://{_host()}/plivo/ws{qs}"
        "</Stream>\n"
        "</Response>"
    )
    return Response(content=xml, media_type="application/xml")


@router.post("/plivo/hangup")
async def plivo_hangup(request: Request, call_id: int | None = None):
    form = dict(await request.form())
    status = str(form.get("CallStatus", "")).lower()
    logger.info(f"plivo hangup call_id={call_id} {status} {form.get('HangupCauseName')}")
    if call_id and status in calls.UNANSWERED:
        calls.mark_unanswered(store.get_conn(), call_id)
    return Response(status_code=204)


@router.post("/plivo/machine")
async def plivo_machine(request: Request, call_id: int | None = None):
    form = dict(await request.form())
    logger.info(f"plivo machine detection call_id={call_id} {form}")
    if call_id and str(form.get("Machine", "")).lower() == "true":
        hook = store.voicemail_hooks.get(call_id)
        if hook:  # bot already running: leave the voicemail line now
            await hook()
        else:  # detection finished before the stream connected
            store.voicemail_calls.add(call_id)
    return Response(status_code=204)


@router.websocket("/plivo/ws")
async def plivo_ws(websocket: WebSocket):
    await websocket.accept()
    raw = websocket.query_params.get("call_id")
    call_id = int(raw) if raw and raw.isdigit() else None
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
    await run_bot(transport, call_id)
