"""
Unit tests for the require_active_subscription FastAPI dependency.

A minimal standalone FastAPI app is used so these tests do not require
MongoDB or the full api.main application. Stripe is always mocked.
"""

from unittest.mock import AsyncMock, patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.auth.subscription_guard import require_active_subscription
from api.services import subscription_service as svc

_app = FastAPI()


@_app.post("/protected")
async def protected_endpoint(_subscription: None = Depends(require_active_subscription)):
    return {"ok": True}


client = TestClient(_app)


def _env(overrides: dict) -> dict:
    base = {
        "STRIPE_API_KEY": "",
        "STRIPE_SUBSCRIPTION_ID": "",
        "STRIPE_CUSTOMER_ID": "",
        "SUBSCRIPTION_MODE": "live",
    }
    base.update(overrides)
    return base


def setup_function():
    """Every test starts with a clean module-level cache."""
    svc._cached_status = None
    svc._cached_at = 0.0


def test_disabled_stripe_returns_200_regression_codefriends():
    """Tenants without STRIPE_* configured (e.g. codefriends) keep working."""
    env = _env({})
    with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)):
        response = client.post("/protected")

    assert response.status_code == 200


def test_active_subscription_returns_200():
    env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
    status_obj = svc.SubscriptionStatus(status="active", current_period_end=None, days_remaining=None, mode="live")
    with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
         patch("api.services.subscription_service.get_status", new=AsyncMock(return_value=status_obj)):
        response = client.post("/protected")

    assert response.status_code == 200


def test_canceled_subscription_returns_402():
    env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
    status_obj = svc.SubscriptionStatus(status="canceled", current_period_end=None, days_remaining=None, mode="live")
    with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
         patch("api.services.subscription_service.get_status", new=AsyncMock(return_value=status_obj)):
        response = client.post("/protected")

    assert response.status_code == 402
    body = response.json()["detail"]
    assert body["detail"] == "subscription_inactive"
    assert body["message"] == "Su empresa no permite el acceso a su cuenta. Contacte con su empresa."


def test_past_due_subscription_returns_200_grace_period():
    env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
    status_obj = svc.SubscriptionStatus(status="past_due", current_period_end=None, days_remaining=None, mode="live")
    with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
         patch("api.services.subscription_service.get_status", new=AsyncMock(return_value=status_obj)):
        response = client.post("/protected")

    assert response.status_code == 200
