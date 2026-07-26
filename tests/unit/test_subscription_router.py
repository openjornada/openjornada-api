"""
Unit tests for the subscription router (status + portal endpoints).

A standalone FastAPI app mounts the real router with `require_admin`
overridden, so these tests do not need MongoDB or a real login. Stripe and
subscription_service are always mocked.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth.permissions import require_admin
from api.models.auth import APIUser
from api.routers import subscription as subscription_router
from api.services import subscription_service as svc

_app = FastAPI()
_app.include_router(subscription_router.router, prefix="/api")
_app.dependency_overrides[require_admin] = lambda: APIUser(username="admin", email="admin@example.com", role="admin")

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
    svc._cached_status = None
    svc._cached_at = 0.0


# ===========================================================================
# GET /subscription/status — refresh-failed signal (finding #1)
# ===========================================================================

def test_status_refresh_failed_surfaces_error_and_does_not_fabricate_active():
    stale = svc.SubscriptionStatus(status="canceled", current_period_end=None, days_remaining=None, mode="live")
    error_status = svc.SubscriptionStatus(
        status="canceled", current_period_end=None, days_remaining=None, mode="live", error="refresh_failed"
    )
    env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
    with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
         patch("api.services.subscription_service.get_status", new=AsyncMock(return_value=error_status)):
        response = client.get("/api/subscription/status?refresh=true")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["status"] == "canceled"
    assert body["error"] == "refresh_failed"
    assert body["message"] == "No se pudo verificar el estado ahora; inténtalo de nuevo."


def test_status_success_has_no_error_field():
    ok_status = svc.SubscriptionStatus(status="active", current_period_end=None, days_remaining=None, mode="live")
    env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
    with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
         patch("api.services.subscription_service.get_status", new=AsyncMock(return_value=ok_status)):
        response = client.get("/api/subscription/status")

    body = response.json()
    assert "error" not in body
    assert body["message"] == "Suscripción al día"


# ===========================================================================
# GET /subscription/portal — customer id validation (finding #2)
# ===========================================================================

def test_portal_missing_customer_id_returns_500():
    env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
    with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
         patch("api.routers.subscription.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
         patch("api.routers.subscription.stripe") as mock_stripe:
        response = client.get("/api/subscription/portal")

    assert response.status_code == 500
    assert response.json()["detail"] == "STRIPE_CUSTOMER_ID no configurado"
    mock_stripe.billing_portal.Session.create.assert_not_called()


# ===========================================================================
# GET /subscription/portal — X-Forwarded-Host (finding #3)
# ===========================================================================

def test_portal_uses_x_forwarded_host_for_return_url():
    env = _env({
        "STRIPE_API_KEY": "rk_test_123",
        "STRIPE_SUBSCRIPTION_ID": "sub_123",
        "STRIPE_CUSTOMER_ID": "cus_123",
    })
    with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
         patch("api.routers.subscription.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
         patch("api.routers.subscription.stripe") as mock_stripe:
        mock_stripe.billing_portal.Session.create.return_value.url = "https://billing.stripe.com/session/xyz"

        response = client.get(
            "/api/subscription/portal",
            headers={"X-Forwarded-Host": "tenant.openjornada.es, internal-proxy"},
        )

    assert response.status_code == 200
    _, kwargs = mock_stripe.billing_portal.Session.create.call_args
    assert kwargs["return_url"] == "https://tenant.openjornada.es/admin"


def test_portal_falls_back_to_request_hostname_without_forwarded_host():
    env = _env({
        "STRIPE_API_KEY": "rk_test_123",
        "STRIPE_SUBSCRIPTION_ID": "sub_123",
        "STRIPE_CUSTOMER_ID": "cus_123",
    })
    with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
         patch("api.routers.subscription.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
         patch("api.routers.subscription.stripe") as mock_stripe:
        mock_stripe.billing_portal.Session.create.return_value.url = "https://billing.stripe.com/session/xyz"

        response = client.get("/api/subscription/portal")

    assert response.status_code == 200
    _, kwargs = mock_stripe.billing_portal.Session.create.call_args
    assert kwargs["return_url"] == f"https://testserver/admin"
