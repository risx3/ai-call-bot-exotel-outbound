import os
import multiprocessing
from contextlib import asynccontextmanager
import asyncio
import xml.etree.ElementTree as ET

import aiohttp
import psycopg2
import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

load_dotenv(override=True)

# ----------------- DATABASE HELPERS ----------------- #

async def save_call_to_database(call_sid: str,call_status: str) -> bool:
    """Save call_sid to the crm-ai-db table asynchronously."""
    def _save_in_thread():
        try:
            conn = psycopg2.connect(
                host=os.getenv("DB_HOST"),
                port=os.getenv("DB_PORT", "5432"),
                database=os.getenv("DB_NAME"),
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD")
            )
            
            cursor = conn.cursor()
            
            # Insert call_sid with Completed = FALSE by default
            insert_query = """
            INSERT INTO "crm-ai-db" (sid, "Completed","call_status") 
            VALUES (%s, FALSE,%s)
            ON CONFLICT (sid) DO NOTHING;
            """
            
            cursor.execute(insert_query, (call_sid,call_status))
            conn.commit()
            
            cursor.close()
            conn.close()
            
            print(f"✅ Call SID {call_sid} saved to database")
            return True
            
        except psycopg2.Error as e:
            print(f"❌ Database error: {e}")
            return False
        except Exception as e:
            print(f"❌ Error saving to database: {e}")
            return False
    
    # Run the blocking database operation in a thread pool
    return await asyncio.to_thread(_save_in_thread)

async def get_call_analysis(call_sid: str) -> dict:
    """Fetch full call analysis from the database (excluding _completed columns)."""
    def _get_analysis_in_thread():
        try:
            conn = psycopg2.connect(
                host=os.getenv("DB_HOST"),
                port=os.getenv("DB_PORT", "5432"),
                database=os.getenv("DB_NAME"),
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD")
            )
            
            cursor = conn.cursor()
            
            # Fetch call analysis data (excluding columns ending with _completed)
            query = """
            SELECT 
                sid, "Completed", transcript, transcript_status,
                call_status, summary,
                information_requested,
                threat, priority,
                human_intervention,
                satisfaction,
                frustration,
                nuisance,
                repeated_complaint,
                next_best_action,
                open_questions,
                pii_details
            FROM "crm-ai-db" WHERE sid = %s;
            """
            
            cursor.execute(query, (call_sid,))
            result = cursor.fetchone()
            
            cursor.close()
            conn.close()
            
            if result:
                analysis = {
                    "sid": result[0],
                    "completed": result[1],
                    "transcript": result[2],
                    "transcript_status": result[3],
                    "call_status": result[4],
                    "summary": result[5],
                    "information_requested": result[6],
                    "threat": result[7],
                    "priority": result[8],
                    "human_intervention": result[9],
                    "satisfaction": result[10],
                    "frustration": result[11],
                    "nuisance": result[12],
                    "repeated_complaint": result[13],
                    "next_best_action": result[14],
                    "open_questions": result[15],
                    "pii_details": result[16],
                }
                return {"status": "success", "data": analysis}
            else:
                return {"status": "not_found", "data": None}
            
        except psycopg2.Error as e:
            print(f"❌ Database error: {e}")
            return {"status": "error", "message": str(e)}
        except Exception as e:
            print(f"❌ Error fetching from database: {e}")
            return {"status": "error", "message": str(e)}
    
    return await asyncio.to_thread(_get_analysis_in_thread)

# ----------------- HELPERS ----------------- #

