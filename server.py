# server.py
import os
import multiprocessing
import aiohttp
import psycopg2
import asyncio
import uvicorn

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from loguru import logger

from pipecat.runner.types import WebSocketRunnerArguments
from bot import bot

load_dotenv(override=True)

# -----------------------------------------------------------------------------
# DATABASE
# -----------------------------------------------------------------------------

def get_db_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT", "5432"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )

def save_call_context_db(call_sid: str, context: dict):
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO call_contexts
            (call_sid, phone_number, app_name, reason, language, client_name, is_active)
            VALUES (%s,%s,%s,%s,%s,%s,TRUE)
            ON CONFLICT (call_sid)
            DO UPDATE SET
                phone_number = EXCLUDED.phone_number,
                app_name = EXCLUDED.app_name,
                reason = EXCLUDED.reason,
                language = EXCLUDED.language,
                client_name = EXCLUDED.client_name,
                updated_at = CURRENT_TIMESTAMP,
                is_active = TRUE
            """,
            (
                call_sid,
                context["phone_number"],
                context["app_name"],
                context["reason"],
                context["language"],
                context["client_name"],
            ),
        )

        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"✅ Saved call context for {call_sid}")
    except Exception as e:
        logger.error(f"❌ DB error: {e}")

# -----------------------------------------------------------------------------
# FASTAPI
# -----------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = aiohttp.ClientSession()
    yield
    await app.state.http.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# EXOTEL
# -----------------------------------------------------------------------------

async def make_exotel_call(session, customer_number: str) -> dict:
    url = f"https://api.exotel.com/v1/Accounts/{os.getenv('EXOTEL_SID')}/Calls/connect"

    auth = aiohttp.BasicAuth(
        os.getenv("EXOTEL_API_KEY"),
        os.getenv("EXOTEL_API_TOKEN"),
    )

    data = {
        "From": customer_number,
        "CallerId": os.getenv("EXOTEL_PHONE_NUMBER"),
        "Url": "http://my.exotel.com/pixelastro1/exoml/start_voice/1136779",
    }

    async with session.post(url, data=data, auth=auth) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise Exception(text)

        return {
            "call_sid": text.split("<Sid>")[1].split("</Sid>")[0],
            "status": text.split("<Status>")[1].split("</Status>")[0],
        }

# -----------------------------------------------------------------------------
# API
# -----------------------------------------------------------------------------

@app.post("/start")
async def initiate_outbound_call(request: Request):
    data = await request.json()
    settings = data.get("dialout_settings", {})

    phone_number = settings.get("phone_number")
    if not phone_number:
        raise HTTPException(400, "dialout_settings.phone_number is required")

    result = await make_exotel_call(
        session=request.app.state.http,
        customer_number=str(phone_number),
    )

    call_sid = result["call_sid"]

    call_context = {
        "phone_number": phone_number,
        "app_name": settings.get("app_name"),
        "reason": settings.get("reason"),
        "language": settings.get("language"),
        "client_name": settings.get("client_name"),
        "call_sid": call_sid,
    }

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, save_call_context_db, call_sid, call_context)

    return JSONResponse(
        {
            "status": result["status"],
            "call_sid": result["call_sid"],
            "call_context": call_context,
        }
    )

# -----------------------------------------------------------------------------
# WEBSOCKET
# -----------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("🔌 WebSocket connected")

    try:
        runner_args = WebSocketRunnerArguments(websocket=websocket)
        runner_args.handle_sigint = False
        await bot(runner_args)

    except StopAsyncIteration:
        logger.info("WS closed before media")
    except Exception as e:
        logger.error(f"WS error: {e}")

# -----------------------------------------------------------------------------

@app.get("/health")
async def healthcheck():
    required_envs = [
        "EXOTEL_API_KEY",
        "EXOTEL_API_TOKEN",
        "EXOTEL_SID",
        "EXOTEL_PHONE_NUMBER",
    ]

    missing = [env for env in required_envs if not os.getenv(env)]

    if missing:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "missing_env_vars": missing},
        )

    return {
        "status": "ok",
        "service": "exotel-outbound-server",
    }

if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=7862,
        workers=min(2, multiprocessing.cpu_count()),
    )
