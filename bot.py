import os
import asyncio
import pickle
from pathlib import Path
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

# Call contexts directory (same as in server.py)
CALL_CONTEXTS_DIR = Path("./call_contexts")
CALL_CONTEXTS_DIR.mkdir(exist_ok=True)

# Helper function to load call context from pickle file
def load_call_context_pkl(call_sid: str) -> dict:
    """Load call context from pickle file."""
    try:
        pkl_path = CALL_CONTEXTS_DIR / f"{call_sid}.pkl"
        if pkl_path.exists():
            with open(pkl_path, 'rb') as f:
                context = pickle.load(f)
            logger.info(f"✅ Loaded call context for {call_sid} from {pkl_path}")
            return context
        else:
            logger.warning(f"⚠️  Pickle file not found for {call_sid}")
            return {}
    except Exception as e:
        logger.error(f"❌ Failed to load call context pickle: {e}")
        return {}

def delete_call_context_pkl(call_sid: str) -> bool:
    """Delete call context pickle file."""
    try:
        pkl_path = CALL_CONTEXTS_DIR / f"{call_sid}.pkl"
        if pkl_path.exists():
            pkl_path.unlink()
            logger.info(f"✅ Deleted call context pickle for {call_sid}")
            return True
        else:
            logger.warning(f"⚠️  Pickle file not found for deletion: {call_sid}")
            return False
    except Exception as e:
        logger.error(f"❌ Failed to delete call context pickle: {e}")
        return False

# -----------------------------------------------------------------------------
# ENV
# -----------------------------------------------------------------------------
load_dotenv(override=True)

# -----------------------------------------------------------------------------
# SERVICE INITIALIZATION
# -----------------------------------------------------------------------------

def _create_services():
    """Create fresh service instances for each call."""
    logger.info("🚀 Creating fresh AI services for this call")

    services = {
        "stt": OpenAISTTService(api_key=os.getenv("OPENAI_API_KEY")),
        "llm": OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY")),
        "tts": ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            voice_id=os.getenv("ELEVENLABS_VOICE_ID"),
        ),
    }

    logger.info("✅ Fresh AI services created")
    return services

