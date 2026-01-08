import os
import multiprocessing
from contextlib import asynccontextmanager
import asyncio
import xml.etree.ElementTree as ET
import pickle
from pathlib import Path
from threading import Thread
from typing import Optional

import aiohttp
import psycopg2
import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from working_pipeline import (
            get_call_info,
            download_audio,
            transcribe_audio,
            analyze_transcript_with_openai,
            save_structured_analysis_to_db,
            insert_call_record,
            restructure_analysis
        )

load_dotenv(override=True)

# Request/Response Models
class CallProcessRequest(BaseModel):
    call_sid: str

class CallProcessDetailedResponse(BaseModel):
    status: str
    message: str
    call_sid: str
    data: Optional[dict] = None

# Create a directory for call context pkl files (keeping for backward compatibility)
CALL_CONTEXTS_DIR = Path("./call_contexts")
CALL_CONTEXTS_DIR.mkdir(exist_ok=True)

# Store call context per call (using call_sid as key)
_call_contexts = {}

# Helper function to save call context to PostgreSQL
def save_call_context_db(call_sid: str, context: dict) -> bool:
    """Save call context to PostgreSQL database."""
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT", "5432"),
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD")
        )
        
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

def load_call_context_db(call_sid: str) -> dict:
    """Load call context from PostgreSQL database."""
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT", "5432"),
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD")
        )
        
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
            print(f"✅ Loaded call context for {call_sid} from database")
            return context
        else:
            print(f"⚠️  Call context not found for {call_sid}")
            return {}
    except Exception as e:
        print(f"❌ Failed to load call context from database: {e}")
        return {}

def delete_call_context_db(call_sid: str) -> bool:
    """Mark call context as inactive (soft delete) in PostgreSQL."""
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT", "5432"),
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD")
        )
        
        cursor = conn.cursor()
        
        # Soft delete by marking as inactive
        query = """
        UPDATE call_contexts
        SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
        WHERE call_sid = %s;
        """
        
        cursor.execute(query, (call_sid,))
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"✅ Marked call context as inactive for {call_sid}")
        return True
    except Exception as e:
        print(f"❌ Failed to delete call context from database: {e}")
        return False

# ----------------- DATABASE HELPERS ----------------- #



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

