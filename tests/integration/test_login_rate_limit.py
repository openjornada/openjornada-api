"""Integration tests for login rate limiting (POST /api/token).

Rate limiting is disabled by default for the suite (see tests/conftest.py)
so other tests calling /api/token aren't throttled by a shared counter.
The `enable_rate_limiting` fixture below re-enables it, using the real
configured LOGIN_RATE_LIMIT (default "5/minute"), and resets the limiter's
in-memory storage before/after so this test doesn't interfere with others.
"""
import pytest
from httpx import AsyncClient

from api.utils.rate_limit import limiter

FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
def enable_rate_limiting():
    limiter.reset()
    limiter.enabled = True
    yield
    limiter.reset()
    limiter.enabled = False


async def test_excess_login_attempts_return_429(
    async_client: AsyncClient, enable_rate_limiting
):
    payload = {"username": "nobody@test.com", "password": "wrong-password"}

    for _ in range(5):
        response = await async_client.post("/api/token", data=payload, headers=FORM_HEADERS)
        assert response.status_code == 401

    response = await async_client.post("/api/token", data=payload, headers=FORM_HEADERS)
    assert response.status_code == 429


async def test_login_within_limit_behaves_normally(
    async_client: AsyncClient, test_db, enable_rate_limiting
):
    from datetime import datetime, timezone

    from api.auth.auth_handler import get_password_hash

    email = "rate_limit_test@test.com"
    password = "TestPassword123!"
    await test_db.APIUsers.delete_one({"email": email})
    await test_db.APIUsers.insert_one({
        "username": "rate_limit_test",
        "email": email,
        "hashed_password": get_password_hash(password),
        "role": "admin",
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
    })

    invalid_response = await async_client.post(
        "/api/token",
        data={"username": email, "password": "wrong-password"},
        headers=FORM_HEADERS,
    )
    assert invalid_response.status_code == 401

    valid_response = await async_client.post(
        "/api/token",
        data={"username": email, "password": password},
        headers=FORM_HEADERS,
    )
    assert valid_response.status_code == 200
    assert "access_token" in valid_response.json()
