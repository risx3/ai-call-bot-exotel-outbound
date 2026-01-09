import os
import asyncio
import pickle
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger
import psycopg2
import time

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



greeting_text_dict = {
    # 🇮🇳 Indian languages
    "hindi": "नमस्ते {client_name}! मैं Priya बोल रही हूँ {app_name} से। क्या अभी बात करना convenient है?",
    "bengali": "নমস্কার {client_name}! আমি Priya বলছি {app_name} থেকে। এখন কথা বলা কি সুবিধাজনক?",
    "telugu": "నమస్తే {client_name}! నేను {app_name} నుండి Priya మాట్లాడుతున్నాను. ఇప్పుడు మాట్లాడటం సౌకర్యంగా ఉందా?",
    "marathi": "नमस्कार {client_name}! मी {app_name} मधून Priya बोलत आहे. सध्या बोलायला सोयीचे आहे का?",
    "tamil": "வணக்கம் {client_name}! நான் {app_name} இலிருந்து Priya பேசுகிறேன். இப்போது பேசுவது வசதியா?",
    "urdu": "نمستے {client_name}! میں {app_name} سے Priya بات کر رہی ہوں۔ کیا اس وقت بات کرنا مناسب है؟",
    "gujarati": "નમસ્તે {client_name}! હું {app_name} તરફથી Priya બોલું છું. શું અત્યારે વાત કરવી અનુકૂળ છે?",
    "kannada": "ನಮಸ್ಕಾರ {client_name}! ನಾನು {app_name} ನಿಂದ Priya ಮಾತನಾಡುತ್ತಿದ್ದೇನೆ. ಈಗ ಮಾತನಾಡಲು ಅನುಕೂಲವೇ?",
    "odia": "ନମସ୍କାର {client_name}! ମୁଁ {app_name} ରୁ Priya କଥା ହେଉଛି। ଏହି ସମୟରେ କଥା ହେବା ସୁବିଧାଜନକ କି?",
    "malayalam": "നമസ്കാരം {client_name}! ഞാൻ {app_name} നിന്നുള്ള Priya ആണ് സംസാരിക്കുന്നത്. ഇപ്പോൾ സംസാരിക്കാൻ സൗകര്യമുണ്ടോ?",
    "punjabi": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ {client_name}! ਮੈਂ {app_name} ਤੋਂ Priya ਗੱਲ ਕਰ ਰਹੀ ਹਾਂ। ਕੀ ਹੁਣ ਗੱਲ ਕਰਨਾ ਠੀਕ ਹੈ?",
    "assamese": "নমস্কাৰ {client_name}! মই {app_name}ৰ পৰা Priya কথা কৈছোঁ। এতিয়া কথা পাতিবলৈ সুবিধা আছে নে?",
    "maithili": "नमस्कार {client_name}! हम {app_name} सँ Priya बोल रहल छी। की एखन बात करनाइ सुविधाजनक अछि?",
    "santali": "ᱱᱚᱢᱚᱥᱠᱟᱨ {client_name}! ᱤᱧ {app_name} ᱠᱷᱚᱱ Priya ᱠᱟᱛᱷᱟ ᱠᱚᱨ ᱮᱫᱟᱹᱧ। ᱱᱤᱛᱚᱜ ᱠᱟᱛᱷᱟ ᱠᱚᱨᱟᱭ ᱥᱩᱵᱤᱫᱷᱟ ᱢᱮᱱᱟ?",
    "kashmiri": "नमस्कार {client_name}! मैं {app_name} से Priya बोल रही हूँ। क्या अभी बात करना मुनासिब है?",
    "nepali": "नमस्ते {client_name}! म {app_name} बाट Priya बोलदै छु। अहिले कुरा गर्न मिल्छ?",
    "konkani": "नमस्कार {client_name}! हांव {app_name} कडल्यान Priya उलयता. आता बोलप सोयीचें आसा?",
    "sindhi": "नमस्ते {client_name}! मैं {app_name} से Priya बात कर रही हूँ। क्या इस वक्त बात करना ठीक है?",
    "dogri": "नमस्कार {client_name}! मैं {app_name} शा Priya बोलै दी आं। क्या हून गल्ल करना ठीक ऐ?",
    "manipuri": "ꯍꯥꯏ {client_name}! ꯑꯩ {app_name} ꯗꯒꯤ Priya ꯃꯥꯏꯗꯨꯅꯥ ꯋꯥꯡꯂꯤ। ꯍꯧꯖꯤꯛ ꯋꯥꯡꯕ ꯃꯇꯧ ꯑꯣꯏꯔꯥ?",
    "bodo": "नमस्कार {client_name}! आं {app_name} निफ्राय Priya बुंनो। दा बाथ्राय जोनाय जाबाय नामा?",
    "sanskrit": "नमस्कारः {client_name}! अहं {app_name} तः Priya भाषे। किम् इदानीं संवादः सुविधाजनकः अस्ति?",

    # 🇮🇳 Additional Indian languages
    "rajasthani": "राम राम सा {client_name}! मैं {app_name} से Priya बोल रही हूँ। क्या अभी बात करना ठीक है?",
    "haryanvi": "राम राम {client_name}! मैं {app_name} तै Priya बोल री स्यूँ। के अभी बात कर साकै सै?",
    "chhattisgarhi": "राम राम {client_name}! मैं {app_name} ले Priya बोलत हौं। अभी बात करना ठीक हे का?",
    "garhwali": "नमस्कार {client_name}! मैं {app_name} बाट Priya बोलूं। क्या अभी बात करना ठीक छ?",
    "kumayuni": "नमस्कार {client_name}! मैं {app_name} बाट Priya बोलूं छूं। अभी बात करन ठीक छा?",
    "tulu": "ನಮಸ್ಕಾರ {client_name}! ನಾನು {app_name} ದಿಂದ Priya ಮಾತನಾಡುತ್ತಿದ್ದೇನೆ. ಇಪ್ಪೊ ಮಾತನಾಡಕ್ಕೆ ಅನುಕೂಲವೇ?",
    "bhili": "नमस्कार {client_name}! हूं {app_name} से Priya बोलूं छूं। अभी बात करवा ठीक छे?",
    "gondi": "नमस्कार {client_name}! मी {app_name} तर्फे Priya बोलत आहें। आता बोलणं योग्य आहे का?",
    "khasi": "Khublei {client_name}! Nga dei ka Priya na {app_name}. Ka long kaba biang ban kren mynta?",
    "mizo": "Chibai {client_name}! Ka Priya ka ni a ni a {app_name}. Tunah hun biak loh?",
    "kokborok": "Khumulung {client_name}! Ang {app_name} ni Priya ya tong. Da tongnai somo somo?",
    "ho": "Johar {client_name}! Ing {app_name} khon Priya katha koira. Nete katha koira suvidha mena?",
    "mundari": "Johar {client_name}! Ing {app_name} khon Priya katha koira. Abhi katha koira thik hae?",
    "angika": "नमस्कार {client_name}! हम {app_name} सँ Priya बोल रहल छी। अभी बात करनाइ ठीक अछि का?",
    "bhojpuri": "नमस्कार {client_name}! हम {app_name} से Priya बोलत बानी। का अभी बात कर सकेनी?",
    "nagamese": "নমস্কাৰ {client_name}! মই {app_name}ৰ পৰা Priya কথা কৈছোঁ। এতিয়া কথা পাতিবলৈ সুবিধা আছে নে?",

    # 🌍 International languages
    "english": "Hello {client_name}! This is Priya calling from {app_name}. Is this a convenient time to talk?",
    "spanish": "¡Hola {client_name}! Le habla Priya de {app_name}. ¿Es un buen momento para hablar?",
    "french": "Bonjour {client_name} ! Je suis Priya de la part de {app_name}. Est-ce un bon moment pour parler ?",
    "german": "Hallo {client_name}! Hier spricht Priya von {app_name}. Ist es gerade ein guter Zeitpunkt zum Sprechen?",
    "italian": "Ciao {client_name}! Sono Priya da {app_name}. È un buon momento per parlare?",
    "portuguese": "Olá {client_name}! Aqui é a Priya falando da {app_name}. Este é um bom momento para conversar?",
    "dutch": "Hallo {client_name}! Dit is Priya van {app_name}. Komt het nu goed om even te praten?",
    "polish": "Dzień dobry {client_name}! Tu Priya z {app_name}. Czy to dobry moment na rozmowę?",
    "russian": "Здравствуйте, {client_name}! Это Priya из {app_name}. Удобно ли вам сейчас поговорить?",
    "turkish": "Merhaba {client_name}! Ben {app_name}’den Priya. Şu an konuşmak için uygun mu?",
    "arabic": "مرحباً {client_name}! معك بريا من {app_name}. هل هذا وقت مناسب للتحدث؟",
    "indonesian": "Halo {client_name}! Saya Priya dari {app_name}. Apakah sekarang waktu yang tepat untuk berbicara?",
    "thai": "สวัสดีค่ะ {client_name}! ดิฉัน Priya โทรมาจาก {app_name} ตอนนี้สะดวกคุยไหมคะ?",
    "vietnamese": "Xin chào {client_name}! Tôi là Priya gọi từ {app_name}. Bây giờ nói chuyện có tiện không?",
    "japanese": "こんにちは {client_name} さん。{app_name}のPriyaと申します。今お話ししてもよろしいでしょうか？",
    "korean": "안녕하세요 {client_name}님! {app_name}의 Priya입니다. 지금 통화 가능하신가요?",
    "chinese_simplified": "您好，{client_name}！我是来自 {app_name} 的 Priya。现在方便通话吗？",
    "chinese_traditional": "您好，{client_name}！我是來自 {app_name} 的 Priya。現在方便通話嗎？",
    "ukrainian": "Добрий день, {client_name}! Це Priya з {app_name}. Чи зручно вам зараз говорити?",
    "czech": "Dobrý den, {client_name}! Tady Priya z {app_name}. Je teď vhodná chvíle na rozhovor?",
    "hungarian": "Jó napot, {client_name}! Itt Priya a(z) {app_name} képviseletében. Most alkalmas beszélni?",
    "romanian": "Bună ziua, {client_name}! Sunt Priya de la {app_name}. Este un moment potrivit pentru a vorbi?",
    "greek": "Γεια σας {client_name}! Είμαι η Priya από το {app_name}. Είναι καλή στιγμή να μιλήσουμε;",
    "swedish": "Hej {client_name}! Det här är Priya från {app_name}. Passar det bra att prata nu?",
    "finnish": "Hei {client_name}! Tämä on Priya {app_name}-sovelluksesta. Onko nyt sopiva hetki puhua?",
    "danish": "Hej {client_name}! Det er Priya fra {app_name}. Passer det at tale nu?",
    "norwegian": "Hei {client_name}! Dette er Priya fra {app_name}. Passer det å snakke nå?",
    "hebrew": "שלום {client_name}! מדברת פריה מ־{app_name}. האם זה זמן נוח לדבר?"
}

