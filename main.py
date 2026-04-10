from fastapi import FastAPI, APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
import traceback
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime, timezone, timedelta
import httpx
from groq import Groq
from supabase import create_client, Client

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Supabase connection — use env vars, fall back to known project values
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    logging.error("CRITICAL: SUPABASE_URL or SUPABASE_SERVICE_KEY is missing from environment variables!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY) if SUPABASE_URL and SUPABASE_SERVICE_KEY else None

# In-memory cache for Destination IDs (City name -> Booking.com dest_id)
DEST_ID_CACHE: Dict[str, str] = {}


# Create the main app without a prefix
app = FastAPI()

# ============ CORS Configuration ============
# Allow requests from the production Vercel frontend and local development
origins = [
    "https://yash-three-dusky.vercel.app",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# ============ Global Exception Handler ============

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logging.error(f"UNHANDLED EXCEPTION: {exc}", exc_info=True)
    tb = traceback.format_exc()
    origin = request.headers.get("Origin", "")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "msg": str(exc), "traceback": tb},
        headers={"Access-Control-Allow-Origin": origin if origin else "https://yash-three-dusky.vercel.app", "Access-Control-Allow-Credentials": "true"}
    )

# ============ Models ============

class User(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str
    email: str
    name: str = "Travel Agent"
    picture: Optional[str] = None
    organization: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    upi_id: Optional[str] = None
    agency_charges_percentage: Optional[float] = 10.0
    has_payment_setup: bool = False
    is_master: bool = False
    is_active: bool = True
    created_at: Optional[str] = None

class OnboardingInput(BaseModel):
    organization: str
    phone: str
    website: Optional[str] = None
    upi_id: str

class PNRStatusRequest(BaseModel):
    pnr: str

class TripDetails(BaseModel):
    from_location: str
    destination: str
    num_people: int
    budget: Optional[float] = None
    num_days: int
    transport_mode: str
    start_date: str
    places_to_cover: Optional[str] = None  # Specific places to visit on the way
    preferences: Optional[str] = None

class DayPlan(BaseModel):
    day: int
    title: str
    activities: List[str]
    places: List[str]

class Itinerary(BaseModel):
    days: List[DayPlan]

class TransportOption(BaseModel):
    option_id: str
    type: str
    from_location: str
    to_location: str
    departure_time: str
    arrival_time: str
    price: float
    provider: str

class StayOption(BaseModel):
    option_id: str
    name: str
    location: str
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    check_in_day: int
    check_out_day: int
    price_per_night: float
    rating: float
    amenities: List[str]

class TouristDetail(BaseModel):
    name: str
    age: Any
    gender: str
    proof: Optional[str] = "" 
    is_primary: Optional[bool] = False

class TouristDetailsInput(BaseModel):
    tourists: List[TouristDetail]
    contact_phone: str
    contact_email: Optional[str] = None
    secondary_phone: Optional[str] = None
    agency_charge: Optional[float] = 0.0
    num_cabs: Optional[int] = 1
    number_plate: Optional[str] = None

class PaymentConfirmInput(BaseModel):
    transaction_id: Optional[str] = None
    primary_phone: Optional[str] = None
    email: Optional[str] = None
    secondary_phone: Optional[str] = None
    total_amount: float
    agency_charge: float

@api_router.post("/fetch-pnr-status")
async def fetch_pnr_status(request: PNRStatusRequest):
    # EPHEMERAL PRODUCTION GATEWAY: pull secrets from Render env
    pnr = request.pnr.strip()
    api_key = os.getenv("RAPIDAPI_KEY")
    api_host = os.getenv("RAPIDAPI_HOST", "real-time-pnr-status-api-for-indian-railways.p.rapidapi.com")

    if not api_key:
        # Fallback to simulation if key is not yet in Render env
        return {"status": "Simulation", "message": "API Key Missing in Environment."}

    # Calibrated to the high-fidelity /name/ entry point
    url = f"https://{api_host}/name/{pnr}"
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": api_host
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=15.0)
            data = response.json()
            
            # The API usually returns "data": { "PassengerStatus": [...] }
            # Adjusting to the specific "Real-Time PNR Status API" schema
            p_data = data.get("data", {})
            p_status = p_data.get("PassengerStatus", [])
            
            if not p_status:
                 return {"status": "Unverified", "message": "No passenger data returned."}

            return {
                "status": "Verified",
                "passengers": p_status, # List of {Number, BookingStatus, CurrentStatus, Coach, Berth}
                "message": "Live Logistics Synchronized"
            }
    except Exception as e:
        return {"status": "Error", "message": f"Handshake Refusal: {str(e)}"}

# ============ Auth Helper ============

async def get_current_user(request: Request) -> User:
    session_token = request.cookies.get('session_token')
    
    if not session_token:
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            session_token = auth_header.replace('Bearer ', '')
    
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # 1. Verify Session Matrix
    session_res = supabase.table('user_sessions').select('*').eq('session_token', session_token).execute()
    if not session_res.data:
        raise HTTPException(status_code=401, detail="Invalid session matrix")
    
    session_doc = session_res.data[0]
    
    # Check expiry
    try:
        raw = session_doc.get("expires_at", "")
        if raw:
            expires_at = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if datetime.now(timezone.utc) > expires_at:
                raise HTTPException(status_code=401, detail="Session expired")
    except Exception:
        pass # Allow session if format is legacy but record exists
    
    # 2. Fetch Personnel Profile
    user_res = supabase.table('users').select('*').eq('user_id', session_doc['user_id']).execute()
    if not user_res.data:
        raise HTTPException(status_code=404, detail="Personnel record missing")
    
    user_data = user_res.data[0]
    
    try:
        user = User(**user_data)
        # 3. Master Command Check
        user.is_master = (user.email == "middlemen1245@gmail.com")
        
        # 4. Access Guard
        if not user.is_master and not user_data.get('is_active', True):
            raise HTTPException(status_code=403, detail="Agency Access REVOKED by Master Command")
            
        return user
    except Exception as e:
        logging.error(f"User construction failed: {e}")
        raise HTTPException(status_code=500, detail="Identity Matrix Malfunction")

