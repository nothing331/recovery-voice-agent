"""Voice pipeline. Transport-agnostic: works with Plivo (telephony) or SmallWebRTC."""

from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
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

from app.config import settings

HELLO_PROMPT = (
    f"You are {settings.agent_name} from {settings.company_name}, on a phone call. "
    "Keep replies to one short sentence. This is a connectivity test."
)


async def run_bot(transport, call_id: int | None, sample_rate: int = 8000) -> None:
    stt = DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        settings=DeepgramSTTService.Settings(model="nova-3", language="en-IN"),
    )
    llm = GoogleLLMService(
        api_key=settings.google_api_key,
        settings=GoogleLLMService.Settings(
            model=settings.gemini_model, system_instruction=HELLO_PROMPT
        ),
    )
    tts = DeepgramTTSService(
        api_key=settings.deepgram_api_key,
        encoding="linear16",
        settings=DeepgramTTSService.Settings(voice=settings.deepgram_tts_voice),
    )

    context = LLMContext()
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_agg,
            llm,
            tts,
            transport.output(),
            assistant_agg,
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=sample_rate,
            audio_out_sample_rate=sample_rate,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_connected(transport, client):
        logger.info(f"call {call_id}: client connected")
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

    await WorkerRunner(handle_sigint=False).run(worker)