def get_db_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT", "5432"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )
# Helper function to load call context from PostgreSQL database
def load_call_context_db(call_sid: str) -> dict:
    """Load call context from PostgreSQL database."""
    try:
        
        conn = get_db_conn()
        cursor = conn.cursor()
        
        # Fetch call context
        query = """
        SELECT call_sid, phone_number, app_name, reason, language, client_name
        FROM call_contexts
        WHERE call_sid = %s AND is_active = TRUE;
        """
        
        cursor.execute(query, (call_sid,))
        result = cursor.fetchone()
        
        cursor.close()
        conn.close()
        
        if result:
            context = {
                "call_sid": result[0],
                "phone_number": result[1],
                "app_name": result[2],
                "reason": result[3],
                "language": result[4],
                "client_name": result[5],
            }
            logger.info(f"✅ Loaded call context for {call_sid} from database")
            return context
        else:
            logger.warning(f"⚠️  Call context not found for {call_sid}")
            return {}
    except Exception as e:
        logger.error(f"❌ Failed to load call context from database: {e}")
        return {}

# -----------------------------------------------------------------------------
# ENV
# -----------------------------------------------------------------------------
load_dotenv(override=True)

# -----------------------------------------------------------------------------
# SERVICE INITIALIZATION
# -----------------------------------------------------------------------------