# -----------------------------------------------------------------------------
# BOT ENTRYPOINT
# -----------------------------------------------------------------------------
async def bot(runner_args: RunnerArguments, call_contexts_dict: dict = None):
    """
    Main bot function for handling incoming calls.
    
    Args:
        runner_args: WebSocket runner arguments
        call_contexts_dict: Dictionary of all call contexts keyed by call_sid
    """
    if call_contexts_dict is None:
        call_contexts_dict = {}
    
    transport_type, call_data = await parse_telephony_websocket(
        runner_args.websocket
    )

    logger.info(f"🔌 Transport detected: {transport_type}")
    
    call_id = call_data.get("call_id")
    print(f"Call ID #########################>>>>: {call_id}")
    
    # Try to load call context from pickle file first (for multi-worker support)
    call_context = load_call_context_pkl(call_id)
    print("pklllllll",call_context)
    delete_call_context_pkl(call_id)  # Clean up after loading
    
    # Fallback to in-memory dict if pickle not found
    if not call_context and call_id and call_id in call_contexts_dict:
        call_context = call_contexts_dict[call_id]
        logger.info(f"✅ Found call context in memory for call_id {call_id}")
        print(f"Call context retrieved from memory: {call_context}")
    elif call_context:
        logger.info(f"✅ Found call context for call_id {call_id} from pickle file")
        print(f"Call context retrieved from pickle: {call_context}")
    else:
        logger.warning(f"⚠️  No stored context found for call_id {call_id}, using defaults")
        print(f"Available contexts in memory: {list(call_contexts_dict.keys())}")

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

    # Create fresh service instances for this specific call
    services = _create_services()
    stt = services["stt"]
    llm = services["llm"]
    tts = services["tts"]
    
    greeting_text_dict = {
    "hindi": "नमस्ते {client_name}! मैं Priya बोल रही हूँ {app_name} से। क्या अभी बात करना convenient है?",
    "bengali": "নমস্কার {client_name}! আমি Priya বলছি {app_name} থেকে। এখন কথা বলা কি সুবিধাজনক?",
    "telugu": "నమస్తే {client_name}! నేను {app_name} నుండి Priya మాట్లాడుతున్నాను. ఇప్పుడు మాట్లాడటం సౌకర్యంగా ఉందా?",
    "marathi": "नमस्कार {client_name}! मी {app_name} मधून Priya बोलत आहे. सध्या बोलायला सोयीचे आहे का?",
    "tamil": "வணக்கம் {client_name}! நான் {app_name} இலிருந்து Priya பேசுகிறேன். இப்போது பேசுவது வசதியா?",
    "urdu": "نمستے {client_name}! میں {app_name} سے Priya بات کر رہی ہوں۔ کیا اس وقت بات کرنا مناسب ہے؟",
    "gujarati": "નમસ્તે {client_name}! હું {app_name} તરફથી Priya બોલું છું. શું અત્યારે વાત કરવી અનુકૂળ છે?",
    "kannada": "ನಮಸ್ಕಾರ {client_name}! ನಾನು {app_name} ನಿಂದ Priya ಮಾತನಾಡುತ್ತಿದ್ದೇನೆ. ಈಗ ಮಾತನಾಡಲು ಅನುಕೂಲವೇ?",
    "odia": "ନମସ୍କାର {client_name}! ମୁଁ {app_name} ରୁ Priya କଥା ହେଉଛି। ଏହି ସମୟରେ କଥା ହେବା ସୁବିଧାଜନକ କି?",
    "malayalam": "നമസ്കാരം {client_name}! ഞാൻ {app_name} നിന്നുള്ള Priya ആണ് സംസാരിക്കുന്നത്. ഇപ്പോൾ സംസാരിക്കാൻ സൗകര്യമുണ്ടോ?",
    "punjabi": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ {client_name}! ਮੈਂ {app_name} ਤੋਂ Priya ਗੱਲ ਕਰ ਰਹੀ ਹਾਂ। ਕੀ ਹੁਣ ਗੱਲ ਕਰਨਾ ਠੀਕ ਹੈ?",
    "assamese": "নমস্কাৰ {client_name}! মই {app_name}ৰ পৰা Priya কথা কৈছোঁ। এতিয়া কথা পাতিবলৈ সুবিধা আছে নে?",
    "maithili": "नमस्कार {client_name}! हम {app_name} सँ Priya बोल रहल छी। की एखन बात करनाइ सुविधाजनक अछि?",
    "santali": "ᱱᱚᱢᱚᱥᱠᱟᱨ {client_name}! ᱤᱧ {app_name} ᱠᱷᱚᱱ Priya ᱠᱟᱛᱷᱟ ᱠᱚᱨ ᱮᱫᱟᱹᱧ। ᱱᱤᱛᱚᱜ ᱠᱟᱛᱷᱟ ᱠᱚᱨᱟᱭ ᱥᱩᱵᱤᱫᱷᱟ ᱢᱮᱱᱟ?",
    "kashmiri": "नमस्कार {client_name}! मैं {app_name} से Priya बोल रही हूँ। क्या अभी बात करना मुनासिब है?",
    "nepali": "नमस्ते {client_name}! म {app_name} बाट Priya बोल्दै छु। अहिले कुरा गर्न मिल्छ?",
    "konkani": "नमस्कार {client_name}! हांव {app_name} कडल्यान Priya उलयता. आता बोलप सोयीचें आसा?",
    "sindhi": "नमस्ते {client_name}! मैं {app_name} से Priya बात कर रही हूँ। क्या इस वक्त बात करना ठीक है?",
    "dogri": "नमस्कार {client_name}! मैं {app_name} शा Priya बोलै दी आं। क्या हून गल्ल करना ठीक ऐ?",
    "manipuri": "ꯍꯥꯏ {client_name}! ꯑꯩ {app_name} ꯗꯒꯤ Priya ꯃꯥꯏꯗꯨꯅꯥ ꯋꯥꯡꯂꯤ। ꯍꯧꯖꯤꯛ ꯋꯥꯡꯕ ꯃꯇꯧ ꯑꯣꯏꯔꯥ?",
    "bodo": "नमस्कार {client_name}! आं {app_name} निफ्राय Priya बुंनो। दा बाथ्राय जोनाय जाबाय नामा?",
    "sanskrit": "नमस्कारः {client_name}! अहं {app_name} तः Priya भाषे। किम् इदानीं संवादः सुविधाजनकः अस्ति?"
}
    lang = call_context.get("language", "")
    if lang not in greeting_text_dict.keys():
        lang = "hindi"  # default to hindi if language not recognized
    greeting_text = greeting_text_dict[lang].format(client_name=call_context.get("client_name", ""),
                                                    app_name=call_context.get("app_name", ""))
    greeting_given = False
    
    
    system_prompt = base_system_prompt.format(
        app_name=call_context.get("app_name", ""),
        reason=call_context.get("reason", ""),
        language=call_context.get("language", ""),
        client_name=call_context.get("client_name", ""),
                                                )
    # print(system_prompt)
    messages = [
        {
            "role": "system",
            "content": system_prompt,
        }
    ]

    context = LLMContext(messages)
    context_aggregator = LLMContextAggregatorPair(context)
    print(context_aggregator)
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
        print("on_pipeline_started called")
        """Wait for user input, then play greeting."""
        nonlocal greeting_given
        
        logger.info("✅ Pipeline started — waiting for user to speak")
        
        # Give the pipeline time to receive first user input
        # await asyncio.sleep(1)÷]
        
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
    
    try:
        await runner.run(task)
    finally:
        pass