# Helper function to fetch call data from database
def get_call_data_from_db(conn, call_sid):
    """Fetch call data from database"""
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
                sid, call_status, transcript, transcript_status,
                summary, summary_completed,
                information_requested, information_requested_completed,
                threat, threat_completed,
                priority, priority_completed,
                human_intervention, human_intervention_completed,
                satisfaction, satisfaction_completed,
                frustration, frustration_completed,
                nuisance, nuisance_completed,
                repeated_complaint, repeated_complaint_completed,
                next_best_action, next_best_action_completed,
                open_questions, open_questions_completed,
                pii_details, pii_details_completed,
                "Completed"
            FROM "crm-ai-db" 
            WHERE sid=%s
        ''', (call_sid,))
        result = cursor.fetchone()
        cursor.close()
        
        if result:
            # Build response data from database
            import json
            call_data = {
                "sid": result[0],
                "call_status": result[1],
                "transcript": result[2],
                "transcript_status": result[3],
                "summary": result[4],
                "summary_completed": result[5],
                "information_requested": result[6],
                "information_requested_completed": result[7],
                "threat": json.loads(result[8]) if result[8] else {},
                "threat_completed": result[9],
                "priority": json.loads(result[10]) if result[10] else {},
                "priority_completed": result[11],
                "human_intervention": json.loads(result[12]) if result[12] else {},
                "human_intervention_completed": result[13],
                "satisfaction": json.loads(result[14]) if result[14] else {},
                "satisfaction_completed": result[15],
                "frustration": json.loads(result[16]) if result[16] else {},
                "frustration_completed": result[17],
                "nuisance": json.loads(result[18]) if result[18] else {},
                "nuisance_completed": result[19],
                "repeated_complaint": json.loads(result[20]) if result[20] else {},
                "repeated_complaint_completed": result[21],
                "next_best_action": result[22],
                "next_best_action_completed": result[23],
                "open_questions": json.loads(result[24]) if result[24] else [],
                "open_questions_completed": result[25],
                "pii_details": json.loads(result[26]) if result[26] else {},
                "pii_details_completed": result[27],
                "completed": result[28]
            }
            return call_data
        return None
    except Exception as e:
        print(f"DB fetch error: {e}")
        return None

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
        
        # Store call context for this specific call using call_sid as key
        call_sid = result.get("call_sid")
        call_status = result.get("status")
        
        if call_sid and call_sid != "unknown":
            # Create the context dictionary
            call_context = {
                "phone_number": phone_number,
                "app_name": app_name,
                "reason": reason,
                "language": language,
                "client_name": client_name,
                "call_sid": call_sid,
            }
            
            # Store context keyed by call_sid for later retrieval in WebSocket
            _call_contexts[call_sid] = call_context
            
            # Save to PostgreSQL database for multi-worker support
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

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handle WebSocket connection from Exotel Media Streams."""
    await websocket.accept()
    print("WebSocket connected")

    try:
        from bot import bot
        from pipecat.runner.types import WebSocketRunnerArguments
        from pipecat.runner.utils import parse_telephony_websocket
        
        runner_args = WebSocketRunnerArguments(websocket=websocket)
        runner_args.handle_sigint = False

        # Pass the entire contexts dictionary to bot for lookup
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

# Background processing function (from api.py)
def process_call_background(call_sid):
    """
    Process the call in the background.
    CRITICAL: Do NOT store ANYTHING in database until ALL steps are successful.
    If ANY error occurs (audio download, transcription, analysis, etc.), 
    the call record must NOT be created or modified in the database.
    """
    conn = None
    
    try:
        # Import the processing functions from working_pipeline
        
        
        print(f"[Background] Starting processing for call: {call_sid}")
        
        # ============ STEP 1: Fetch call info from Exotel ============
        print(f"[Background] Fetching call info from Exotel for {call_sid}")
        call_data = get_call_info(call_sid)
        if not call_data:
            print(f"[Background] ❌ Call {call_sid} not found on Exotel - ABORTING (no DB changes)")
            return
        
        print(f"[Background] ✅ Call data retrieved: {call_data}")
        
        # ============ STEP 2: Check if recording exists ============
        recording_url = call_data.get("RecordingUrl")
        if not recording_url:
            print(f"[Background] ❌ Recording not available for call {call_sid} - ABORTING (no DB changes)")
            return
        
        print(f"[Background] ✅ Recording URL found")
        
        # ============ STEP 3: Download audio ============
        print(f"[Background] Downloading audio for {call_sid}")
        audio_path = f"/tmp/{call_sid}.mp3"
        if not download_audio(recording_url, audio_path):
            print(f"[Background] ❌ Failed to download recording for {call_sid} - ABORTING (no DB changes)")
            return
        
        print(f"[Background] ✅ Audio downloaded to {audio_path}")
        
        # ============ STEP 4: Transcribe audio ============
        print(f"[Background] Transcribing audio for {call_sid}")
        transcript = transcribe_audio(audio_path)
        if not transcript:
            print(f"[Background] ❌ Failed to transcribe audio for {call_sid} - ABORTING (no DB changes)")
            return
        
        print(f"[Background] ✅ Transcription completed for {call_sid}")
        
        # ============ STEP 5: Analyze transcript with OpenAI ============
        print(f"[Background] Analyzing transcript for {call_sid}")
        structured_analysis = analyze_transcript_with_openai(transcript)
        if not structured_analysis:
            print(f"[Background] ❌ Failed to analyze transcript for {call_sid} - ABORTING (no DB changes)")
            return
        structured_analysis = restructure_analysis(structured_analysis)
        
        print(f"[Background] ✅ Analysis completed for {call_sid}")
        
        # ============ STEP 6: ALL SUCCESSFUL - NOW SAVE TO DATABASE ============
        print(f"[Background] ✅ All processing steps successful - saving to database")
        
        # Get database connection ONLY after all processing is done
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT", "5432"),
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD")
        )
        
        # Insert the call record with all processed data
        if not insert_call_record(conn, call_sid):
            print(f"[Background] ❌ Failed to insert call record for {call_sid}")
            conn.close()
            return
        
        # Save all the analysis data in a single transaction
        save_structured_analysis_to_db(conn, call_sid, transcript, structured_analysis)
        
        conn.close()
        print(f"[Background] ✅ Successfully completed processing and saved to database for {call_sid}")
    
    except Exception as e:
        print(f"[Background] ❌ Unexpected error processing call {call_sid}: {str(e)}")
        print(f"[Background] NO DATABASE CHANGES WERE MADE due to error")
        # Close connection without making any changes
        if conn:
            try:
                conn.close()
            except:
                pass
    
    finally:
        # Clean up temporary audio file
        audio_path = f"/tmp/{call_sid}.wav"
        if os.path.exists(audio_path):
            try:
                os.remove(audio_path)
                print(f"[Background] Cleaned up audio file: {audio_path}")
            except Exception as e:
                print(f"[Background] Failed to clean up audio file: {e}")

@app.post("/process-call", response_model=CallProcessDetailedResponse)
async def process_call(request: CallProcessRequest):
    """
    Process or fetch status of a call.
    
    CRITICAL BEHAVIOR:
    - NEW CALLS (not in DB): Validate call exists and recording available, start async processing
    - EXISTING CALLS (in DB): Return current status from database
    - If validation fails or any error during processing: NO database changes are made
    
    Payload:
    {
        "call_sid": "string"
    }
    
    Returns:
    {
        "status": "processing" | "completed" | "call-recording-yet-generate" | etc,
        "message": "string",
        "call_sid": "string",
        "data": { call data from database } OR null
    }
    """
    from working_pipeline import (
        get_call_info,
        check_sid_exists
    )
    
    call_sid = request.call_sid
    
    if not call_sid:
        raise HTTPException(status_code=400, detail="call_sid is required")
    
    try:
        # Get database connection
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT", "5432"),
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD")
        )
        
        # Check if SID exists in database
        if check_sid_exists(conn, call_sid):
            # SID already exists - return current status and data from database
            print(f"[API] Call {call_sid} already exists in database - returning cached status")
            
            call_data = get_call_data_from_db(conn, call_sid)
            conn.close()
            
            if call_data:
                current_status = call_data.get("call_status", "unknown")
                is_completed = call_data.get("completed", False)
                
                # If processing is still ongoing or has failed (not completed)
                if not is_completed:
                    return CallProcessDetailedResponse(
                        status="processing",
                        message="Call is still being processed. Please try again later.",
                        call_sid=call_sid,
                        data=None
                    )
                
                # Processing completed successfully - return full data
                return CallProcessDetailedResponse(
                    status=current_status,
                    message=f"Call processing completed",
                    call_sid=call_sid,
                    data=call_data
                )
            else:
                conn.close()
                raise HTTPException(status_code=500, detail="Failed to fetch call data from database")
        
        else:
            # SID doesn't exist in database - validate call BEFORE starting processing
            print(f"[API] New call {call_sid} - validating call exists and has recording")
            
            # Fetch call info from Exotel to validate
            call_data = get_call_info(call_sid)
            if not call_data:
                conn.close()
                print(f"[API] Call {call_sid} not found on Exotel - NOT creating database record")
                return CallProcessDetailedResponse(
                    status="call-recording-yet-generate",
                    message="Call not found on Exotel or recording not yet available.",
                    call_sid=call_sid,
                    data=None
                )
            
            print(f"[API] Call data retrieved from Exotel: {call_data}")
            
            # Check if recording exists
            recording_url = call_data.get("RecordingUrl")
            if not recording_url:
                conn.close()
                print(f"[API] Recording not available for call {call_sid} - NOT creating database record")
                return CallProcessDetailedResponse(
                    status="call-recording-yet-generate",
                    message="Recording not yet available for this call. Please try again later.",
                    call_sid=call_sid,
                    data=None
                )
            
            # ✅ Validation passed - ready to process
            print(f"[API] Call {call_sid} validated - recording exists, starting background processing")
            conn.close()
            
            # Start background processing in a separate thread
            # NOTE: Database insertion happens ONLY after ALL processing steps succeed in the background
            processing_thread = Thread(target=process_call_background, args=(call_sid,), daemon=True)
            processing_thread.start()
            
            print(f"[API] Call {call_sid} scheduled for background processing (DB record created only on success)")
            
            return CallProcessDetailedResponse(
                status="processing",
                message="Call scheduled for processing. Recording is being processed.",
                call_sid=call_sid,
                data=None
            )
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"[API] Error in process_call for {call_sid}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error processing call: {str(e)}")

if __name__ == "__main__":
    # Get the number of workers from the environment variable or calculate based on CPU cores
    workers = int(os.getenv("UVICORN_WORKERS", (2 * multiprocessing.cpu_count()) + 1))

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=7862,
        workers=workers  # Specify the number of workers
    )

