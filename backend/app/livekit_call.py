"""Browser calls through LiveKit Cloud.

Render (and similar hosts) accept no inbound UDP, so a direct WebRTC connection to the bot never
completes. With LiveKit, both the bot and the browser connect *out* to a LiveKit room, which relays
the audio. Flow: the dashboard opens /talk/?sid=<single-use token>; the page POSTs the token to
/api/livekit/join, which starts the bot in a fresh room and returns a short-lived token that lets
the browser join that room only.
"""

import asyncio
import secrets
from datetime import timedelta

from fastapi import APIRouter, HTTPException
from livekit import api as lk
from loguru import logger
from pydantic import BaseModel

from app import store
from app.bot import run_bot
from app.config import settings

router = APIRouter()

TOKEN_TTL = timedelta(minutes=15)
JOIN_TIMEOUT_SECONDS = 120  # bot leaves if the browser never joins
BOT_IDENTITY = "agent"
_tasks: set[asyncio.Task] = set()


def _token(identity: str, name: str, room: str) -> str:
    grants = lk.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True)
    return (
        lk.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(name)
        .with_grants(grants)
        .with_ttl(TOKEN_TTL)
        .to_jwt()
    )


async def _run(call_id: int, room: str) -> None:
    # Imported here so the app still starts if the livekit extra is missing and LiveKit is unused.
    from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport

    transport = LiveKitTransport(
        url=settings.livekit_url,
        token=_token(BOT_IDENTITY, settings.agent_name, room),
        room_name=room,
        params=LiveKitParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    await run_bot(transport, call_id, sample_rate=16000)


async def _watchdog(call_id: int, task: asyncio.Task) -> None:
    await asyncio.sleep(JOIN_TIMEOUT_SECONDS)
    row = store.get_conn().execute("SELECT status FROM calls WHERE id=?", (call_id,)).fetchone()
    if not task.done() and row and row["status"] == "queued":
        logger.warning(f"call {call_id}: browser never joined the LiveKit room, stopping bot")
        task.cancel()


class JoinRequest(BaseModel):
    sid: str


@router.post("/api/livekit/join")
async def join(body: JoinRequest):
    if not settings.livekit_enabled:
        raise HTTPException(404, "LiveKit is not configured")
    call_id = store.take_browser_token(body.sid)
    if call_id is None:
        raise HTTPException(403, "Unknown or expired browser session. Click 'Talk in browser' again.")
    room = f"call-{call_id}-{secrets.token_hex(4)}"
    task = asyncio.create_task(_run(call_id, room))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    watchdog = asyncio.create_task(_watchdog(call_id, task))
    _tasks.add(watchdog)
    watchdog.add_done_callback(_tasks.discard)
    logger.info(f"call {call_id}: LiveKit room {room}")
    return {"url": settings.livekit_url, "token": _token(f"customer-{call_id}", "Customer", room)}
