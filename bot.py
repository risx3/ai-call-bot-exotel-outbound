import os
import asyncio
from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.exotel import ExotelFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

from prompts import base_system_prompt

# -----------------------------------------------------------------------------
# ENV
# -----------------------------------------------------------------------------
load_dotenv(override=True)

# -----------------------------------------------------------------------------
# GLOBAL SERVICE CACHE
# -----------------------------------------------------------------------------
_cached_services = None


def _initialize_cached_services():
    global _cached_services

    if _cached_services is not None:
        return _cached_services

    logger.info("🚀 Initializing AI services (once)")

    _cached_services = {
        "stt": OpenAISTTService(api_key=os.getenv("OPENAI_API_KEY")),
        "llm": OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY")),
        "tts": ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            voice_id=os.getenv("ELEVENLABS_VOICE_ID"),
        ),
    }

    logger.info("✅ AI services cached")
    return _cached_services


_cached_services = _initialize_cached_services()

# -----------------------------------------------------------------------------
# BOT ENTRYPOINT
# -----------------------------------------------------------------------------
async def bot(runner_args: RunnerArguments):
    transport_type, call_data = await parse_telephony_websocket(
        runner_args.websocket
    )

    logger.info(f"🔌 Transport detected: {transport_type}")

    serializer = ExotelFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data["call_id"],
    )

    transport = FastAPIWebsocketTransport(
        websocket=runner_args.websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=SileroVADAnalyzer(),
            serializer=serializer,
        ),
    )

    services = _cached_services
    stt = services["stt"]
    llm = services["llm"]
    tts = services["tts"]

    greeting_text = "नमस्ते! मैं Priya बोल रही हूँ OS Games से। क्या अभी बात करना convenient है?"
    greeting_given = False
    
    messages = [
        {
            "role": "system",
            "content": base_system_prompt,
        }
    ]

    context = LLMContext(messages)
    context_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    # -------------------------------------------------------------------------
    # GREETING (AFTER USER SPEAKS 🔊)
    # -------------------------------------------------------------------------
    @task.event_handler("on_pipeline_started")
    async def on_pipeline_started(task, event):
        """Wait for user input, then play greeting."""
        nonlocal greeting_given
        
        logger.info("✅ Pipeline started — waiting for user to speak")
        
        # Give the pipeline time to receive first user input
        await asyncio.sleep(2)
        
        if not greeting_given:
            logger.info("✅ Now speaking greeting after user input")
            logger.info(f"🎤 Generating greeting: {greeting_text}")
            
            try:
                # Generate audio from greeting text (tts.run_tts returns an async generator)
                async for frame in tts.run_tts(text=greeting_text):
                    logger.info("✅ Greeting audio frame generated, pushing to transport")
                    # Push each audio frame to the transport output
                    await transport.output().push_frame(frame)
                
                # Add greeting to conversation context so LLM knows bot already greeted
                context.messages.append({
                    "role": "assistant",
                    "content": greeting_text
                })
                logger.info("✅ Greeting added to LLM context")
                greeting_given = True
                
            except Exception as e:
                logger.error(f"❌ Error generating greeting: {e}")

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(task)