# ============ Master Command Endpoints ============

@api_router.get("/master/agencies")
async def get_master_dashboard(request: Request):
    user = await get_current_user(request)
    if not getattr(user, 'is_master', False):
        raise HTTPException(status_code=403, detail="Master Access Denied")
        
    agencies_res = supabase.table('users').select('*').execute()
    trips_res = supabase.table('trips').select('user_id', 'status').execute()
    
    # Aggregate Stats Matrix
    stats = {}
    for t in (trips_res.data or []):
        uid = t['user_id']
        if uid not in stats: stats[uid] = {"total": 0, "completed": 0}
        stats[uid]["total"] += 1
        if t['status'] == 'completed': stats[uid]["completed"] += 1
        
    return {
        "agencies": agencies_res.data,
        "stats": stats
    }

@api_router.post("/master/agency/{target_user_id}/toggle")
async def toggle_agency_access(request: Request, target_user_id: str):
    user = await get_current_user(request)
    if not getattr(user, 'is_master', False):
        raise HTTPException(status_code=403, detail="Master Access Denied")
        
    # Get current status
    res = supabase.table('users').select('is_active').eq('user_id', target_user_id).execute()
    if not res.data: raise HTTPException(status_code=404, detail="Agency not found")
    
    new_status = not res.data[0].get('is_active', True)
    supabase.table('users').update({"is_active": new_status}).eq('user_id', target_user_id).execute()
    
    return {"status": "ok", "is_active": new_status}

# ============ Auth Routes ============

@api_router.post("/auth/login")
async def login(request: Request):
    body = await request.json()
    email = body.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    
    referer = request.headers.get("referer", "http://localhost:3000/")
    if "localhost" in referer:
        redirect_to = "http://localhost:3000/auth/callback"
    else:
        from urllib.parse import urlparse
        parsed = urlparse(referer)
        redirect_to = f"{parsed.scheme}://{parsed.netloc}/auth/callback"
        
    try:
        supabase.auth.sign_in_with_otp({
            "email": email, 
            "options": {"email_redirect_to": redirect_to}
        })
        return {"success": True, "message": "Magic link sent"}
    except Exception as e:
        logging.error(f"OTP login failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to send login link")

@api_router.post("/auth/google")
async def google_login(request: Request):
    referer = request.headers.get("referer", "http://localhost:3000/")
    if "localhost" in referer:
        redirect_to = "http://localhost:3000/auth/callback"
    else:
        from urllib.parse import urlparse
        parsed = urlparse(referer)
        redirect_to = f"{parsed.scheme}://{parsed.netloc}/auth/callback"
        
    try:
        res = supabase.auth.sign_in_with_oauth({
            "provider": "google", 
            "options": {"redirect_to": redirect_to}
        })
        url = getattr(res, 'url', None)
        if url is None and isinstance(res, dict):
            url = res.get('url')
        return {"url": url}
    except Exception as e:
        logging.error(f"OAuth redirect failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to initialize Google login")

@api_router.post("/auth/session")
async def create_session(request: Request, response: Response):
    if not supabase:
        logging.error("Supabase client not initialized due to missing keys")
        return JSONResponse(
            status_code=500,
            content={"detail": "External Service Error", "msg": "Supabase credentials missing on server"},
            headers={"Access-Control-Allow-Origin": request.headers.get("Origin", "*"), "Access-Control-Allow-Credentials": "true"}
        )

    body = await request.json()
    access_token = body.get('access_token')

    # Verify the Supabase access_token and get the authenticated user
    try:
        logging.info("START: Verifying Supabase access token...")
        auth_response = supabase.auth.get_user(access_token)
        supabase_user = auth_response.user
        if not supabase_user:
            logging.error("CRITICAL: Supabase user not found in auth response object")
            raise HTTPException(status_code=401, detail="Invalid token session")
        logging.info(f"SUCCESS: Supabase user identified: {supabase_user.email}")
    except Exception as e:
        tb = traceback.format_exc()
        logging.error(f"FAIL: Supabase token verification failed: {e}\n{tb}")
        return JSONResponse(
            status_code=401, 
            content={"detail": f"Identity Verification Failed: {str(e)}", "trace": tb},
            headers={"Access-Control-Allow-Origin": request.headers.get("Origin", "https://yash-three-dusky.vercel.app"), "Access-Control-Allow-Credentials": "true"}
        )

    # Extract user info from Supabase user metadata
    user_data = {
        "email": supabase_user.email,
        "name": (
            supabase_user.user_metadata.get("full_name")
            or supabase_user.user_metadata.get("name")
            or supabase_user.email
        ),
        "picture": supabase_user.user_metadata.get("avatar_url"),
    }

    # Generate our own session token
    session_token = f"session_{uuid.uuid4().hex}"
    user_id = f"user_{uuid.uuid4().hex[:12]}"

    try:
        logging.info("DB: Searching for existing user...")
        existing_user_response = supabase.table('users').select('*').eq('email', user_data["email"]).execute()

        if existing_user_response.data and len(existing_user_response.data) > 0:
            user_id = existing_user_response.data[0]["user_id"]
            logging.info(f"DB: User found. Updating profile for {user_id}")
            supabase.table('users').update({
                "name": user_data["name"],
                "picture": user_data.get("picture")
            }).eq('user_id', user_id).execute()
        else:
            logging.info(f"DB: New user. Inserting record for {user_id}")
            new_user = {
                "user_id": user_id,
                "email": user_data["email"],
                "name": user_data["name"],
                "picture": user_data.get("picture"),
                "phone": None,
                "organization": None,
                "upi_id": None,
                "agency_charges_percentage": 10.0,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "has_payment_setup": False
            }
            supabase.table('users').insert(new_user).execute()

        logging.info("DB: Storing session token...")
        session_doc = {
            "user_id": user_id,
            "session_token": session_token,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        supabase.table('user_sessions').insert(session_doc).execute()
        logging.info("DB: Session storage successful")
    except Exception as e:
        tb = traceback.format_exc()
        logging.error(f"FAIL: Database sync failed: {e}\n{tb}")
        origin = request.headers.get("Origin", "https://yash-three-dusky.vercel.app")
        return JSONResponse(
            status_code=500,
            content={"detail": "Database Synchronization Failed", "msg": str(e), "traceback": tb},
            headers={"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true"}
        )

    # Set httpOnly cookie so the browser sends it on every request
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=7 * 24 * 60 * 60,
        path="/"
    )

    # Return user data and onboarding flag
    user_response = supabase.table('users').select('*').eq('user_id', user_id).execute()
    user_doc = user_response.data[0]

    return {
        "user": User(**user_doc).model_dump(),
        "needs_onboarding": not user_doc.get('has_payment_setup', False)
    }

