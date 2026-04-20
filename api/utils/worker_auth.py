"""
Shared worker authentication helpers.

These functions are used by multiple routers (reports, change_requests, workers)
to authenticate a worker by email/password and verify company access.
"""

from fastapi import HTTPException, status

from ..auth.auth_handler import verify_password
from ..database import db


async def _authenticate_worker(email: str, password: str) -> dict:
    """
    Authenticate a worker by email and hashed password.

    Args:
        email: Worker's registered email address.
        password: Plain-text password to verify against the stored hash.

    Returns:
        The raw MongoDB worker document.

    Raises:
        HTTPException 401: If the worker is not found or the password is wrong.
    """
    worker = await db.Workers.find_one({"email": email, "deleted_at": None})
    if not worker or not verify_password(password, worker["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
        )
    return worker


def _verify_worker_company_access(worker: dict, company_id: str) -> None:
    """
    Verify that *worker* belongs to *company_id*.

    Args:
        worker: Raw MongoDB worker document (must contain ``company_ids``).
        company_id: String company identifier to check access against.

    Raises:
        HTTPException 403: If the worker does not belong to the company.
    """
    worker_company_ids = [str(cid) for cid in worker.get("company_ids", [])]
    if company_id not in worker_company_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permisos para acceder a los datos de esta empresa",
        )
