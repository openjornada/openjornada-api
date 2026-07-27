"""
Unit tests for subscription_service.

All tests are pure unit tests — no MongoDB, no HTTP server, and Stripe is
always mocked (stripe.Subscription.retrieve is never called for real).
"""

from unittest.mock import MagicMock, patch

import pytest

from api.services import subscription_service as svc


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts with a clean module-level cache."""
    svc._cached_status = None
    svc._cached_at = 0.0
    yield
    svc._cached_status = None
    svc._cached_at = 0.0


def _env(overrides: dict) -> dict:
    base = {
        "STRIPE_API_KEY": "",
        "STRIPE_SUBSCRIPTION_ID": "",
        "STRIPE_CUSTOMER_ID": "",
        "SUBSCRIPTION_MODE": "live",
    }
    base.update(overrides)
    return base


def _stripe_subscription(status: str = "active", current_period_end: int = 1234567890) -> MagicMock:
    sub = MagicMock()
    sub.get.side_effect = lambda key, default=None: {
        "status": status,
        "current_period_end": current_period_end,
    }.get(key, default)
    return sub


# ===========================================================================
# is_enabled()
# ===========================================================================

class TestIsEnabled:
    def test_true_when_both_vars_present(self):
        env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)):
            assert svc.is_enabled() is True

    def test_false_when_missing_api_key(self):
        env = _env({"STRIPE_SUBSCRIPTION_ID": "sub_123"})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)):
            assert svc.is_enabled() is False

    def test_false_when_missing_subscription_id(self):
        env = _env({"STRIPE_API_KEY": "rk_test_123"})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)):
            assert svc.is_enabled() is False

    def test_false_when_disabled_regression_codefriends(self):
        """Regression: a tenant with no STRIPE_* vars must behave as disabled."""
        env = _env({})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)):
            assert svc.is_enabled() is False


# ===========================================================================
# get_status()
# ===========================================================================

class TestGetStatus:
    async def test_disabled_returns_permissive_status_without_calling_stripe(self):
        env = _env({})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
             patch("api.services.subscription_service.stripe") as mock_stripe:
            result = await svc.get_status()

        assert result.status == "active"
        assert result.error is None
        mock_stripe.Subscription.retrieve.assert_not_called()

    async def test_enabled_calls_stripe_and_caches_result(self):
        env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
             patch("api.services.subscription_service.stripe") as mock_stripe:
            mock_stripe.Subscription.retrieve.return_value = _stripe_subscription("active")

            first = await svc.get_status()
            second = await svc.get_status()

        assert first.status == "active"
        assert second.status == "active"
        # Second call within TTL must use the cache, not re-hit Stripe.
        mock_stripe.Subscription.retrieve.assert_called_once_with("sub_123", api_key="rk_test_123")

    async def test_cache_respects_ttl_and_refetches_after_expiry(self):
        env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
             patch("api.services.subscription_service.stripe") as mock_stripe, \
             patch("api.services.subscription_service.time.monotonic") as mock_monotonic:
            mock_stripe.Subscription.retrieve.return_value = _stripe_subscription("active")

            mock_monotonic.return_value = 0.0
            await svc.get_status()

            # Still within the 600s TTL.
            mock_monotonic.return_value = 500.0
            await svc.get_status()
            assert mock_stripe.Subscription.retrieve.call_count == 1

            # TTL expired -> must refetch.
            mock_monotonic.return_value = 700.0
            await svc.get_status()
            assert mock_stripe.Subscription.retrieve.call_count == 2

    async def test_force_refresh_bypasses_cache(self):
        env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
             patch("api.services.subscription_service.stripe") as mock_stripe:
            mock_stripe.Subscription.retrieve.return_value = _stripe_subscription("active")

            await svc.get_status()
            await svc.get_status(force_refresh=True)

        assert mock_stripe.Subscription.retrieve.call_count == 2

    async def test_stripe_error_fails_open(self):
        """A transient Stripe/network error must never block workers (fail-open)."""
        env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
             patch("api.services.subscription_service.stripe") as mock_stripe:
            mock_stripe.Subscription.retrieve.side_effect = RuntimeError("network down")

            result = await svc.get_status()

        assert result.status == "active"

    async def test_cold_cache_stripe_error_fails_open_with_error_flag(self):
        """
        No prior successful fetch (cold cache) + Stripe error: worker gate
        must still fail open (status=active, allowed), but the error is
        surfaced via `error` so an admin knows it wasn't actually verified.
        """
        env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
             patch("api.services.subscription_service.stripe") as mock_stripe:
            mock_stripe.Subscription.retrieve.side_effect = RuntimeError("network down")

            result = await svc.get_status()

        assert result.status == "active"
        assert result.error == "refresh_failed"
        assert svc.is_worker_access_allowed(result) is True
        # The failed fetch must not populate the cache.
        assert svc._cached_status is None

    async def test_forced_refresh_error_with_stale_cache_does_not_fabricate_active(self):
        """
        A stale cached `canceled` status plus a forced-refresh Stripe error
        must NOT be reported as "active" to the admin. It must return the
        last-known status (canceled) with error="refresh_failed", and must
        not clear/overwrite the cache.
        """
        env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)), \
             patch("api.services.subscription_service.stripe") as mock_stripe:
            mock_stripe.Subscription.retrieve.return_value = _stripe_subscription("canceled")
            first = await svc.get_status()
            assert first.status == "canceled"

            mock_stripe.Subscription.retrieve.side_effect = RuntimeError("network down")
            refreshed = await svc.get_status(force_refresh=True)

            assert refreshed.status == "canceled"
            assert refreshed.error == "refresh_failed"
            # Cache preserved untouched, so the (non-refresh) gate keeps seeing
            # the same last-known "canceled" status -> workers stay blocked.
            assert svc._cached_status.status == "canceled"
            assert svc._cached_status.error is None
            gate_status = await svc.get_status()
            assert gate_status.status == "canceled"
            assert svc.is_worker_access_allowed(gate_status) is False


# ===========================================================================
# is_worker_access_allowed()
# ===========================================================================

class TestIsWorkerAccessAllowed:
    @pytest.mark.parametrize("status_value", ["active", "trialing", "past_due"])
    def test_allowed_statuses(self, status_value):
        env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)):
            status_obj = svc.SubscriptionStatus(status=status_value, current_period_end=None, days_remaining=None, mode="live")
            assert svc.is_worker_access_allowed(status_obj) is True

    @pytest.mark.parametrize("status_value", ["canceled", "unpaid", "incomplete_expired"])
    def test_blocked_statuses(self, status_value):
        env = _env({"STRIPE_API_KEY": "rk_test_123", "STRIPE_SUBSCRIPTION_ID": "sub_123"})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)):
            status_obj = svc.SubscriptionStatus(status=status_value, current_period_end=None, days_remaining=None, mode="live")
            assert svc.is_worker_access_allowed(status_obj) is False

    def test_always_allowed_when_disabled_even_if_status_would_block(self):
        env = _env({})
        with patch("api.services.subscription_service.os.getenv", side_effect=lambda k, d=None: env.get(k, d)):
            status_obj = svc.SubscriptionStatus(status="canceled", current_period_end=None, days_remaining=None, mode="live")
            assert svc.is_worker_access_allowed(status_obj) is True
