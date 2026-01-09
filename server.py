import os
import multiprocessing
from contextlib import asynccontextmanager
import asyncio
import xml.etree.ElementTree as ET
from pathlib import Path
from threading import Thread
from typing import Optional
from bot import bot
from pipecat.runner.types import WebSocketRunnerArguments
import aiohttp
import psycopg2
import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel


load_dotenv(override=True)

# Request/Response Models
class CallProcessRequest(BaseModel):
    call_sid: str

class CallProcessDetailedResponse(BaseModel):
    status: str
    message: str
    call_sid: str
    data: Optional[dict] = None

def get_db_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT", "5432"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )



# Helper function to save call context to PostgreSQL
def save_call_context_db(call_sid: str, context: dict) -> bool:
    """Save call context to PostgreSQL database."""
    try:
        
        conn = get_db_conn()
        cursor = conn.cursor()
        
        # Insert or update call context
        query = """
        INSERT INTO call_contexts (call_sid, phone_number, app_name, reason, language, client_name, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT (call_sid) DO UPDATE SET
            phone_number = EXCLUDED.phone_number,
            app_name = EXCLUDED.app_name,
            reason = EXCLUDED.reason,
            language = EXCLUDED.language,
            client_name = EXCLUDED.client_name,
            updated_at = CURRENT_TIMESTAMP,
            is_active = TRUE;
        """
        
        cursor.execute(query, (
            call_sid,
            context.get("phone_number"),
            context.get("app_name"),
            context.get("reason"),
            context.get("language"),
            context.get("client_name")
        ))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"✅ Saved call context for {call_sid} to database")
        return True
    except Exception as e:
        print(f"❌ Failed to save call context to database: {e}")
        return False


async def make_exotel_call(
    session: aiohttp.ClientSession,
    customer_number: str,
):
    """Make an outbound call using Exotel Connect API (ExoML)."""

    api_key = os.getenv("EXOTEL_API_KEY")
    api_token = os.getenv("EXOTEL_API_TOKEN")
    sid = os.getenv("EXOTEL_SID")
    caller_id = os.getenv("EXOTEL_PHONE_NUMBER")

    if not all([api_key, api_token, sid, caller_id]):
        raise ValueError("Missing Exotel credentials or EXOTEL_PHONE_NUMBER")

    # ✅ CLEAN URL (NO CREDENTIALS IN URL)
    url = f"https://api.exotel.com/v1/Accounts/{sid}/Calls/connect"

    data = {
        "From": customer_number,     # Customer phone number
        "CallerId": caller_id,       # Your ExoPhone
        "Url": "http://my.exotel.com/pixelastro1/exoml/start_voice/1136779",
    }

    auth = aiohttp.BasicAuth(api_key, api_token)

    async with session.post(
        url,
        data=data,
        auth=auth,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as response:

        text = await response.text()
        print(f"Exotel response: {response.status} - {text}")
        
        if response.status != 200:
            raise Exception(f"Exotel API error ({response.status}): {text}")

        call_sid = "unknown"
        if "<Sid>" in text:
            call_sid = text.split("<Sid>")[1].split("</Sid>")[0]
        if "<Status>" in text:
            call_status = text.split("<Status>")[1].split("</Status>")[0]
        return {
            "status": call_status,
            "call_sid": call_sid,
        }


# ----------------- FASTAPI SETUP ----------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.session = aiohttp.ClientSession()
    yield
    await app.state.session.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store call context per call (using call_sid as key)
_call_contexts = {}

# ----------------- API ----------------- #

@app.post("/start")
async def initiate_outbound_call(request: Request) -> JSONResponse:
    """Trigger outbound Exotel call via Postman."""
    
    data = await request.json()
    settings = data.get("dialout_settings", {})

    phone_number = settings.get("phone_number")
    app_name = settings.get("app_name")
    reason = settings.get("reason")
    language = settings.get("language")
    client_name = settings.get("client_name")

    if not phone_number:
        raise HTTPException(
            status_code=400,
            detail="dialout_settings.phone_number is required",
        )

    try:
        
        result = await make_exotel_call(
            session=request.app.state.session,
            customer_number=str(phone_number),
        )
        call_sid = result.get("call_sid")
        call_context = {
                "phone_number": phone_number,
                "app_name": app_name,
                "reason": reason,
                "language": language,
                "client_name": client_name,
                "call_sid": call_sid,
            }
        save_call_context_db(call_sid, call_context)
            
        print(f"📦 Stored call context for {call_sid} in database")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse(
        {
            "status": result["status"],
            "call_sid": result["call_sid"],
            "phone_number": phone_number,
            "call_context": {
                "app_name": app_name,
                "reason": reason,
                "language": language,
                "client_name": client_name,
            }
        }
    )

# @app.websocket("/ws")
# async def websocket_endpoint(websocket: WebSocket):
#     await websocket.accept()
#     print("WebSocket connected")

#     try:
#         from bot import bot
#         from pipecat.runner.types import WebSocketRunnerArguments

#         runner_args = WebSocketRunnerArguments(websocket=websocket)
#         runner_args.handle_sigint = False

#         await bot(runner_args)

#     except StopAsyncIteration:
#         # Normal Exotel behavior
#         print("⚠️ Exotel WS closed before start frame")
#         return

#     except Exception as e:
#         print(f"WebSocket error: {e}")

#     finally:
#         try:
#             await websocket.close()
#         except RuntimeError:
#             pass


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket connected")

    try:
        

        runner_args = WebSocketRunnerArguments(websocket=websocket)
        runner_args.handle_sigint = False

        await bot(runner_args)

    except StopAsyncIteration:
        # ✅ NORMAL: Exotel opened then closed without media
        print("Exotel WS closed before start frame")
        return

    except asyncio.CancelledError:
        # ✅ Shutdown / Ctrl+C
        print("WebSocket task cancelled")
        raise

    except Exception as e:
        print(f"WebSocket error: {e}")

    # ❌ NO websocket.close() HERE
    # Let FastAPI / Uvicorn handle it



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
    # Get the number of workers from the environment variable or calculate based on CPU cores
    workers = int(os.getenv("UVICORN_WORKERS", (2 * multiprocessing.cpu_count()) + 1))

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=7862,
        workers=5  # Specify the number of workers
    )

