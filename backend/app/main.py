import asyncio

from fastapi import FastAPI
from pydantic import BaseModel

from app import api, telephony

try:
    from pipecat_ai_prebuilt.frontend import PipecatPrebuiltUI
except ImportError:  # pragma: no cover
    PipecatPrebuiltUI = None

app = FastAPI(title="Autopay Recovery Voice Agent")
app.include_router(telephony.router)
app.include_router(api.router)
if PipecatPrebuiltUI is not None:
    app.mount("/client", PipecatPrebuiltUI)


@app.get("/health")
async def health():
    return {"ok": True}


class TestCall(BaseModel):
    to_number: str


@app.post("/dev/test-call")
async def dev_test_call(body: TestCall):
    uuid = await asyncio.to_thread(telephony.place_call, body.to_number, 0)
    return {"request_uuid": uuid}
