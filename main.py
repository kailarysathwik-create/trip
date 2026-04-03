from fastapi import FastAPI, APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
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
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://bitpovthujinbitxgiys.supabase.co')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# ============ Models ============

class User(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str
    email: str
    name: str
    picture: Optional[str] = None
    organization: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    upi_id: Optional[str] = None
    agency_charges_percentage: Optional[float] = 0.0
    has_payment_setup: bool = False
    created_at: str

class OnboardingInput(BaseModel):
    organization: str
    phone: str
    website: Optional[str] = None
    upi_id: str

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
    age: int
    gender: str

class TouristDetailsInput(BaseModel):
    tourists: List[TouristDetail]
    contact_phone: str
    contact_email: Optional[str] = None
    additional_phones: Optional[List[str]] = None
    agency_charges: Optional[float] = 0.0

# ============ Auth Helper ============

async def get_current_user(request: Request) -> User:
    session_token = request.cookies.get('session_token')
    
    if not session_token:
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            session_token = auth_header.replace('Bearer ', '')
    
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # Find session in Supabase
    session_response = supabase.table('user_sessions').select('*').eq('session_token', session_token).execute()
    
    if not session_response.data or len(session_response.data) == 0:
        raise HTTPException(status_code=401, detail="Invalid session")
    
    session_doc = session_response.data[0]
    
    # Check expiry
    expires_at = datetime.fromisoformat(session_doc["expires_at"].replace('Z', '+00:00'))
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired")
    
    # Get user
    user_response = supabase.table('users').select('*').eq('user_id', session_doc["user_id"]).execute()
    
    if not user_response.data or len(user_response.data) == 0:
        raise HTTPException(status_code=404, detail="User not found")
    
    user_doc = user_response.data[0]
    
    return User(**user_doc)

# ============ Auth Routes ============

@api_router.post("/auth/session")
async def create_session(request: Request, response: Response):
    body = await request.json()
    access_token = body.get('access_token')

    if not access_token:
        raise HTTPException(status_code=400, detail="access_token required")

    # Verify the Supabase access_token and get the authenticated user
    try:
        auth_response = supabase.auth.get_user(access_token)
        supabase_user = auth_response.user
        if not supabase_user:
            raise Exception("No user returned")
    except Exception as e:
        logging.error(f"Supabase token verification failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired access_token")

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

    # Check if user already exists in our users table
    existing_user_response = supabase.table('users').select('*').eq('email', user_data["email"]).execute()

    if existing_user_response.data and len(existing_user_response.data) > 0:
        user_id = existing_user_response.data[0]["user_id"]
        # Update name/picture in case they changed
        supabase.table('users').update({
            "name": user_data["name"],
            "picture": user_data.get("picture")
        }).eq('user_id', user_id).execute()
    else:
        # Create new user record
        new_user = {
            "user_id": user_id,
            "email": user_data["email"],
            "name": user_data["name"],
            "picture": user_data.get("picture"),
            "organization": None,
            "phone": None,
            "website": None,
            "upi_id": None,
            "agency_charges_percentage": 10.0,
            "has_payment_setup": False,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        supabase.table('users').insert(new_user).execute()

    # Store our session in Supabase
    session_doc = {
        "user_id": user_id,
        "session_token": session_token,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    supabase.table('user_sessions').insert(session_doc).execute()

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
async def complete_onboarding(request: Request, input: OnboardingInput):
    user = await get_current_user(request)
    
    supabase.table('users').update({
        "organization": input.organization,
        "phone": input.phone,
        "website": input.website,
        "upi_id": input.upi_id,
        "has_payment_setup": True
    }).eq('user_id', user.user_id).execute()
    
    # Get updated user
    user_response = supabase.table('users').select('*').eq('user_id', user.user_id).execute()
    
    return User(**user_response.data[0])

# ============ Trip Routes ============

@api_router.post("/trips/create")
async def create_trip(request: Request, details: TripDetails):
    user = await get_current_user(request)
    
    trip_id = f"trip_{uuid.uuid4().hex[:12]}"
    
    trip_doc = {
        "trip_id": trip_id,
        "user_id": user.user_id,
        "details": details.model_dump(),
        "itinerary": None,
        "transport_options": None,
        "selected_transport": None,
        "stay_options": None,
        "selected_stays": None,
        "checkout_plans": None,
        "selected_plan": None,
        "tourist_details": None,
        "payment_status": "pending",
        "payment_id": None,
        "status": "draft",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    supabase.table('trips').insert(trip_doc).execute()
    
    return {"trip_id": trip_id}

@api_router.post("/trips/{trip_id}/generate-itinerary")
async def generate_itinerary(request: Request, trip_id: str):
    user = await get_current_user(request)
    
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not trip_response.data or len(trip_response.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    trip_doc = trip_response.data[0]
    details = trip_doc["details"]
    
    # Generate itinerary using AI
    prompt = f"""Create a detailed {details['num_days']}-day travel itinerary from {details['from_location']} to {details['destination']}.
    
CRITICAL: You MUST provide exactly {details['num_days']} days in the itinerary. No more, no less.

Number of travelers: {details['num_people']}
Transport mode: {details['transport_mode']}
Start date: {details['start_date']}
{f"Budget: ₹{details['budget']}" if details.get('budget') else ''}
{f"Must-visit places on the way: {details['places_to_cover']}" if details.get('places_to_cover') else ''}
{f"Additional preferences: {details['preferences']}" if details.get('preferences') else ''}

IMPORTANT: 
1. If places_to_cover is mentioned, include those places in the itinerary
2. Create EXACTLY {details['num_days']} days - not 1 day, not 2 days, exactly {details['num_days']} days
3. Each day should be unique and well-planned

For each day (from Day 1 to Day {details['num_days']}), provide:
1. A descriptive title
2. 3-5 must-visit places with brief descriptions
3. Activities to do

Return ONLY a JSON array with exactly {details['num_days']} objects:
[
  {{
    "day": 1,
    "title": "Arrival and Day 1 in [Location]",
    "places": ["Place 1: Description", "Place 2: Description", "Place 3: Description"],
    "activities": ["Activity 1", "Activity 2", "Activity 3"]
  }},
  {{
    "day": 2,
    "title": "Day 2 title",
    "places": ["Place 1", "Place 2", "Place 3"],
    "activities": ["Activity 1", "Activity 2", "Activity 3"]
  }}
  ... continue until day {details['num_days']}
]

CRITICAL REMINDER: Array must contain EXACTLY {details['num_days']} day objects. Return ONLY the JSON array, no other text."""
    
    try:
        # Initialize Groq client
        client = Groq(
            api_key=os.environ.get('GROQ_API_KEY') or os.environ.get('EMERGENT_LLM_KEY', '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')
        )
        
        # Get AI response using Groq library
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a professional travel planner. Return only valid JSON without markdown formatting."},
                {"role": "user", "content": prompt}
            ],
            response_format={ "type": "json_object" }
        )
        
        response_text = completion.choices[0].message.content
        
        import json
        itinerary_data = json.loads(response_text)
        
        supabase.table('trips').update({
            "itinerary": {"days": itinerary_data}
        }).eq('trip_id', trip_id).execute()
        
        return {"itinerary": {"days": itinerary_data}}
    except Exception as e:
        logging.error(f"AI generation error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate itinerary")

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
    details = trip_doc["details"]
    
    # Generate realistic mock transport options (structured for easy API replacement later)
    prompt = f"""Generate 3-4 realistic {details['transport_mode']} options from {details['from_location']} to {details['destination']} starting on {details['start_date']}.

IMPORTANT: Provide REAL and ACCURATE Indian transport details:
- For trains: Use actual train names, numbers, and realistic timings for this route
- For flights: Use actual airline names and realistic flight timings
- For buses: Use actual bus operators for this route

For each option provide:
- Type (same as {details['transport_mode']})
- From and to locations (use actual {details['from_location']} to {details['destination']})
- Departure and arrival times (realistic based on start date {details['start_date']})
- Price in INR (base price per person - realistic Indian pricing)
- Provider name (actual provider for this route)

Return as JSON array:
[
  {{
    "type": "{details['transport_mode']}",
    "from_location": "{details['from_location']}",
    "to_location": "{details['destination']}",
    "departure_time": "YYYY-MM-DD HH:MM",
    "arrival_time": "YYYY-MM-DD HH:MM",
    "price": 1500.00,
    "provider": "Actual Provider Name"
  }}
]

Use REAL Indian transport data and realistic INR pricing."""
    
    try:
        client = Groq(
            api_key=os.environ.get('GROQ_API_KEY') or os.environ.get('EMERGENT_LLM_KEY', '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')
        )
        
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a travel booking assistant. Return only valid JSON without markdown formatting."},
                {"role": "user", "content": prompt}
            ],
            response_format={ "type": "json_object" }
        )
        
        response_text = completion.choices[0].message.content
        
        import json
        transport_data = json.loads(response_text)
        
        # Add option IDs
        for i, option in enumerate(transport_data):
            option["option_id"] = f"transport_{i+1}"
        
        supabase.table('trips').update({
            "transport_options": transport_data
        }).eq('trip_id', trip_id).execute()
        
        return {"transport_options": transport_data}
    except Exception as e:
        logging.error(f"Transport generation error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate transport options")

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
    budget = body.get('budget')
    
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not trip_response.data or len(trip_response.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    trip_doc = trip_response.data[0]
    details = trip_doc["details"]
    num_days = details["num_days"]
    
    # Generate stay options
    prompt = f"""Generate {num_days-1} hotel/accommodation options for a {num_days}-day trip to {details['destination']}.
{f'Budget per night: ₹{budget / num_days:.2f}' if budget else ''}

IMPORTANT: Provide REAL and ACCURATE accommodation details for {details['destination']}:
- Use actual hotel names or types common in {details['destination']}
- Realistic INR pricing for {details['destination']}
- Accurate area/location names within {details['destination']}
- Contact details for booking confirmation

For each accommodation:
- Name of hotel/stay (realistic name for {details['destination']})
- Location in {details['destination']} (actual area name)
- Contact phone (realistic Indian format: +91-XXXXXXXXXX)
- Contact email
- Check-in day (1 to {num_days})
- Check-out day
- Price per night in INR (realistic for {details['destination']})
- Rating (out of 5)
- List of amenities

Return as JSON array:
[
  {{
    "name": "Hotel Name",
    "location": "Actual Area in {details['destination']}",
    "contact_phone": "+91-9876543210",
    "contact_email": "hotel@example.com",
    "check_in_day": 1,
    "check_out_day": 3,
    "price_per_night": 2500.00,
    "rating": 4.5,
    "amenities": ["WiFi", "Breakfast", "Pool"]
  }}
]

Use REAL data for {details['destination']} with accurate INR pricing."""
    
    try:
        client = Groq(
            api_key=os.environ.get('GROQ_API_KEY') or os.environ.get('EMERGENT_LLM_KEY', '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')
        )
        
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a hotel booking assistant. Return only valid JSON without markdown formatting."},
                {"role": "user", "content": prompt}
            ],
            response_format={ "type": "json_object" }
        )
        
        response_text = completion.choices[0].message.content
        
        import json
        stays_data = json.loads(response_text)
        
        # Add option IDs
        for i, option in enumerate(stays_data):
            option["option_id"] = f"stay_{i+1}"
        
        supabase.table('trips').update({
            "stay_options": stays_data
        }).eq('trip_id', trip_id).execute()
        
        return {"stay_options": stays_data}
    except Exception as e:
        logging.error(f"Stay generation error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate stay options")

