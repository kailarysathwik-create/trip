import sys
import os

path = 's:/AI_ITERARY/YASH/Y.A.S.H-main/backend/main.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Define the old function start and end (concise enough to match)
old_start = '@api_router.post(\"/trips/{trip_id}/generate-itinerary\")\nasync def generate_itinerary(request: Request, trip_id: str):'
old_end = 'Return ONLY the JSON array, no other text.\"\"\"'

# New function content
new_func = """@api_router.post("/trips/{trip_id}/generate-itinerary")
async def generate_itinerary(request: Request, trip_id: str):
    user = await get_current_user(request)
    body = await request.json()
    transport = body.get('transport', {})
    stays = body.get('stays', [])
    
    trip_response = supabase.table('trips').select('*').eq('trip_id', trip_id).eq('user_id', user.user_id).execute()
    
    if not trip_response.data or len(trip_response.data) == 0:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    trip_doc = trip_response.data[0]
    details = trip_doc["details"]
    
    # Enrich prompt with actual booking data
    transport_info = f"Transport: {transport.get('provider')} ({transport.get('type')}) - {transport.get('booking_id')}. Arriving at {transport.get('arrival_time')}." if transport else "Transport: Not specified"
    stays_info = "Accommodations:\\n" + "\\n".join([f"- {s.get('hotel_name')} in {s.get('location')} (Check-in: {s.get('check_in')})" for s in stays]) if stays else ""

    # Generate itinerary using AI
    prompt = f\"\"\"Create a detailed {details['num_days']}-day luxury travel itinerary for {details['from_location']} to {details['destination']}.

CRITICAL: You MUST provide exactly {details['num_days']} days.

CONTEXT:
Starting {details['start_date']}
{transport_info}
{stays_info}
Travelers: {details['num_people']}

INSTRUCTIONS:
1. Use the EXACT transport and stay details above.
2. Day 1 starts with arrival.
3. Plan activities near the specified hotels.

Return ONLY a JSON array with exactly {details['num_days']} objects:
[
  {{
    "day": 1,
    "title": "Title",
    "places": ["Place description"],
    "activities": ["Activity 1"]
  }}
]\"\"\""""

import re

# Find the start and end of the function body
pattern = r'@api_router\.post\("/trips/\{trip_id\}/generate-itinerary"\)\nasync def generate_itinerary\(request: Request, trip_id: str\):.*?Return ONLY the JSON array, no other text\."{3}'
# Use DOTALL to match accross multiple lines
updated_content = re.sub(pattern, new_func, content, flags=re.DOTALL)

if updated_content != content:
    with open(path, 'w', encoding='utf-8') as f:
        f.write(updated_content)
    print("Successfully updated generate_itinerary")
else:
    print("Could not find the function to replace using regex")