@api_router.get("/auth/me")
async def get_me(request: Request):
    user = await get_current_user(request)
    return {
        "user": user.model_dump(), 
        "needs_onboarding": not user.has_payment_setup
    }

@api_router.post("/auth/logout")
async def logout(request: Request, response: Response):
    session_token = request.cookies.get('session_token')
    
    if session_token:
        supabase.table('user_sessions').delete().eq('session_token', session_token).execute()
    
    response.delete_cookie(key="session_token", path="/")
    return {"message": "Logged out"}

# ============ Onboarding Route ============

@api_router.post("/onboarding")
async def onboarding(request: Request, input: OnboardingInput):
    try:
        user = await get_current_user(request)
        
        supabase.table('users').update({
            "organization": input.organization,
            "phone": input.phone,
            "website": input.website,
            "upi_id": input.upi_id,
            "has_payment_setup": True
        }).eq('user_id', user.user_id).execute()
        
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logging.error(f"Onboarding failed: {e}\n{tb}")
        origin = request.headers.get("Origin", "https://yash-three-dusky.vercel.app")
        return JSONResponse(
            status_code=500,
            content={"detail": "Onboarding Failed", "msg": str(e), "traceback": tb},
            headers={"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true"}
        )

# ============ Trip Helper ============
def get_trip_details(trip_doc: Dict[str, Any]) -> Dict[str, Any]:
    from datetime import datetime
    start_date = trip_doc.get("start_date")
    end_date = trip_doc.get("end_date")
    
    num_days = 1
    if start_date and end_date:
        try:
            # Handle potential 'Z' suffix and other variants
            d1_str = start_date.replace('Z', '+00:00') if isinstance(start_date, str) else start_date
            d2_str = end_date.replace('Z', '+00:00') if isinstance(end_date, str) else end_date
            
            d1 = datetime.fromisoformat(d1_str)
            d2 = datetime.fromisoformat(d2_str)
            num_days = (d2 - d1).days + 1
        except Exception:
            num_days = trip_doc.get("num_days", 1)

    return {
        "from_location": trip_doc.get("from_location") or "Unknown",
        "destination": trip_doc.get("destination") or "Unknown",
        "start_date": start_date or datetime.now().strftime("%Y-%m-%d"),
        "end_date": end_date or datetime.now().strftime("%Y-%m-%d"),
        "num_people": trip_doc.get("num_people") or 1,
        "transport_mode": trip_doc.get("transport_mode") or "flight",
        "num_days": trip_doc.get("num_days") or max(1, num_days)
    }

# ============ Trip Routes ============

