"""
Gating for the optional (opt-in) absence & vacation management module.

The module is enabled per company via ``Company.absence_management_enabled``
(default ``False``, see design decision D9). Every endpoint under
``/api/absences`` and ``/api/absence-policies`` must reject operations for a
company that doesn't have the module active — this is the *authoritative*
gate; admin and webapp UIs additionally hide the feature client-side.
"""
from bson.objectid import ObjectId
from fastapi import HTTPException, status

from ..database import db


async def ensure_absence_module_enabled(company_id: str) -> dict:
    """
    Verify that ``company_id`` exists and has the absence module active.

    Args:
        company_id: MongoDB _id (string) of the company.

    Returns:
        The raw MongoDB company document.

    Raises:
        HTTPException 404: If the company doesn't exist (or is deleted).
        HTTPException 403: If the company exists but the module is disabled.
    """
    try:
        oid = ObjectId(company_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )

    company = await db.Companies.find_one({"_id": oid, "deleted_at": None})
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )

    if not company.get("absence_management_enabled", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El módulo de gestión de ausencias no está activo para esta empresa",
        )

    return company


async def require_absence_module(company_id: str) -> str:
    """
    FastAPI dependency that gates a route by ``company_id``.

    Binds to a path parameter named ``company_id`` when the route declares
    one (e.g. ``/absence-policies/{company_id}``), or to a required query
    parameter otherwise (e.g. ``/absences/?company_id=...``) — FastAPI
    resolves dependency parameters exactly like path operation parameters.
    """
    await ensure_absence_module_enabled(company_id)
    return company_id
