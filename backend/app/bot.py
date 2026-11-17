"""Voice pipeline. Transport-agnostic: works with Plivo (telephony) or SmallWebRTC."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    EndWorkerFrame,
    FunctionCallResultProperties,
    LLMRunFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.workers.runner import WorkerRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openrouter.llm import OpenRouterLLMService

from app import calls, store
from app.config import settings
from app.prompt import build_system_prompt
from app.tools import TOOL_SCHEMAS, CallSession

MAX_CALL_SECONDS = 8 * 60
GOODBYE = "Thank you for your time. Take care, goodbye."

HELLO_PROMPT = (
    f"You are {settings.agent_name} from {settings.company_name}, on a phone call. "
    "Keep replies to one short sentence. This is a connectivity test."
)



def _build_llm(system_prompt: str):
    """LLM factory; both services take the prompt via system_instruction and use the same tool handlers."""
    if settings.llm_provider == "google":
        return GoogleLLMService(
            api_key=settings.google_api_key,
            settings=GoogleLLMService.Settings(model=settings.gemini_model, system_instruction=system_prompt),
        )
    return OpenRouterLLMService(
        api_key=settings.openrouter_api_key,
        settings=OpenRouterLLMService.Settings(model=settings.openrouter_model, system_instruction=system_prompt),
    )


def _tools_schema() -> ToolsSchema:
    return ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name=t["name"],
                description=t["description"],
                properties=t["parameters"].get("properties", {}),
                required=t["parameters"].get("required", []),
            )
            for t in TOOL_SCHEMAS
        ]
    )


def _clean_args(args: dict) -> dict:
    """Gemini sends integers as floats sometimes (500.0); the rules want ints."""
    return {k: int(v) if isinstance(v, float) and v.is_integer() else v for k, v in args.items()}


def _voicemail_text() -> str:
    return (
        f"Hello, this is {settings.agent_name} from {settings.company_name}. "
        "Sorry we missed you. We will try you again soon. Goodbye."
    )


async def run_bot(transport, call_id: int | None, sample_rate: int = 8000) -> None:
    conn = store.get_conn()
    row = None
    if call_id:
        row = conn.execute(
            "SELECT c.id, c.customer_id, cu.name FROM calls c JOIN customers cu ON cu.id=c.customer_id "
            "WHERE c.id=?",
            (call_id,),
        ).fetchone()
    session = None
    if row:
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        session = CallSession(conn, row["id"], row["customer_id"], today)
        system_prompt = build_system_prompt(row["name"], settings.company_name, settings.agent_name, today)
    else:
        logger.warning(f"call {call_id}: no call row, running connectivity test")
        system_prompt = HELLO_PROMPT

    stt = DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        settings=DeepgramSTTService.Settings(model="nova-3", language="en-IN"),
    )
    llm = _build_llm(system_prompt)
    tts = DeepgramTTSService(
        api_key=settings.deepgram_api_key,
        encoding="linear16",
        settings=DeepgramTTSService.Settings(voice=settings.deepgram_tts_voice),
    )

    context = LLMContext(tools=_tools_schema()) if session else LLMContext()
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [transport.input(), stt, user_agg, llm, tts, transport.output(), assistant_agg]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=sample_rate, audio_out_sample_rate=sample_rate),
    )

    if session:

        def make_handler(name: str):
            async def handler(params):
                args = _clean_args(dict(params.arguments or {}))
                logger.info(f"call {call_id}: tool {name} args={args}")
                try:
                    result = getattr(session, name)(**args)
                except TypeError as e:
                    result = {"error": "bad_arguments", "message": str(e)}
                except Exception as e:  # never let a tool crash the call
                    logger.exception(f"call {call_id}: tool {name} failed")
                    result = {"error": "tool_failed", "message": str(e)}
                logger.info(f"call {call_id}: tool {name} -> {result}")
                if name == "end_call" and result.get("success"):
                    # Goodbye is spoken by us, then EndWorkerFrame flows behind it so the
                    # pipeline ends only after the audio has played (Plivo then hangs up).
                    await params.result_callback(
                        result, properties=FunctionCallResultProperties(run_llm=False)
                    )
                    await params.llm.push_frame(TTSSpeakFrame(GOODBYE))
                    await params.llm.push_frame(EndWorkerFrame(reason="end_call"))
                else:
                    await params.result_callback(result)

            return handler

        for t in TOOL_SCHEMAS:
            llm.register_function(t["name"], make_handler(t["name"]))

    async def leave_voicemail():
        logger.info(f"call {call_id}: answering machine, leaving voicemail")
        session.end_call("voicemail", "Answering machine detected; left a short message with no details.")
        await worker.queue_frames([TTSSpeakFrame(_voicemail_text()), EndWorkerFrame(reason="voicemail")])

    async def safety_timeout():
        await asyncio.sleep(MAX_CALL_SECONDS)
        logger.warning(f"call {call_id}: max duration reached, ending")
        await worker.cancel(reason="max call duration")

    timeout_task: asyncio.Task | None = None

    @transport.event_handler("on_client_connected")
    async def on_connected(transport, client):
        nonlocal timeout_task
        logger.info(f"call {call_id}: client connected")
        if session:
            conn.execute(
                "UPDATE calls SET status='in_progress', started_at=? WHERE id=?", (calls.now_iso(), call_id)
            )
            conn.commit()
            timeout_task = asyncio.create_task(safety_timeout())
            if call_id in store.voicemail_calls:
                await leave_voicemail()
                return
            store.voicemail_hooks[call_id] = leave_voicemail
            context.add_message({"role": "user", "content": "[call connected] The person has picked up. Begin."})
        else:
            context.add_message(
                {
                    "role": "user",
                    "content": f"Say exactly: Hi, this is {settings.agent_name} from "
                    f"{settings.company_name}. Can you hear me clearly?",
                }
            )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, client):
        logger.info(f"call {call_id}: client disconnected")
        await worker.cancel()

    try:
        await WorkerRunner(handle_sigint=False).run(worker)
    finally:
        if timeout_task:
            timeout_task.cancel()
        if session:
            store.voicemail_hooks.pop(call_id, None)
            store.voicemail_calls.discard(call_id)
            try:
                calls.finalize_call(conn, call_id, context.get_messages())
            except Exception:
                logger.exception(f"call {call_id}: failed to finalize")
        logger.info(f"call {call_id}: finished")
