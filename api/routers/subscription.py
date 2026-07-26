"""
Subscription router: admin-only endpoints exposing the tenant's Stripe
subscription status and Customer Portal access.
"""

import logging
import os

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth.permissions import require_admin
from ..services import subscription_service

router = APIRouter()
logger = logging.getLogger(__name__)

_STATUS_MESSAGES = {
    "active": "Suscripción al día",
    "trialing": "Suscripción al día",
    "past_due": "Pago pendiente",
    "canceled": "Suscripción caducada",
    "unpaid": "Suscripción caducada",
    "incomplete_expired": "Suscripción caducada",
}


@router.get("/subscription/status")
async def get_subscription_status(
    refresh: bool = False,
    current_user=Depends(require_admin),
):
    """Admin-only subscription status. `?refresh=true` bypasses the cache (D13)."""
    if not subscription_service.is_enabled():
        return {"enabled": False}

    current_status = subscription_service.get_status(force_refresh=refresh)
    return {
        "enabled": True,
        "status": current_status.status,
        "current_period_end": current_status.current_period_end,
        "days_remaining": current_status.days_remaining,
        "message": _STATUS_MESSAGES.get(current_status.status, current_status.status),
        "mode": current_status.mode,
    }


@router.get("/subscription/portal")
async def get_subscription_portal(
    request: Request,
    current_user=Depends(require_admin),
):
    """Admin-only Stripe Customer Portal session for this tenant."""
    if not subscription_service.is_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not configured")

    return_url = f"https://{request.url.hostname}/admin"

    try:
        session = stripe.billing_portal.Session.create(
            customer=os.getenv("STRIPE_CUSTOMER_ID"),
            return_url=return_url,
            api_key=os.getenv("STRIPE_API_KEY"),
        )
    except Exception as e:
        logger.error(f"[Subscription] Error creating billing portal session: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo crear la sesión del portal de facturación",
        )

    return {"url": session.url}