# def _create_services():
#     """Create fresh service instances for each call."""
#     logger.info("🚀 Creating fresh AI services for this call")

#     services = {
#         "stt": OpenAISTTService(api_key=os.getenv("OPENAI_API_KEY")),
#         # "llm": OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY")),

#         "llm": OpenAILLMService(
#                 api_key=os.getenv("OPENAI_API_KEY"),
#                 stream=True,          # 🔥 critical
#                 temperature=0.4,
#             ),
#         "tts": ElevenLabsTTSService(
#             api_key=os.getenv("ELEVENLABS_API_KEY"),
#             voice_id=os.getenv("ELEVENLABS_VOICE_ID"),
#         ),
#     }

#     logger.info("✅ Fresh AI services created")
#     return services

# -----------------------------------------------------------------------------
# GLOBAL SERVICE CLIENTS (REUSED)
# -----------------------------------------------------------------------------

GLOBAL_STT = OpenAISTTService(
    api_key=os.getenv("OPENAI_API_KEY"),
)

GLOBAL_LLM = OpenAILLMService(
    api_key=os.getenv("OPENAI_API_KEY"),
    stream=True,
    temperature=0.4,
)

GLOBAL_TTS = ElevenLabsTTSService(
    api_key=os.getenv("ELEVENLABS_API_KEY"),
    voice_id=os.getenv("ELEVENLABS_VOICE_ID"),
)


