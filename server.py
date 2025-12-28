# #
# # Copyright (c) 2025, Daily
# #
# # SPDX-License-Identifier: BSD 2-Clause License
# #

# """server.py

# Webhook server to handle outbound call requests, initiate calls via Exotel API,
# and handle subsequent WebSocket connections for Media Streams.
# """

# import os
# from contextlib import asynccontextmanager
# import multiprocessing

# import aiohttp
# import uvicorn
# from dotenv import load_dotenv
# from fastapi import FastAPI, HTTPException, Request, WebSocket
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.responses import JSONResponse

# load_dotenv(override=True)


# # ----------------- HELPERS ----------------- #


# async def make_exotel_call(session: aiohttp.ClientSession, to_number: str, from_number: str):
#     """Make an outbound call using Exotel's Connect API."""
#     api_key = os.getenv("EXOTEL_API_KEY")
#     api_token = os.getenv("EXOTEL_API_TOKEN")
#     sid = os.getenv("EXOTEL_SID")

#     if not all([api_key, api_token, sid]):
#         raise ValueError("Missing Exotel credentials: EXOTEL_API_KEY, EXOTEL_API_TOKEN, EXOTEL_SID")

#     # Exotel Connect API endpoint
#     url = f"https://api.exotel.com/v1/Accounts/{sid}/Calls/connect"

#     # Use form data for Exotel Connect Two Numbers API
#     data = {
#         "From": from_number,  # Bot number (called first, connects to WebSocket via App Bazaar)
#         "To": to_number,  # Customer number (called second, after bot "answers")
#         "CallerId": from_number,  # Your ExoPhone number
#         "CallType": "trans",  # Transactional call
#     }

#     # Use HTTP Basic Auth
#     auth = aiohttp.BasicAuth(api_key, api_token)

#     async with session.post(url, data=data, auth=auth) as response:
#         if response.status != 200:
#             error_text = await response.text()
#             raise Exception(f"Exotel API error ({response.status}): {error_text}")

#         # Exotel returns XML by default, extract key information
#         result_text = await response.text()

#         # Extract Sid from XML response for tracking
#         call_sid = "unknown"
#         if "<Sid>" in result_text:
#             start = result_text.find("<Sid>") + 5
#             end = result_text.find("</Sid>")
#             if end > start:
#                 call_sid = result_text[start:end]

#         return {"status": "call_initiated", "call_sid": call_sid}


# # ----------------- API ----------------- #


# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     # Create aiohttp session for Exotel API calls
#     app.state.session = aiohttp.ClientSession()
#     yield
#     # Close session when shutting down
#     await app.state.session.close()


# app = FastAPI(lifespan=lifespan)

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],  # Allow all origins for testing
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


# @app.post("/start")
# async def initiate_outbound_call(request: Request) -> JSONResponse:
#     """Handle outbound call request and initiate call via Exotel."""
#     print("Received outbound call request")

#     try:
#         data = await request.json()

#         # Validate request data
#         if not data.get("dialout_settings"):
#             raise HTTPException(
#                 status_code=400, detail="Missing 'dialout_settings' in the request body"
#             )

#         if not data["dialout_settings"].get("phone_number"):
#             raise HTTPException(
#                 status_code=400, detail="Missing 'phone_number' in dialout_settings"
#             )

#         # Extract the phone number to dial
#         phone_number = str(data["dialout_settings"]["phone_number"])
#         print(f"Processing outbound call to {phone_number}")

#         # Initiate outbound call via Exotel Connect API
#         try:
#             call_result = await make_exotel_call(
#                 session=request.app.state.session,
#                 to_number=phone_number,
#                 from_number=os.getenv("EXOTEL_PHONE_NUMBER"),
#             )

#             # Extract call SID from Exotel response
#             call_sid = call_result.get("call_sid", "unknown")

#         except Exception as e:
#             print(f"Error initiating Exotel call: {e}")
#             raise HTTPException(status_code=500, detail=f"Failed to initiate call: {str(e)}")

#     except HTTPException:
#         raise
#     except Exception as e:
#         print(f"Unexpected error: {str(e)}")
#         raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

#     return JSONResponse(
#         {
#             "call_sid": call_sid,
#             "status": "call_initiated",
#             "phone_number": phone_number,
#         }
#     )


# @app.websocket("/ws")
# async def websocket_endpoint(websocket: WebSocket):
#     """Handle WebSocket connection from Exotel Media Streams."""
#     await websocket.accept()
#     print("WebSocket connection accepted for outbound call")

#     try:
#         # Import the bot function from the bot module
#         from bot import bot
#         from pipecat.runner.types import WebSocketRunnerArguments

#         # Create runner arguments and run the bot
#         runner_args = WebSocketRunnerArguments(websocket=websocket)
#         runner_args.handle_sigint = False

#         await bot(runner_args)

#     except Exception as e:
#         print(f"Error in WebSocket endpoint: {e}")
#         await websocket.close()


# @app.get("/health")
# async def healthcheck():
#     required_envs = [
#         "EXOTEL_API_KEY",
#         "EXOTEL_API_TOKEN",
#         "EXOTEL_SID",
#         "EXOTEL_PHONE_NUMBER",
#     ]

#     missing = [env for env in required_envs if not os.getenv(env)]

#     if missing:
#         return JSONResponse(
#             status_code=500,
#             content={
#                 "status": "error",
#                 "missing_env_vars": missing,
#             },
#         )

#     return {
#         "status": "ok",
#         "service": "exotel-outbound-server",
#     }



# # ----------------- Main ----------------- #


# if __name__ == "__main__":
#     # Get the number of workers from the environment variable or calculate based on CPU cores
#     workers = int(os.getenv("UVICORN_WORKERS", (2 * multiprocessing.cpu_count()) + 1))

#     uvicorn.run(
#         app,
#         host="0.0.0.0",
#         port=7862,
#         workers=workers  # Specify the number of workers
#     )



import os
import multiprocessing
from contextlib import asynccontextmanager

import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

load_dotenv(override=True)

# ----------------- HELPERS ----------------- #

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

        if response.status != 200:
            raise Exception(f"Exotel API error ({response.status}): {text}")

        call_sid = "unknown"
        if "<Sid>" in text:
            call_sid = text.split("<Sid>")[1].split("</Sid>")[0]

        return {
            "status": "call_initiated",
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

# ----------------- API ----------------- #

@app.post("/start")
async def initiate_outbound_call(request: Request) -> JSONResponse:
    """Trigger outbound Exotel call via Postman."""

    data = await request.json()
    settings = data.get("dialout_settings", {})

    phone_number = settings.get("phone_number")

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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse(
        {
            "status": result["status"],
            "call_sid": result["call_sid"],
            "phone_number": phone_number,
        }
    )

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handle WebSocket connection from Exotel Media Streams."""
    await websocket.accept()
    print("WebSocket connected")

    try:
        from bot import bot
        from pipecat.runner.types import WebSocketRunnerArguments

        runner_args = WebSocketRunnerArguments(websocket=websocket)
        runner_args.handle_sigint = False

        await bot(runner_args)

    except Exception as e:
        print(f"WebSocket error: {e}")
        await websocket.close()

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

# ----------------- MAIN ----------------- #

if __name__ == "__main__":
    # Get the number of workers from the environment variable or calculate based on CPU cores
    workers = int(os.getenv("UVICORN_WORKERS", (2 * multiprocessing.cpu_count()) + 1))

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=7862,
        workers=workers  # Specify the number of workers
    )

