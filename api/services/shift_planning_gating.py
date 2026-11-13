"""
Gating for the optional (opt-in) shift planning module.

The module is enabled per company via ``Company.shift_planning_enabled``
(default ``False``, same pattern as the absence module). Every endpoint under
``/api/shift-types`` and ``/api/shift-plans`` must reject operations for a
company that doesn't have the module active — this is the *authoritative*
gate; admin and webapp UIs additionally hide the feature client-side.
"""
from bson.objectid import ObjectId
from fastapi import status

from ..database import db
from ..utils.errors import raise_api_error


async def ensure_shift_planning_module_enabled(company_id: str) -> dict:
    """
    Verify that ``company_id`` exists and has the shift planning module active.

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
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="shift_plan.company_not_found",
            message="Company not found",
        )

    company = await db.Companies.find_one({"_id": oid, "deleted_at": None})
    if company is None:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="shift_plan.company_not_found",
            message="Company not found",
        )

    if not company.get("shift_planning_enabled", False):
        raise_api_error(
            status_code=status.HTTP_403_FORBIDDEN,
            error_code="shift_plan.module_disabled",
            message="El módulo de planificación de turnos no está activo para esta empresa",
        )

    return company


async def require_shift_planning_module(company_id: str) -> str:
    """
    FastAPI dependency that gates a route by ``company_id`` (query or body is
    handled by the routers themselves; this is used with query parameters).
    """
    await ensure_shift_planning_module_enabled(company_id)
    return company_id