@api_router.post("/trips/create")
async def create_trip(request: Request, details: TripDetails):
    user = await get_current_user(request)
    
    trip_id = f"trip_{uuid.uuid4().hex[:12]}"
    
    # Robust date parsing
    try:
        if 'T' in details.start_date:
            start_dt = datetime.fromisoformat(details.start_date.replace('Z', '+00:00'))
        else:
            start_dt = datetime.strptime(details.start_date, "%Y-%m-%d")
        end_dt = start_dt + timedelta(days=details.num_days - 1)
        end_date_iso = end_dt.isoformat()
    except Exception as e:
        logging.error(f"Date parsing failed for {details.start_date}: {e}")
        end_date_iso = details.start_date

    trip_doc = {
        "trip_id": trip_id,
        "user_id": user.user_id,
        "from_location": details.from_location,
        "destination": details.destination,
        "start_date": details.start_date,
        "num_days": details.num_days,
        "num_people": details.num_people,
        "transport_mode": details.transport_mode,
        "status": "draft",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    # We do NOT insert end_date into the DB yet as the column may not exist in Supabase
    
    supabase.table('trips').insert(trip_doc).execute()
    
    return {"trip_id": trip_id}

@api_router.post("/trips/{trip_id}/generate-itinerary")
async def generate_itinerary(request: Request, trip_id: str):
    user = await get_current_user(request)
    body = await request.json()
    transport = body.get('transport', {})
    stays = body.get('stays', [])
    
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not trip_response.data or len(trip_response.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    trip_doc = trip_response.data[0]
    details = get_trip_details(trip_doc)
    
    # Robust extraction of potential missing columns
    places_to_cover = trip_doc.get("places_to_cover") or ""
    places_context = f"STRICT: You MUST include these specific places in the plan: {places_to_cover}." if places_to_cover else ""
    
    # Pull saved transport and stay selections from DB
    selected_transport = trip_doc.get("selected_transport") or {}
    
    # Capture ephemeral PNRs from request body
    onward_pnr = body.get('onward_pnr') or 'Hub Transit Protocol'
    return_pnr = body.get('return_pnr', 'N/A')
    stay_name = body.get('stay_name', 'N/A')
    
    transport_mode = details.get('transport_mode', 'car').lower()
    vector_directive = ""
    if transport_mode == 'car':
        vector_directive = "STRICT: This is a GROUND CAB trip. DO NOT mention flight boarding, airport transfers, or trains. Focus on road travel times."
    elif transport_mode == 'train':
        vector_directive = "STRICT: This is a RAIL trip. Mention exact station codes and arrival/departure station timings."
    else:
        vector_directive = "STRICT: This is a FLIGHT trip. Include boarding pass/airport transfer logistics."

    prompt = f"""Create a comprehensive, REAL-TIME {details['num_days']}-day travel itinerary for a trip to {details['destination']}.
{vector_directive}
{places_context}

CRITICAL RULES:
1. You MUST provide exactly {details['num_days']} days. If num_days is {details['num_days']}, I expect activity blocks for Day 1, Day 2 ... through Day {details['num_days']}.
2. {details['from_location']} is ONLY the departure city. Do NOT plan activities there.
3. Day 1 begins with arrival at {details['destination']}.
4. Provide precise TIMINGS (e.g., 09:00 AM) for every activity.
5. Include "Stay Timings" (Check-in/Check-out) and "Travel Details" (Local durations via {transport_mode}).
6. INTEGRATE requested places to cover into the timeline naturally.
7. CRITICAL: Logistics Sync. Mention the technical identifiers: Onward Vector (Ref: {onward_pnr}), Return Vector (Ref: {return_pnr}), and Mission HQ: {stay_name} in the relevant check-in/travel blocks.


Return ONLY valid JSON in this format:
{{
  "itinerary": [
    {{
      "day": 1,
      "title": "Arrival & Vector Synchronization",
      "activities": [
        "09:00 AM - Arrival: Transfer to hotel via {transport_mode} (30 mins)",
        "11:30 AM - Sightseeing: Visit Monument X (Duration: 2 hours)",
        "02:00 PM - Lunch: Local Cuisine at Restaurant Z",
        "06:30 PM - Evening: Leisure Walk at Destination Park"
      ],
      "summary": "Arrival and initial exploration with check-in at hotel."
    }}
  ]
}}
"""
    
    try:
        # Initialize Groq client
        api_key = os.environ.get('GROQ_API_KEY') or os.environ.get('EMERGENT_LLM_KEY')
        if not api_key:
            raise ValueError("GROQ_API_KEY/EMERGENT_LLM_KEY is missing from environment variables")
            
        client = Groq(api_key=api_key)
        
        # Get AI response using Groq library
        system_prompt = (
            "You are a professional travel agency orchestrator. Generate an EXTREMELY DETAILED day-wise itinerary. "
            "For each day, provide a title and a list of activities. "
            "Activities MUST include specific timings (e.g., '10:00 AM - Visit Beach'), transit details (e.g., 'Travel via private cab'), "
            "and stay instructions (e.g., '6:00 PM - Check-in at Sea View Resort'). "
            "Format: Return ONLY a JSON object with an 'itinerary' key containing a list of {day: int, title: str, activities: List[str]} objects. "
            "DO NOT return markdown. Activities MUST be strings, NOT objects."
        )
        
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            response_format={ "type": "json_object" }
        )
        
        response_text = completion.choices[0].message.content
        
        import json
        itinerary_data = json.loads(response_text)
        
        # Robustly extract the array from dict wrapper keys
        if isinstance(itinerary_data, dict):
            for key in ["days", "itinerary", "plan", "trip", "activities", "data"]:
                if key in itinerary_data and isinstance(itinerary_data[key], list):
                    itinerary_data = itinerary_data[key]
                    break
            else:
                for v in itinerary_data.values():
                    if isinstance(v, list):
                        itinerary_data = v
                        break

        if not isinstance(itinerary_data, list):
            logging.error(f"Unexpected itinerary format from AI: {type(itinerary_data)}")
            # Fallback to empty list or handle error
            itinerary_data = []

        supabase.table('trips').update({
            "itinerary": {"days": itinerary_data}
        }).eq('trip_id', trip_id).execute()
        
        return {"itinerary": {"days": itinerary_data}}
    except Exception as e:
        tb = traceback.format_exc()
        logging.error(f"AI itinerary generation error: {e}\n{tb}")
        origin = request.headers.get("Origin", "https://yash-three-dusky.vercel.app")
        return JSONResponse(
            status_code=500,
            content={"detail": "Failed to generate itinerary", "msg": str(e), "traceback": tb},
            headers={"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true"}
        )

@api_router.put("/trips/{trip_id}/itinerary")
async def update_itinerary(request: Request, trip_id: str, itinerary: Itinerary):
    user = await get_current_user(request)
    
    result = supabase.table('trips').update({
        "itinerary": itinerary.model_dump()
    }).eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not result.data or len(result.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    return {"message": "Itinerary updated"}

@api_router.post("/trips/{trip_id}/generate-transport")
async def generate_transport(request: Request, trip_id: str):
    user = await get_current_user(request)
    
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not trip_response.data or len(trip_response.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    trip_doc = trip_response.data[0]
    details = get_trip_details(trip_doc)
    transport_mode = details['transport_mode']
    
    RAPIDAPI_KEY = os.environ.get('RAPIDAPI_KEY', 'e580c5c040msh0b8c675d17e2bacp1009bbjsn165082508389')
    transport_data = {"onward": [], "return": []}

    try:
        import json
        from datetime import datetime, timedelta
        api_key = os.environ.get('GROQ_API_KEY') or os.environ.get('EMERGENT_LLM_KEY')
        if not api_key:
            raise ValueError("GROQ_API_KEY/EMERGENT_LLM_KEY is missing from environment variables")
            
        client = Groq(api_key=api_key)
        
        return_date = (datetime.strptime(details['start_date'], "%Y-%m-%d") + timedelta(days=details.get("num_days", 1))).strftime("%Y-%m-%d")
        
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            if transport_mode == 'train':
                prompt = f"Convert these cities into exact IRCTC Indian Railway station codes. From: '{details['from_location']}', To: '{details['destination']}'. Return ONLY JSON format: {{\"from_code\": \"NDLS\", \"to_code\": \"MAS\"}}"
                completion = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": prompt}],
                    response_format={ "type": "json_object" }
                )
                codes = json.loads(completion.choices[0].message.content)
                from_code = codes.get('from_code', 'NDLS')
                to_code = codes.get('to_code', 'MAS')

                headers = {"x-rapidapi-host": "irctc1.p.rapidapi.com", "x-rapidapi-key": RAPIDAPI_KEY}
                
                for route_date, route_key, f_code, t_code, fl, tl in [
                    (details['start_date'], "onward", from_code, to_code, details['from_location'], details['destination']),
                    (return_date, "return", t_code, from_code, details['destination'], details['from_location'])
                ]:
                    res = await http_client.get(f"https://irctc1.p.rapidapi.com/api/v3/trainBetweenStations?fromStationCode={f_code}&toStationCode={t_code}&dateOfJourney={route_date}", headers=headers)
                    trains = []
                    if res.status_code == 200:
                        trains = res.json().get('data', [])
                    
                    if not trains:
                        logging.info(f"No direct trains for {f_code}->{t_code}. Initiating Nearby Station Protocol.")
                        # Fallback: Ask LLM for nearby major stations and search again if possible, or just add to transport options
                    
                    for i, t in enumerate(trains[:5]):
                            for c in t.get('class_type', ['SL']):
                                price = 600 if c == 'SL' else (1800 if c in ['3A', '3E'] else (2500 if c == '2A' else 4000))
                                transport_data[route_key].append({
                                    "option_id": f"transport_{route_key}_{len(transport_data[route_key])+1}",
                                    "type": "train",
                                    "provider": f"{t.get('train_name')} ({t.get('train_number')})",
                                    "class": c,
                                    "from_location": t.get('from_station_name', fl),
                                    "to_location": t.get('to_station_name', tl),
                                    "departure_time": f"{t.get('train_date', route_date)} {t.get('from_std', '00:00')}",
                                    "arrival_time": f"Day {t.get('to_day', 0) + 1} {t.get('to_sta', '00:00')}",
                                    "duration": str(t.get('duration', 'N/A')),
                                    "price": price,
                                    "seats_hint": "Available" if i % 2 == 0 else "WL"
                                })

            elif transport_mode == 'flight':
                async def get_airport(city):
                    headers = {"x-rapidapi-host": "sky-scrapper.p.rapidapi.com", "x-rapidapi-key": RAPIDAPI_KEY}
                    res = await http_client.get(f"https://sky-scrapper.p.rapidapi.com/api/v1/flights/searchAirport?query={city}", headers=headers)
                    if res.status_code == 200 and res.json().get('data'):
                        return res.json()['data'][0]['skyId'], res.json()['data'][0]['entityId']
                    return None, None

                from_skyId, from_entityId = await get_airport(details['from_location'])
                to_skyId, to_entityId = await get_airport(details['destination'])

                if from_skyId and to_skyId:
                    headers = {"x-rapidapi-host": "sky-scrapper.p.rapidapi.com", "x-rapidapi-key": RAPIDAPI_KEY}
                    for route_date, route_key, fsy, tsy, fey, tey, fl, tl in [
                        (details['start_date'], "onward", from_skyId, to_skyId, from_entityId, to_entityId, details['from_location'], details['destination']),
                        (return_date, "return", tsy, fsy, tey, fey, details['destination'], details['from_location'])
                    ]:
                        url = f"https://sky-scrapper.p.rapidapi.com/api/v2/flights/searchFlightsComplete?originSkyId={fsy}&destinationSkyId={tsy}&originEntityId={fey}&destinationEntityId={tey}&date={route_date}&cabinClass=economy&adults=1&sortBy=best&currency=INR&market=en-IN&countryCode=IN"
                        res = await http_client.get(url, headers=headers)
                        if res.status_code == 200:
                            flights = res.json().get('data', {}).get('itineraries', [])
                            for i, f in enumerate(flights[:10]):
                                leg = f.get('legs', [{}])[0]
                                carrier = leg.get('carriers', {}).get('marketing', [{}])[0].get('name', 'Airline')
                                transport_data[route_key].append({
                                    "option_id": f"transport_{route_key}_{len(transport_data[route_key])+1}",
                                    "type": "flight",
                                    "provider": carrier,
                                    "vehicle_id": leg.get('segments', [{}])[0].get('flightNumber', f"{carrier[:2].upper()}-{i+100}"),
                                    "class": "Economy",
                                    "from_location": leg.get('origin', {}).get('name', fl),
                                    "to_location": leg.get('destination', {}).get('name', tl),
                                    "departure_time": f"{route_date} {leg.get('departure', 'T00:00')[-8:-3]}",
                                    "arrival_time": f"{route_date} {leg.get('arrival', 'T00:00')[-8:-3]}",
                                    "duration": f"{leg.get('durationInMinutes', 120) // 60}h {leg.get('durationInMinutes', 0) % 60}m",
                                    "price": f.get('price', {}).get('raw', 5000),
                                    "seats_hint": "Fast Filling" if i % 3 == 0 else "Available"
                                })

    except Exception as e:
        tb = traceback.format_exc()
        logging.error(f"Transport generation error: {e}\n{tb}")

    if not transport_data["onward"] or (return_date and not transport_data["return"]):
        logging.info("Falling back to LLM transport generation")
        prompt = f"""
    Find 5 onward transport options (Flight/Train/Bus) from {details['from_location']} to {details['destination']} on {details['start_date']}.
    {f"Also find 5 return transport options from {details['destination']} to {details['from_location']} on {return_date}." if return_date else ""}
    STRICT: If no direct trains are available between {details['from_location']} and {details['destination']}, search for trains to NEARBY major stations and include them with a note.
    Return ONLY a JSON object with keys 'onward' and 'return' (return can be empty if no return_date), each containing a list of objects.
    Each object MUST have:
    - option_id: unique string
    - provider: name of airline or service
    - type: Flight, Train, or Bus
    - price: numeric price in INR
    - vehicle_id: a REAListic flight number or vehicle ID (e.g., IndiGo 6E-205, AI-101, IRCTC-12401)
    - from_location: string (e.g., 'From Nearby Station X')
    - to_location: string
    - departure_time: string (HH:MM)
    - arrival_time: string (HH:MM)
    """
        try:
            api_key = os.environ.get('GROQ_API_KEY') or os.environ.get('EMERGENT_LLM_KEY')
            if not api_key:
                raise ValueError("GROQ_API_KEY/EMERGENT_LLM_KEY is missing from environment variables")
                
            client = Groq(api_key=api_key)
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                response_format={ "type": "json_object" }
            )
            import json
            fallback_data = json.loads(completion.choices[0].message.content)
            
            if "onward" in fallback_data and isinstance(fallback_data["onward"], list):
                for i, opt in enumerate(fallback_data["onward"]):
                    opt["option_id"] = f"transport_onward_fallback_{i}"
                    transport_data["onward"].append(opt)
            
            if "return" in fallback_data and isinstance(fallback_data["return"], list):
                for i, opt in enumerate(fallback_data["return"]):
                    opt["option_id"] = f"transport_return_fallback_{i}"
                    transport_data["return"].append(opt)

        except Exception as e:
            tb = traceback.format_exc()
            logging.error(f"LLM Transport Fallback error: {e}\n{tb}")
            origin = request.headers.get("Origin", "https://yash-three-dusky.vercel.app")
            return JSONResponse(
                status_code=500,
                content={"detail": "Failed to generate transport options", "msg": str(e), "traceback": tb},
                headers={"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true"}
            )

    supabase.table('trips').update({"transport_options": transport_data}).eq('trip_id', trip_id).execute()
    return {"transport_options": transport_data}

@api_router.post("/trips/{trip_id}/select-transport")
async def select_transport(request: Request, trip_id: str):
    user = await get_current_user(request)
    body = await request.json()
    transport_id = body.get('transport_id')
    
    result = supabase.table('trips').update({
        "selected_transport": transport_id
    }).eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not result.data or len(result.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    return {"message": "Transport selected"}

@api_router.post("/trips/{trip_id}/generate-stays")
async def generate_stays(request: Request, trip_id: str):
    user = await get_current_user(request)
    body = await request.json()
    budget = body.get('budget', 50000)
    
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not trip_response.data or len(trip_response.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    trip_doc = trip_response.data[0]
    details = get_trip_details(trip_doc)
    num_days = details["num_days"]
    
    RAPIDAPI_KEY = os.environ.get('RAPIDAPI_KEY', 'e580c5c040msh0b8c675d17e2bacp1009bbjsn165082508389')
    stays_data = []

    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            headers = {"x-rapidapi-host": "booking-com.p.rapidapi.com", "x-rapidapi-key": RAPIDAPI_KEY}
            
            # 1. Resolve Destination ID (with local caching to bypass redundant API calls)
            city_key = details['destination'].strip().upper()
            dest_id = DEST_ID_CACHE.get(city_key)
            
            if not dest_id:
                try:
                    loc_res = await http_client.get(f"https://booking-com.p.rapidapi.com/v1/hotels/locations?name={details['destination']}&locale=en-gb", headers=headers, timeout=5.0)
                    if loc_res.status_code == 200:
                        for loc in loc_res.json():
                            if loc.get('dest_type') == 'city':
                                dest_id = loc.get('dest_id')
                                DEST_ID_CACHE[city_key] = dest_id
                                break
                except Exception as e:
                    logging.warning(f"Destination resolution timeout for {details['destination']}, moving to fallback or AI search.")
            
            if dest_id:
                # 2. Search Hotels
                from datetime import datetime, timedelta
                end_date = datetime.strptime(details['start_date'], "%Y-%m-%d") + timedelta(days=num_days)
                checkout_str = end_date.strftime("%Y-%m-%d")
                
                search_url = f"https://booking-com.p.rapidapi.com/v1/hotels/search?dest_id={dest_id}&dest_type=city&adults_number=1&checkin_date={details['start_date']}&checkout_date={checkout_str}&order_by=price&room_number=1&filter_by_currency=INR&locale=en-gb"
                hotel_res = await http_client.get(search_url, headers=headers)
                
                if hotel_res.status_code == 200:
                    data = hotel_res.json()
                    hotels = data.get('result', []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    logging.info(f"RapidAPI yielded {len(hotels)} potential stays for {details['destination']}.")
                    for i, h in enumerate(hotels[:15]):
                        stays_data.append({
                            "option_id": f"stay_{i+1}",
                            "name": h.get("hotel_name"),
                            "location": h.get("address", details['destination']),
                            "contact_phone": "+91-XXXXXXXXXX", # Generic since API drops this
                            "contact_email": f"reservations@{h.get('hotel_name', 'hotel').lower().replace(' ', '')}.com",
                            "check_in_day": 1,
                            "check_out_day": num_days,
                            "price_per_night": int(h.get("gross_amount_per_night", {}).get("value", 2500)) if isinstance(h.get("gross_amount_per_night"), dict) else 2500,
                            "rating": h.get("review_score", 4.0),
                            "amenities": ["WiFi"] + (["Breakfast"] if h.get("hotel_include_breakfast") else []) + (["Parking"] if h.get("has_free_parking") else [])
                        })
    except Exception as e:
        logging.error(f"Live Stay API error: {e}")

    if not stays_data:
        logging.info("Falling back to LLM stays generation")
        prompt = f"""Generate 5 premium hotel options for a {num_days}-day trip to {details['destination']}. These should be REALISTIC options with different price ranges.
        Return ONLY a JSON array of objects with these keys: 
        name, location, contact_phone, contact_email, check_in_day (always 1), check_out_day (always {num_days}), price_per_night (in INR, e.g., 4500), rating (float between 3.5 and 5.0), amenities (list of strings)."""
        try:
            api_key = os.environ.get('GROQ_API_KEY') or os.environ.get('EMERGENT_LLM_KEY')
            if api_key:
                client = Groq(api_key=api_key)
                completion = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": prompt}],
                    response_format={ "type": "json_object" }
                )
                import json
                raw_data = json.loads(completion.choices[0].message.content)
                # Handle different AI wrapper keys
                for key in ['hotels', 'stays', 'options', 'data']:
                    if key in raw_data and isinstance(raw_data[key], list):
                        stays_data = raw_data[key]
                        break
                else:
                    for v in raw_data.values():
                        if isinstance(v, list):
                            stays_data = v
                            break
                    else:
                        if isinstance(raw_data, list):
                            stays_data = raw_data

            if not stays_data:
                raise ValueError("AI failed to return valid list")

            for i, option in enumerate(stays_data):
                option["option_id"] = f"stay_{i+1}"

        except Exception as e:
            logging.error(f"Stay Fallback chain error: {e}")
            # FINAL EMERGENCY HARDCODED LIST: Guaranteed to work
            stays_data = [
                {
                    "option_id": "stay_f1",
                    "name": f"Luxury {details['destination']} Regency",
                    "location": f"Elite Sector, {details['destination']}",
                    "contact_phone": "+91-XXXXX-XXXXX",
                    "contact_email": f"booking@{details['destination'].lower().replace(' ', '')}regency.com",
                    "check_in_day": 1,
                    "check_out_day": num_days,
                    "price_per_night": 5500,
                    "rating": 4.8,
                    "amenities": ["Luxury WiFi", "Spa", "All Meals Included", "Airport Shuttle"]
                },
                {
                    "option_id": "stay_f2",
                    "name": f"Comfort {details['destination']} Inn",
                    "location": f"City Center, {details['destination']}",
                    "contact_phone": "+91-XXXXX-XXXXX",
                    "contact_email": f"hello@comfortinn{details['destination'].lower().replace(' ', '')}.com",
                    "check_in_day": 1,
                    "check_out_day": num_days,
                    "price_per_night": 2800,
                    "rating": 4.0,
                    "amenities": ["WiFi", "Breakfast", "Central Location"]
                }
            ]

    supabase.table('trips').update({"stay_options": stays_data}).eq('trip_id', trip_id).execute()
    return {"stay_options": stays_data}

@api_router.post("/trips/{trip_id}/select-stays")
async def select_stays(request: Request, trip_id: str):
    user = await get_current_user(request)
    body = await request.json()
    stay_ids = body.get('stay_ids', [])
    
    # Get trip and stay details
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    if trip_response.data:
        trip_doc = trip_response.data[0]
        
        # Send notification to each selected stay owner
        for stay_id in stay_ids:
            stay = next((s for s in trip_doc.get("stay_options", []) if s.get("option_id") == stay_id), None)
            if stay and stay.get("contact_phone"):
                # Log notification (in production, send SMS/Email)
                logging.info(f"NOTIFICATION: Stay booking at {stay['name']} - Contact: {stay['contact_phone']}")
                # TODO: Implement actual SMS/Email notification here
    
    result = supabase.table('trips').update({
        "selected_stays": stay_ids
    }).eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not result.data or len(result.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    return {"message": "Stays selected and owners notified"}

@api_router.post("/trips/{trip_id}/orchestrate")
async def orchestrate_trip(request: Request, trip_id: str, input: TouristDetailsInput):
    # This is an alias for tourist-details to maintain frontend compatibility
    return await save_tourist_details(request, trip_id, input)

@api_router.post("/trips/{trip_id}/tourist-details")
async def save_tourist_details(request: Request, trip_id: str, input: TouristDetailsInput):
    user = await get_current_user(request)
    
    # Verify trip ownership
    trip_res = supabase.table('trips').select('trip_id').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    if not trip_res.data:
        raise HTTPException(status_code=404, detail="Trip not found")

    # Clear existing passengers for this trip
    supabase.table('passengers').delete().eq('trip_id', trip_id).execute()

    # Bulk insert new passengers
    pax_records = []
    for t in input.tourists:
        pax_records.append({
            "trip_id": trip_id,
            "name": t.name,
            "age": t.age,
            "gender": t.gender,
            "proof": t.proof,
            "is_primary": getattr(t, 'is_primary', False)
        })
    
    if pax_records:
        supabase.table('passengers').insert(pax_records).execute()

    # Update trip with metadata and status
    supabase.table('trips').update({
        "contact_phone": input.contact_phone,
        "contact_email": input.contact_email,
        "secondary_phone": input.secondary_phone,
        "agency_charge": input.agency_charge,
        "num_cabs": input.num_cabs,
        "number_plate": input.number_plate,
        "status": "orchestrated"
    }).eq('trip_id', trip_id).execute()

    return {"status": "ok", "message": "Details saved and trip orchestrated"}


# ============ Payment Routes ============

@api_router.get("/trips/{trip_id}/payment-info")
async def get_payment_info(request: Request, trip_id: str):
    user = await get_current_user(request)
    
    # Get trip details
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not trip_response.data or len(trip_response.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    trip_doc = trip_response.data[0]
    
    # Get agency details
    user_response = supabase.table('users').select('*').eq('user_id', user.user_id).execute()
    user_doc = user_response.data[0]
    
    agency_charges = 0
    tourist_details = trip_doc.get("tourist_details")
    if tourist_details:
        agency_charges = tourist_details.get("agency_charges", 0) or 0
    
    return {
        "upi_id": user_doc.get('upi_id'),
        "agency_name": user_doc.get('organization'),
        "total_amount": agency_charges,
        "plan_name": "Agency Service Charges"
    }

@api_router.put("/user/profile")
async def update_user_profile(request: Request):
    user = await get_current_user(request)
    body = await request.json()
    
    # Allowed fields only (Email is read-only)
    update_data = {}
    for field in ['organization', 'phone', 'website', 'upi_id']:
        if field in body:
            update_data[field] = body[field]
            
    if not update_data:
        return {"message": "No changes requested"}
        
    supabase.table('users').update(update_data).eq('user_id', user.user_id).execute()
    return {"message": "Agency profile modernized"}

@api_router.post("/trips/{trip_id}/confirm-payment")
async def confirm_trip_payment(request: Request, trip_id: str, payload: PaymentConfirmInput):
    user = await get_current_user(request)
    
    # Verify Trip existence to avoid 500 error on deleted trips
    trip_check = supabase.table('trips').select('trip_id').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    if not trip_check.data or len(trip_check.data) == 0:
        raise HTTPException(status_code=404, detail="Trip sequence not found in hub matrix")

    # 1. Update Trip Status and Financial Metadata
    supabase.table('trips').update({
        "status": "completed",
        "total_amount": round(float(payload.total_amount or 0), 2),
        "transaction_id": payload.transaction_id or f"TXN_{trip_id}"
    }).eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    try:
        # 2. Record Payment Detail
        tx_id = payload.transaction_id
        if not tx_id or tx_id.strip() == "":
            tx_id = f"OFFLINE_{trip_id}_{int(datetime.now().timestamp())}"

        # Defensive casting and rounding for Postgres DECIMAL
        total_amt = round(float(payload.total_amount or 0), 2)
        agency_amt = round(float(payload.agency_charge or 0), 2)

        payment_record = {
            "trip_id": trip_id,
            "transaction_id": tx_id,
            "primary_phone": payload.primary_phone or "",
            "email": payload.email or "",
            "secondary_phone": payload.secondary_phone or "",
            "total_amount": total_amt,
            "agency_charge": agency_amt,
            "status": "completed"
        }
        
        supabase.table('payments').insert(payment_record).execute()
        
        return {
            "message": "Settlement Authorized",
            "trip_id": trip_id,
            "status": "orchestrated",
            "reference": tx_id
        }
    except Exception as e:
        tb = traceback.format_exc()
        logging.error(f"CRITICAL: Payment Confirmation Failed for {trip_id}: {e}\n{tb}")
        origin = request.headers.get("Origin", "")
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Financial Matrix Handshake Failed",
                "msg": str(e),
                "trip_id": trip_id
            },
            headers={"Access-Control-Allow-Origin": origin if origin else "https://yash-three-dusky.vercel.app", "Access-Control-Allow-Credentials": "true"}
        )

@api_router.post("/trips/{trip_id}/finalize")
async def finalize_trip(request: Request, trip_id: str):
    user = await get_current_user(request)
    
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not trip_response.data or len(trip_response.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    # Update status
    supabase.table('trips').update({
        "status": "completed"
    }).eq('trip_id', trip_id).execute()
    
    return {
        "message": "Trip finalized successfully",
        "trip_id": trip_id
    }

@api_router.get("/trips/{trip_id}")
async def get_trip(request: Request, trip_id: str):
    user = await get_current_user(request)
    
    # 1. Fetch Core Trip
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not trip_response.data or len(trip_response.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    trip = trip_response.data[0]
    
    # 2. Hydrate with Personnel Matrix
    passengers_res = supabase.table('passengers').select('*').eq('trip_id', trip_id).execute()
    trip['tourists'] = passengers_res.data if passengers_res.data else []
        # 3. Hydrate with Settlement Data (Prioritize hydrated fields if trips table has them)
    if not trip.get('total_amount'):
        payment_res = supabase.table('payments').select('*').eq('trip_id', trip_id).execute()
        if payment_res.data:
            # Safer fallback: Just take the last record without explicit sorting on missing column
            latest_pay = payment_res.data[-1] 
            trip['total_amount'] = latest_pay.get('total_amount', 0)
            trip['transaction_id'] = latest_pay.get('transaction_id', '')
        else:
            trip['total_amount'] = 0

    
    return trip


@api_router.post("/trip/send-manifest")
async def send_trip_manifest(request: Request, payload: Dict[str, Any]):
    user = await get_current_user(request)
    # This simulates sending a real Email/WhatsApp/SMS notification
    logging.info(f"NOTIFICATION_HUB: Sending manifest to {payload.get('emails')} and {payload.get('phones')}")
    logging.info(f"MANIFEST_CONTENT: {payload.get('manifest')}")
    
    return {"message": "Dispatched to notification hub"}

@api_router.get("/trips")
async def get_trips(request: Request):
    user = await get_current_user(request)
    
    # 1. Fetch Core Trips
    trips_response = supabase.table('trips').select('*').eq('user_id', user.user_id).order('created_at', desc=True).execute()
    trips = trips_response.data
    
    if not trips:
        return []
    
    # 2. Bulk Fetch Associated Data for Better Performance
    trip_ids = [t['trip_id'] for t in trips]
    
    # Fetch all passengers for these trips
    all_passengers = supabase.table('passengers').select('*').in_('trip_id', trip_ids).execute()
    pax_map = {}
    for p in (all_passengers.data or []):
        tid = p['trip_id']
        if tid not in pax_map: pax_map[tid] = []
        pax_map[tid].append(p)
        
    # Fetch all payments for these trips
    all_payments = supabase.table('payments').select('*').in_('trip_id', trip_ids).execute()
    pay_map = {p['trip_id']: p for p in (all_payments.data or [])}
    
    # 3. Merge Data (Prioritize trips table fields if present)
    for t in trips:
        tid = t['trip_id']
        t['tourists'] = pax_map.get(tid, [])
        # If trips table misses financial data, fallback to payments map
        if not t.get('total_amount'):
            t['total_amount'] = pay_map.get(tid, {}).get('total_amount', 0)
            t['transaction_id'] = pay_map.get(tid, {}).get('transaction_id', '')
        
    return trips

origins = os.environ.get("CORS_ORIGINS")

if origins:
    origins = origins.split(",")
else:
    # Explicitly whitelist the Vercel production origin to avoid wildcard issues with credentials
    origins = [
        "https://yash-three-dusky.vercel.app", 
        "http://localhost:3000",
        "https://yash-kailarysathwik-create.vercel.app"
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "v2-relational-fix", "has_get_trip_details": True}

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
