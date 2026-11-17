from urllib.parse import urlparse

from fastapi import FastAPI
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app import api, telephony

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


class TunnelGuard:
    """Requests that arrive through the public tunnel may only reach /plivo/*; the rest is 404."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] in ("http", "websocket") and _is_tunnel(scope):
            path = scope.get("path", "")
            if not (path == "/plivo" or path.startswith("/plivo/")):
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
if PipecatPrebuiltUI is not None:
    app.mount("/client", PipecatPrebuiltUI)


@app.get("/health")
async def health():
    return {"ok": True}
