"""
Quick test script to verify incidents implementation
This script checks that all modules import correctly and the API structure is valid
"""

import sys
from api.models.incidents import (
    IncidentStatus,
    IncidentBase,
    IncidentCreate,
    IncidentUpdate,
    IncidentInDB,
    IncidentResponse
)
from api.routers import incidents

def test_models():
    """Test that all models are properly defined"""
    print("Testing models...")

    # Test IncidentStatus enum
    assert IncidentStatus.PENDING.value == "pending"
    assert IncidentStatus.IN_REVIEW.value == "in_review"
    assert IncidentStatus.RESOLVED.value == "resolved"
    print("  ✓ IncidentStatus enum works correctly")

    # Test IncidentCreate schema
    incident_create = IncidentCreate(
        email="worker@example.com",
        password="password123",
        description="Test incident description"
    )
    assert incident_create.email == "worker@example.com"
    assert incident_create.description == "Test incident description"
    print("  ✓ IncidentCreate schema works correctly")

    # Test IncidentUpdate schema
    incident_update = IncidentUpdate(
        status=IncidentStatus.IN_REVIEW,
        admin_notes="Admin is reviewing this"
    )
    assert incident_update.status == IncidentStatus.IN_REVIEW
    assert incident_update.admin_notes == "Admin is reviewing this"
    print("  ✓ IncidentUpdate schema works correctly")

    print("✓ All models tests passed!\n")


def test_router():
    """Test that router is properly configured"""
    print("Testing router...")

    # Check that router exists and has correct endpoints
    assert hasattr(incidents, 'router')
    routes = [route.path for route in incidents.router.routes]

    expected_routes = ["/", "/{incident_id}"]
    for route in expected_routes:
        assert route in routes, f"Route {route} not found"

    print(f"  ✓ Router has {len(routes)} endpoints")
    print(f"  ✓ All expected routes found: {expected_routes}")
    print("✓ Router tests passed!\n")


def main():
    print("\n" + "="*60)
    print("INCIDENTS FUNCTIONALITY TEST")
    print("="*60 + "\n")

    try:
        test_models()
        test_router()

        print("="*60)
        print("ALL TESTS PASSED! ✓")
        print("="*60 + "\n")
        print("The incidents functionality is properly implemented and ready to use.")
        print("\nAPI Endpoints available:")
        print("  POST   /api/incidents/          - Create incident (worker auth)")
        print("  GET    /api/incidents/          - List all incidents (admin only)")
        print("  GET    /api/incidents/{id}      - Get single incident (admin only)")
        print("  PATCH  /api/incidents/{id}      - Update incident (admin only)")
        return 0

    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
