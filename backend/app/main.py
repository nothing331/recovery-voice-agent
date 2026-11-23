import base64
import secrets
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from starlette.responses import PlainTextResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from app import api, livekit_call, telephony
from app.config import settings

try:
    from pipecat_ai_prebuilt.frontend import PipecatPrebuiltUI
except ImportError:  # pragma: no cover
    PipecatPrebuiltUI = None

# "testserver" is Starlette's TestClient host; real tunnel traffic is also caught by Cloudflare headers.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}
TUNNEL_HEADERS = (b"cf-connecting-ip", b"cf-ray")


def _is_tunnel(scope: Scope) -> bool:
    headers = dict(scope.get("headers") or [])
    if any(h in headers for h in TUNNEL_HEADERS):
        return True
    host = headers.get(b"host", b"").decode("latin-1").strip().lower()
    hostname = urlparse(f"//{host}").hostname or ""
    return hostname not in LOCAL_HOSTS


def _is_plivo(scope: Scope) -> bool:
    path = scope.get("path", "")
    return path == "/plivo" or path.startswith("/plivo/")


def _has_password(scope: Scope) -> bool:
    auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
    scheme, _, encoded = auth.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        _, _, password = base64.b64decode(encoded).decode("utf-8").partition(":")
    except ValueError:
        return False
    return secrets.compare_digest(password.encode(), settings.dashboard_password.encode())


class TunnelGuard:
    """Without DASHBOARD_PASSWORD, requests arriving through the public tunnel may only reach /plivo/* (else 404).
    With it, every non-Plivo request needs HTTP Basic auth with that password."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] in ("http", "websocket") and not _is_plivo(scope):
            if settings.dashboard_password:
                # Hosted mode: the Host header can't be trusted, so every non-Plivo request needs the password
                # (except the host's health check, which returns nothing sensitive).
                if scope.get("path") != "/health" and not _has_password(scope):
                    if scope["type"] == "websocket":
                        await receive()
                        await send({"type": "websocket.close", "code": 1008})
                    else:
                        await PlainTextResponse(
                            "Authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="dashboard"'},
                        )(scope, receive, send)
                    return
            elif _is_tunnel(scope):
                if scope["type"] == "websocket":
                    await receive()
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app = FastAPI(title="Autopay Recovery Voice Agent")
app.add_middleware(TunnelGuard)
app.include_router(telephony.router)
app.include_router(api.router)
app.include_router(livekit_call.router)
if PipecatPrebuiltUI is not None:
    app.mount("/client", PipecatPrebuiltUI)


@app.get("/health")
async def health():
    return {"ok": True}


# "Talk in browser" page for the LiveKit path.
app.mount("/talk", StaticFiles(directory=Path(__file__).parent / "static" / "talk", html=True), name="talk")

# The built dashboard (frontend/dist), served last so API and Plivo routes win.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="dashboard")
