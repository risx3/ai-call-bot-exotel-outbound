# bot.py
import os
import asyncio
import psycopg2
from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.exotel import ExotelFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams

from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

from prompts import base_system_prompt, greeting_text_dict

load_dotenv(override=True)

# -----------------------------------------------------------------------------
# GLOBAL SAFE COMPONENTS
# -----------------------------------------------------------------------------

GLOBAL_VAD = SileroVADAnalyzer()

GLOBAL_STT = OpenAISTTService(
    api_key=os.getenv("OPENAI_API_KEY"),
)

GLOBAL_LLM = OpenAILLMService(
    api_key=os.getenv("OPENAI_API_KEY"),
    stream=True,
    temperature=0.4,
)

# -----------------------------------------------------------------------------
# DB
# -----------------------------------------------------------------------------

def load_call_context_db(call_sid: str) -> dict:
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT", "5432"),
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT phone_number, app_name, reason, language, client_name
            FROM call_contexts
            WHERE call_sid=%s AND is_active=TRUE
            """,
            (call_sid,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return {}

        return dict(
            phone_number=row[0],
            app_name=row[1],
            reason=row[2],
            language=row[3],
            client_name=row[4],
        )

    except Exception as e:
        logger.error(f"DB load error: {e}")
        return {}

# -----------------------------------------------------------------------------
# BOT
# -----------------------------------------------------------------------------

async def bot(runner_args):
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    
    logger.info(f"🔌 Transport detected: {transport_type}")

    call_sid = call_data["call_id"]

    logger.info(f"📞 Call started: {call_sid}")

    call_context = load_call_context_db(call_sid)

    serializer = ExotelFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_sid,
    )

    transport = FastAPIWebsocketTransport(
        websocket=runner_args.websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=GLOBAL_VAD,
            serializer=serializer,
        ),
    )

    # 🔥 PER-CALL TTS (fixes latency)
    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY"),
        voice_id=os.getenv("ELEVENLABS_VOICE_ID"),
    )

    lang = call_context.get("language", "hindi")

    greeting_text = greeting_text_dict.get(lang, greeting_text_dict["hindi"]).format(
        client_name=call_context.get("client_name", ""),
        app_name=call_context.get("app_name", ""),
    )
    greeting_given = False

    system_prompt = base_system_prompt.format(
        app_name=call_context.get("app_name", ""),
        reason=call_context.get("reason", ""),
        language=lang,
        client_name=call_context.get("client_name", ""),
    )

    context = LLMContext([{"role": "system", "content": system_prompt}])
    aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            GLOBAL_STT,
            aggregator.user(),
            GLOBAL_LLM,
            tts,
            transport.output(),
            aggregator.assistant(),
        ]
    )

    task = PipelineTask(
    pipeline,
    params=PipelineParams(
        audio_in_sample_rate=8000,
        audio_out_sample_rate=8000,
        enable_metrics=False,
        enable_usage_metrics=False,
    ),
    idle_timeout_secs=None,           # disables idle detection
    cancel_on_idle_timeout=False,     # optional, doesn't matter here
)
    # -------------------------------------------------------------------------
    # GREETING (AFTER USER SPEAKS 🔊)
    # -------------------------------------------------------------------------
    @task.event_handler("on_pipeline_started")
    async def on_pipeline_started(task, event):
        print("on_pipeline_started called")
        """Wait for user input, then play greeting."""
        nonlocal greeting_given
        
        logger.info("✅ Pipeline started — waiting for user to speak")
        
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
        
    

    
