"""
Subscription Service — reads the tenant's Stripe subscription status.

Optional feature: enabled only when STRIPE_API_KEY and STRIPE_SUBSCRIPTION_ID
are configured for this tenant. Tenants without those vars (e.g. codefriends)
keep working exactly as before, with no subscription limit whatsoever.

State lives in Stripe (pull model, D8): this service never persists a
subscription status of its own, it only caches the last read in memory for
a short TTL (D13) — safe because uvicorn runs this API without --workers
(single process per tenant container).
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import stripe

logger = logging.getLogger(__name__)

# TTL for the in-memory cache (D13): ~10 min, single cache per tenant.
_CACHE_TTL_SECONDS = 600

# DA1 — grace period policy.
_ALLOWED_STATUSES = {"active", "trialing", "past_due"}
_BLOCKED_STATUSES = {"canceled", "unpaid", "incomplete_expired"}


@dataclass
class SubscriptionStatus:
    status: str
    current_period_end: Optional[int]
    days_remaining: Optional[int]
    mode: str


# Module-level cache state.
_cached_status: Optional[SubscriptionStatus] = None
_cached_at: float = 0.0


def is_enabled() -> bool:
    """Stripe enforcement is enabled only when both vars are configured."""
    return bool(os.getenv("STRIPE_API_KEY") and os.getenv("STRIPE_SUBSCRIPTION_ID"))


def _fail_open_status() -> SubscriptionStatus:
    """Permissive status used when Stripe is disabled or unreachable."""
    return SubscriptionStatus(
        status="active",
        current_period_end=None,
        days_remaining=None,
        mode=os.getenv("SUBSCRIPTION_MODE", "live"),
    )


def _fetch_from_stripe() -> SubscriptionStatus:
    api_key = os.getenv("STRIPE_API_KEY")
    subscription_id = os.getenv("STRIPE_SUBSCRIPTION_ID")
    mode = os.getenv("SUBSCRIPTION_MODE", "live")

    subscription = stripe.Subscription.retrieve(subscription_id, api_key=api_key)
    current_period_end = subscription.get("current_period_end")
    days_remaining = None
    if current_period_end is not None:
        days_remaining = max(0, int((current_period_end - time.time()) // 86400))

    return SubscriptionStatus(
        status=subscription.get("status"),
        current_period_end=current_period_end,
        days_remaining=days_remaining,
        mode=mode,
    )


def get_status(force_refresh: bool = False) -> SubscriptionStatus:
    """
    Return the (cached) subscription status.

    force_refresh=True bypasses the cache and repopulates it — used by
    `GET /api/subscription/status?refresh=true` (D13, "Ya he pagado" button).

    Fails open on any Stripe/network error: logs and returns a permissive
    status, since fichaje (clocking in/out) is a legal obligation that a
    transient Stripe incident must never block.
    """
    global _cached_status, _cached_at

    if not is_enabled():
        return _fail_open_status()

    now = time.monotonic()
    if not force_refresh and _cached_status is not None and (now - _cached_at) < _CACHE_TTL_SECONDS:
        return _cached_status

    try:
        fetched_status = _fetch_from_stripe()
    except Exception as e:
        logger.error(f"[Subscription] Error fetching status from Stripe (fail-open): {e}")
        return _fail_open_status()

    _cached_status = fetched_status
    _cached_at = now
    return fetched_status


def is_worker_access_allowed(status: SubscriptionStatus) -> bool:
    """
    DA1 policy: active/trialing/past_due (grace period) allowed;
    canceled/unpaid/incomplete_expired blocked. Always allowed when Stripe
    is not configured for this tenant.
    """
    if not is_enabled():
        return True
    return status.status in _ALLOWED_STATUSES