# -----------------------------------------------------------------------------
# BOT ENTRYPOINT
# -----------------------------------------------------------------------------
async def bot(runner_args: RunnerArguments):
    """
    Main bot function for handling incoming calls.
    
    Args:
        runner_args: WebSocket runner arguments
        call_contexts_dict: Dictionary of all call contexts keyed by call_sid
    """
    
    
    transport_type, call_data = await parse_telephony_websocket(
        runner_args.websocket
    )

    logger.info(f"🔌 Transport detected: {transport_type}")
    
    call_id = call_data.get("call_id")
    print(f"Call ID #########################>>>>: {call_id}")
    
    # Load call context from PostgreSQL database (for multi-worker support)
    print("Loading call context from database...")
    call_context = load_call_context_db(call_id)
    print("Initial database called")
    # if not call_context:
    #     time.sleep(2)  # wait for 2 seconds before retrying
    #     call_context = load_call_context_db(call_id)
    
    print("database context:", call_context)
    
    # Fallback to in-memory dict if database context not found
    # if not call_context and call_id:
    #     logger.warning(f"⚠️  No context found in database for call_id {call_id}, using defaults")
    #     print(f"Call context is empty, using defaults")

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
    # services = _create_services()
    # stt = services["stt"]
    # llm = services["llm"]
    # tts = services["tts"]

    stt = GLOBAL_STT
    llm = GLOBAL_LLM
    tts = GLOBAL_TTS

    
    

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
            enable_metrics=False,
            enable_usage_metrics=False  ,
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
    # @task.event_handler("on_pipeline_started")
    # async def on_pipeline_started(task, event):
    #     nonlocal greeting_given

    #     if greeting_given:
    #         return

    #     greeting_given = True
    #     logger.info("🎤 Scheduling greeting (non-blocking)")

    #     async def play_greeting():
    #         try:
    #             async for frame in tts.run_tts(text=greeting_text):
    #                 await transport.output().push_frame(frame)

    #             context.messages.append({
    #                 "role": "assistant",
    #                 "content": greeting_text
    #             })

    #             logger.info("✅ Greeting completed")

    #         except Exception as e:
    #             logger.error(f"❌ Greeting error: {e}")

    #     # 🚀 THIS IS THE KEY LINE
    #     asyncio.create_task(play_greeting())

    

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    
    try:
        await runner.run(task)
        
    finally:
        pass
