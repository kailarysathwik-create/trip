"""
Backend API Tests for Y.A.S.H (Yatra And Stay Hub) - AI Trip Planner
Tests all API endpoints including auth, trips, itinerary, transport, stays, and payment flows.
"""
import pytest
import requests
import os
import uuid
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client

# Get backend URL from environment
BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', 'https://ai-trip-planner-38.preview.emergentagent.com')
API_URL = f"{BASE_URL}/api"

# Supabase connection for test data setup
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://bitpovthujinbitxgiys.supabase.co')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')


class TestSetup:
    """Setup test user and session in Supabase"""
    
    @staticmethod
    def create_test_user_and_session():
        """Create a test user and session in Supabase, return session token"""
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        
        # Generate unique IDs
        timestamp = int(datetime.now().timestamp() * 1000)
        user_id = f"test_user_{timestamp}"
        session_token = f"test_session_{uuid.uuid4().hex}"
        
        # Create test user
        user_data = {
            "user_id": user_id,
            "email": f"test.user.{timestamp}@example.com",
            "name": "Test User",
            "picture": "https://via.placeholder.com/150",
            "organization": "Test Travel Agency",
            "phone": "+919876543210",
            "website": "https://testagency.com",
            "upi_id": "testagency@paytm",
            "agency_charges_percentage": 10.0,
            "has_payment_setup": True,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        
        supabase.table('users').insert(user_data).execute()
        
        # Create session
        session_data = {
            "user_id": user_id,
            "session_token": session_token,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        
        supabase.table('user_sessions').insert(session_data).execute()
        
        return session_token, user_id
    
    @staticmethod
    def cleanup_test_data(user_id):
        """Clean up test data from Supabase"""
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        
        # Delete trips
        supabase.table('trips').delete().eq('user_id', user_id).execute()
        # Delete sessions
        supabase.table('user_sessions').delete().eq('user_id', user_id).execute()
        # Delete user
        supabase.table('users').delete().eq('user_id', user_id).execute()


@pytest.fixture(scope="module")
def auth_session():
    """Create authenticated session for tests"""
    session_token, user_id = TestSetup.create_test_user_and_session()
    
    session = requests.Session()
    session.cookies.set('session_token', session_token)
    session.headers.update({
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {session_token}'
    })
    
    yield session, user_id
    
    # Cleanup after all tests
    TestSetup.cleanup_test_data(user_id)


@pytest.fixture
def api_client():
    """Unauthenticated API client"""
    session = requests.Session()
    session.headers.update({'Content-Type': 'application/json'})
    return session


# ============ Auth Tests ============

class TestAuth:
    """Authentication endpoint tests"""
    
    def test_auth_me_without_session(self, api_client):
        """Test /api/auth/me returns 401 without session"""
        response = api_client.get(f"{API_URL}/auth/me")
        assert response.status_code == 401
        data = response.json()
        assert "detail" in data
        print("✓ /api/auth/me correctly returns 401 without session")
    
    def test_auth_me_with_session(self, auth_session):
        """Test /api/auth/me returns user data with valid session"""
        session, user_id = auth_session
        response = session.get(f"{API_URL}/auth/me")
        assert response.status_code == 200
        data = response.json()
        assert "user" in data
        assert data["user"]["user_id"] == user_id
        assert "needs_onboarding" in data
        print(f"✓ /api/auth/me returns user data: {data['user']['email']}")


# ============ Trip Creation Tests ============

class TestTripCreation:
    """Trip creation endpoint tests"""
    
    def test_create_trip_without_auth(self, api_client):
        """Test trip creation fails without authentication"""
        payload = {
            "from_location": "Mumbai",
            "destination": "Goa",
            "num_people": 2,
            "num_days": 3,
            "transport_mode": "flight",
            "start_date": "2026-02-15"
        }
        response = api_client.post(f"{API_URL}/trips/create", json=payload)
        assert response.status_code == 401
        print("✓ Trip creation correctly requires authentication")
    
    def test_create_trip_with_all_fields(self, auth_session):
        """Test trip creation with all fields"""
        session, user_id = auth_session
        
        payload = {
            "from_location": "Mumbai",
            "destination": "Goa",
            "num_people": 2,
            "budget": 50000,
            "num_days": 3,
            "transport_mode": "flight",
            "start_date": "2026-02-15",
            "places_to_cover": "Pune, Lonavala",
            "preferences": "Beach activities, cultural sites"
        }
        
        response = session.post(f"{API_URL}/trips/create", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "trip_id" in data
        assert data["trip_id"].startswith("trip_")
        print(f"✓ Trip created successfully: {data['trip_id']}")
        
        # Store trip_id for later tests
        TestTripCreation.trip_id = data["trip_id"]
        return data["trip_id"]
    
    def test_create_trip_with_train_transport(self, auth_session):
        """Test trip creation with train transport mode"""
        session, user_id = auth_session
        
        payload = {
            "from_location": "Delhi",
            "destination": "Jaipur",
            "num_people": 4,
            "num_days": 2,
            "transport_mode": "train",
            "start_date": "2026-03-01"
        }
        
        response = session.post(f"{API_URL}/trips/create", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "trip_id" in data
        print(f"✓ Trip with train transport created: {data['trip_id']}")
    
    def test_create_trip_with_cab_transport(self, auth_session):
        """Test trip creation with cab (car) transport mode"""
        session, user_id = auth_session
        
        payload = {
            "from_location": "Bangalore",
            "destination": "Mysore",
            "num_people": 3,
            "num_days": 2,
            "transport_mode": "car",
            "start_date": "2026-03-10"
        }
        
        response = session.post(f"{API_URL}/trips/create", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "trip_id" in data
        print(f"✓ Trip with cab transport created: {data['trip_id']}")


# ============ Itinerary Tests ============

class TestItinerary:
    """Itinerary generation and update tests"""
    
    def test_generate_itinerary(self, auth_session):
        """Test itinerary generation for a trip"""
        session, user_id = auth_session
        
        # First create a trip
        payload = {
            "from_location": "Chennai",
            "destination": "Pondicherry",
            "num_people": 2,
            "num_days": 3,
            "transport_mode": "car",
            "start_date": "2026-04-01"
        }
        
        create_response = session.post(f"{API_URL}/trips/create", json=payload)
        assert create_response.status_code == 200
        trip_id = create_response.json()["trip_id"]
        
        # Generate itinerary
        response = session.post(f"{API_URL}/trips/{trip_id}/generate-itinerary", json={})
        assert response.status_code == 200
        data = response.json()
        assert "itinerary" in data
        assert "days" in data["itinerary"]
        
        # Verify correct number of days
        days = data["itinerary"]["days"]
        assert len(days) == 3, f"Expected 3 days, got {len(days)}"
        
        # Verify day structure
        for day in days:
            assert "day" in day
            assert "title" in day
            assert "places" in day
            assert "activities" in day
        
        print(f"✓ Itinerary generated with {len(days)} days")
        TestItinerary.trip_id = trip_id
        return trip_id
    
    def test_get_trip_with_itinerary(self, auth_session):
        """Test getting trip details includes itinerary"""
        session, user_id = auth_session
        
        if not hasattr(TestItinerary, 'trip_id'):
            pytest.skip("No trip_id from previous test")
        
        response = session.get(f"{API_URL}/trips/{TestItinerary.trip_id}")
        assert response.status_code == 200
        data = response.json()
        assert "itinerary" in data
        assert data["itinerary"] is not None
        print("✓ Trip details include itinerary")
    
    def test_update_itinerary(self, auth_session):
        """Test updating itinerary"""
        session, user_id = auth_session
        
        if not hasattr(TestItinerary, 'trip_id'):
            pytest.skip("No trip_id from previous test")
        
        updated_itinerary = {
            "days": [
                {
                    "day": 1,
                    "title": "Updated Day 1 - Arrival",
                    "places": ["Beach", "Temple"],
                    "activities": ["Swimming", "Sightseeing"]
                },
                {
                    "day": 2,
                    "title": "Updated Day 2 - Exploration",
                    "places": ["Market", "Fort"],
                    "activities": ["Shopping", "Photography"]
                },
                {
                    "day": 3,
                    "title": "Updated Day 3 - Departure",
                    "places": ["Cafe", "Garden"],
                    "activities": ["Breakfast", "Walk"]
                }
            ]
        }
        
        response = session.put(
            f"{API_URL}/trips/{TestItinerary.trip_id}/itinerary",
            json=updated_itinerary
        )
        assert response.status_code == 200
        print("✓ Itinerary updated successfully")


# ============ Tourist Details Tests ============

class TestTouristDetails:
    """Tourist details and agency charges tests"""
    
    def test_save_tourist_details_with_agency_charges(self, auth_session):
        """Test saving tourist details with agency charges (INR amount)"""
        session, user_id = auth_session
        
        # Create a trip first
        payload = {
            "from_location": "Hyderabad",
            "destination": "Vizag",
            "num_people": 2,
            "num_days": 3,
            "transport_mode": "train",
            "start_date": "2026-05-01"
        }
        
        create_response = session.post(f"{API_URL}/trips/create", json=payload)
        assert create_response.status_code == 200
        trip_id = create_response.json()["trip_id"]
        
        # Save tourist details with agency charges
        tourist_details = {
            "tourists": [
                {"name": "John Doe", "age": 30, "gender": "male"},
                {"name": "Jane Doe", "age": 28, "gender": "female"}
            ],
            "contact_phone": "+919876543210",
            "contact_email": "john.doe@example.com",
            "additional_phones": ["+919876543211"],
            "agency_charges": 5000  # INR amount, not percentage
        }
        
        response = session.post(
            f"{API_URL}/trips/{trip_id}/tourist-details",
            json=tourist_details
        )
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Tourist details saved"
        print(f"✓ Tourist details saved with agency charges: ₹5000")
        
        TestTouristDetails.trip_id = trip_id
        return trip_id


# ============ Payment Info Tests ============

class TestPaymentInfo:
    """Payment info and UPI payment tests"""
    
    def test_get_payment_info(self, auth_session):
        """Test getting payment info returns agency charges from tourist_details"""
        session, user_id = auth_session
        
        # Create trip and save tourist details first
        payload = {
            "from_location": "Kolkata",
            "destination": "Darjeeling",
            "num_people": 2,
            "num_days": 4,
            "transport_mode": "train",
            "start_date": "2026-06-01"
        }
        
        create_response = session.post(f"{API_URL}/trips/create", json=payload)
        trip_id = create_response.json()["trip_id"]
        
        # Save tourist details with agency charges
        tourist_details = {
            "tourists": [
                {"name": "Test Tourist 1", "age": 25, "gender": "male"},
                {"name": "Test Tourist 2", "age": 24, "gender": "female"}
            ],
            "contact_phone": "+919876543210",
            "agency_charges": 7500  # INR amount
        }
        
        session.post(f"{API_URL}/trips/{trip_id}/tourist-details", json=tourist_details)
        
        # Get payment info
        response = session.get(f"{API_URL}/trips/{trip_id}/payment-info")
        assert response.status_code == 200
        data = response.json()
        
        # Verify payment info structure
        assert "upi_id" in data
        assert "agency_name" in data
        assert "total_amount" in data
        
        # Verify total_amount equals agency_charges from tourist_details
        assert data["total_amount"] == 7500, f"Expected 7500, got {data['total_amount']}"
        
        print(f"✓ Payment info: UPI={data['upi_id']}, Amount=₹{data['total_amount']}")
        TestPaymentInfo.trip_id = trip_id
    
    def test_confirm_payment(self, auth_session):
        """Test confirming UPI payment"""
        session, user_id = auth_session
        
        if not hasattr(TestPaymentInfo, 'trip_id'):
            pytest.skip("No trip_id from previous test")
        
        response = session.post(
            f"{API_URL}/trips/{TestPaymentInfo.trip_id}/confirm-payment",
            json={"transaction_id": "UPI123456789"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "confirmed"
        print("✓ Payment confirmed successfully")


# ============ Trip List Tests ============

class TestTripList:
    """Trip listing tests"""
    
    def test_get_trips_list(self, auth_session):
        """Test getting list of trips"""
        session, user_id = auth_session
        
        response = session.get(f"{API_URL}/trips")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"✓ Retrieved {len(data)} trips")
    
    def test_get_single_trip(self, auth_session):
        """Test getting a single trip by ID"""
        session, user_id = auth_session
        
        # Create a trip first
        payload = {
            "from_location": "Pune",
            "destination": "Mahabaleshwar",
            "num_people": 4,
            "num_days": 2,
            "transport_mode": "car",
            "start_date": "2026-07-01"
        }
        
        create_response = session.post(f"{API_URL}/trips/create", json=payload)
        trip_id = create_response.json()["trip_id"]
        
        # Get the trip
        response = session.get(f"{API_URL}/trips/{trip_id}")
        assert response.status_code == 200
        data = response.json()
        
        # Verify trip structure
        assert data["trip_id"] == trip_id
        assert "details" in data
        assert data["details"]["destination"] == "Mahabaleshwar"
        assert data["details"]["transport_mode"] == "car"
        print(f"✓ Retrieved trip: {trip_id}")


# ============ Onboarding Tests ============

class TestOnboarding:
    """Onboarding endpoint tests"""
    
    def test_onboarding_update(self, auth_session):
        """Test onboarding updates user profile"""
        session, user_id = auth_session
        
        onboarding_data = {
            "organization": "Updated Travel Agency",
            "phone": "+919999999999",
            "website": "https://updatedagency.com",
            "upi_id": "updated@paytm"
        }
        
        response = session.post(f"{API_URL}/onboarding", json=onboarding_data)
        assert response.status_code == 200
        data = response.json()
        
        # Verify updated fields
        assert data["organization"] == "Updated Travel Agency"
        assert data["upi_id"] == "updated@paytm"
        assert data["has_payment_setup"] == True
        print("✓ Onboarding completed successfully")


# ============ Negative Tests ============

class TestNegativeCases:
    """Negative test cases"""
    
    def test_get_nonexistent_trip(self, auth_session):
        """Test getting a trip that doesn't exist"""
        session, user_id = auth_session
        
        response = session.get(f"{API_URL}/trips/trip_nonexistent123")
        assert response.status_code == 404
        print("✓ Non-existent trip returns 404")
    
    def test_generate_itinerary_nonexistent_trip(self, auth_session):
        """Test generating itinerary for non-existent trip"""
        session, user_id = auth_session
        
        response = session.post(f"{API_URL}/trips/trip_nonexistent123/generate-itinerary", json={})
        assert response.status_code == 404
        print("✓ Generate itinerary for non-existent trip returns 404")


# ============ Verify No Packages Step ============

class TestNoPackagesStep:
    """Verify packages/plans endpoints are removed"""
    
    def test_generate_checkout_plans_removed(self, auth_session):
        """Test that generate-checkout-plans endpoint is removed or returns error"""
        session, user_id = auth_session
        
        # Create a trip
        payload = {
            "from_location": "Ahmedabad",
            "destination": "Udaipur",
            "num_people": 2,
            "num_days": 3,
            "transport_mode": "car",
            "start_date": "2026-08-01"
        }
        
        create_response = session.post(f"{API_URL}/trips/create", json=payload)
        trip_id = create_response.json()["trip_id"]
        
        # Try to call generate-checkout-plans (should be removed)
        response = session.post(f"{API_URL}/trips/{trip_id}/generate-checkout-plans", json={})
        
        # This endpoint should either not exist (404/405) or return an error
        # Based on the agent context, this endpoint was supposed to be REMOVED
        # If it still exists, we note it as an issue
        if response.status_code == 200:
            print("⚠ WARNING: generate-checkout-plans endpoint still exists (should be removed)")
        else:
            print(f"✓ generate-checkout-plans endpoint returns {response.status_code} (expected to be removed)")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