async def get_call_info_from_exotel(call_sid: str) -> dict:
    """
    Fetch call information from Exotel API (returns XML).
    
    Args:
        call_sid (str): The call SID to fetch information for
        
    Returns:
        dict: Parsed call information with status and other details, or error dict if failed
    """
    def _fetch_from_exotel():
        try:
            api_key = os.getenv("EXOTEL_API_KEY")
            api_token = os.getenv("EXOTEL_API_TOKEN")
            exotel_sid = os.getenv("EXOTEL_SID")
            exotel_subdomain = os.getenv("EXOTEL_SUBDOMAIN", "api.exotel.com")
            
            if not all([api_key, api_token, exotel_sid]):
                return {"status": "error", "message": "Missing Exotel credentials"}
            
            # Build the Exotel API URL with credentials embedded
            url = f"https://{api_key}:{api_token}@{exotel_subdomain}/v1/Accounts/{exotel_sid}/Calls/{call_sid}"
            
            # Make the API request
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                try:
                    # Parse XML response
                    root = ET.fromstring(response.text)
                    
                    # Extract call data from XML
                    call_data = {}
                    call_element = root.find('Call')
                    
                    if call_element is not None:
                        for child in call_element:
                            call_data[child.tag] = child.text
                        
                        # Extract status from the parsed data
                        status = call_data.get('Status', 'unknown')
                        print(f"✅ Call info fetched for SID {call_sid}: Status = {status}")
                        
                        return {"status": "success", "call_status": status, "call_data": call_data}
                    else:
                        return {"status": "error", "message": "Call element not found in XML response"}
                        
                except ET.ParseError as e:
                    print(f"❌ Error parsing XML response for SID {call_sid}: {e}")
                    print(f"Response text: {response.text}")
                    return {"status": "error", "message": f"Failed to parse XML: {str(e)}"}
            else:
                print(f"❌ Error fetching call info for SID {call_sid}: Status {response.status_code}")
                print(f"Response text: {response.text}")
                return {"status": "error", "message": f"Exotel API returned status {response.status_code}"}
        
        except requests.exceptions.RequestException as error:
            print(f"❌ Request error while fetching call info from Exotel API: {error}")
            return {"status": "error", "message": f"Request failed: {str(error)}"}
        except Exception as error:
            print(f"❌ Error while fetching call info from Exotel API: {error}")
            return {"status": "error", "message": str(error)}
    
    return await asyncio.to_thread(_fetch_from_exotel)

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

# ----------------- API ----------------- #
CALL_CONTEXT = {}

@app.post("/start")
async def initiate_outbound_call(request: Request) -> JSONResponse:
    """Trigger outbound Exotel call via Postman."""
    
    data = await request.json()
    settings = data.get("dialout_settings", {})
    # settings = payload.dialout_settings


    phone_number = settings.get("phone_number")
    app_name = settings.get("app_name")
    reason = settings.get("reason")
    language = settings.get("language")
    client_name = settings.get("client_name")
    CALL_CONTEXT["phone_number"]=phone_number
    CALL_CONTEXT["app_name"]=app_name
    CALL_CONTEXT["reason"]=reason
    CALL_CONTEXT["language"]=language
    CALL_CONTEXT["client_name"]=client_name

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
        
        # Save call_sid to database asynchronously in the background
        call_status = result.get("status")
        call_sid = result.get("call_sid")
        if call_sid and call_sid != "unknown":
            # Fire and forget - don't await, returns immediately
            asyncio.create_task(save_call_to_database(call_sid,call_status))
        
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

        await bot(runner_args,CALL_CONTEXT)

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

@app.get("/check_call_status")
async def check_call_status(sid: str) -> JSONResponse:
    """Check the status of a call by SID using Exotel API."""
    if not sid:
        raise HTTPException(
            status_code=400,
            detail="sid parameter is required",
        )
    
    try:
        result = await get_call_info_from_exotel(sid)
        
        if result["status"] == "error":
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch call status: {result['message']}",
            )
        
        return JSONResponse(
            status_code=200,
            content={
                "sid": sid,
                "call_status": result["call_status"],
                }
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/call_analysis")
async def call_analysis(sid: str) -> JSONResponse:
    """Fetch detailed call analysis by SID."""
    if not sid:
        raise HTTPException(
            status_code=400,
            detail="sid parameter is required",
        )
    
    try:
        result = await get_call_analysis(sid)
        
        if result["status"] == "not_found":
            raise HTTPException(
                status_code=404,
                detail=f"Call with SID '{sid}' not found",
            )
        elif result["status"] == "error":
            raise HTTPException(
                status_code=500,
                detail=f"Database error: {result['message']}",
            )
        
        return JSONResponse(
            status_code=200,
            content=result["data"]
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




# ----------------- MAIN ----------------- #

if __name__ == "__main__":
    # Get the number of workers from the environment variable or calculate based on CPU cores
    workers = int(os.getenv("UVICORN_WORKERS", (2 * multiprocessing.cpu_count()) + 1))

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=7862,
        workers=2  # Specify the number of workers
    )