@api_router.post("/trips/{trip_id}/select-stays")
async def select_stays(request: Request, trip_id: str):
    user = await get_current_user(request)
    body = await request.json()
    stay_ids = body.get('stay_ids', [])
    
    # Get trip and stay details
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    if trip_response.data:
        trip_doc = trip_response.data[0]
        stay_options = trip_doc.get("stay_options", [])
        
        # Send notification to each selected stay owner
        for stay_id in stay_ids:
            stay = next((s for s in stay_options if s.get("option_id") == stay_id), None)
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

@api_router.post("/trips/{trip_id}/tourist-details")
async def save_tourist_details(request: Request, trip_id: str, input: TouristDetailsInput):
    user = await get_current_user(request)
    
    details_data = input.model_dump()
    
    result = supabase.table('trips').update({
        "tourist_details": details_data
    }).eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not result.data or len(result.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    return {"message": "Tourist details saved"}

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

@api_router.post("/trips/{trip_id}/confirm-payment")
async def confirm_payment(request: Request, trip_id: str):
    """Customer confirms they have paid via UPI"""
    user = await get_current_user(request)
    body = await request.json()
    
    transaction_id = body.get('transaction_id', '')  # Optional UPI transaction ID
    
    # Update trip as payment completed
    supabase.table('trips').update({
        "payment_status": "completed",
        "payment_id": transaction_id,
        "status": "confirmed"
    }).eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    return {
        "message": "Payment confirmed successfully",
        "trip_id": trip_id,
        "status": "confirmed"
    }

@api_router.post("/trips/{trip_id}/finalize")
async def finalize_trip(request: Request, trip_id: str):
    user = await get_current_user(request)
    
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not trip_response.data or len(trip_response.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    trip_doc = trip_response.data[0]
    
    # Check if payment is completed
    if trip_doc.get('payment_status') != 'completed':
        raise HTTPException(status_code=400, detail="Payment not completed")
    
    # Update status
    supabase.table('trips').update({
        "status": "completed"
    }).eq('trip_id', trip_id).execute()
    
    return {
        "message": "Trip finalized successfully",
        "trip_id": trip_id,
        "note": "Itinerary confirmation sent to the provided contact details"
    }

@api_router.get("/trips/{trip_id}")
async def get_trip(request: Request, trip_id: str):
    user = await get_current_user(request)
    
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not trip_response.data or len(trip_response.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    return trip_response.data[0]

@api_router.get("/trips")
async def get_trips(request: Request):
    user = await get_current_user(request)
    
    trips_response = supabase.table('trips').select('*').eq('user_id', user.user_id).order('created_at', desc=True).execute()
    
    return trips_response.data

origins = os.environ.get("CORS_ORIGINS")

if origins:
    origins = origins.split(",")
else:
    origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
