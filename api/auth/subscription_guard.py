"""
FastAPI dependency gating worker-facing endpoints on the tenant's Stripe
subscription status (DA1). No-op when Stripe is not configured (D8).
"""

from fastapi import HTTPException, status

from ..services import subscription_service

SUBSCRIPTION_INACTIVE_DETAIL = {
    "detail": "subscription_inactive",
    "message": "Su empresa no permite el acceso a su cuenta. Contacte con su empresa.",
}


async def require_active_subscription() -> None:
    """
    Passes through untouched when Stripe is disabled for this tenant.

    Otherwise checks the (cached) subscription status and raises HTTP 402
    when access is blocked (canceled/unpaid/incomplete_expired).
    """
    if not subscription_service.is_enabled():
        return

    current_status = subscription_service.get_status()
    if not subscription_service.is_worker_access_allowed(current_status):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=SUBSCRIPTION_INACTIVE_DETAIL,
        )
