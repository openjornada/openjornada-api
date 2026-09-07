"""
User-facing error contract: ``detail: { "error_code", "message" }``.

The API keeps FastAPI's ``detail`` channel but turns it into an object with a
stable, machine-readable ``error_code`` (namespaced per domain: ``auth.*``,
``worker.*``, ``company.*``, ``absence.*``, ``change_request.*``, ``sms.*``,
``settings.*``) plus a default human-readable ``message`` (Spanish).

Frontends translate from ``error_code`` and fall back to ``message`` for
unknown codes. HTTP status codes are unchanged by this contract; non-migrated
endpoints keep returning a plain-string ``detail``.

Canonical registry: ``docs/error-codes.md``.
"""

from typing import NoReturn, Optional

from fastapi import HTTPException


def api_error(
    status_code: int,
    error_code: str,
    message: str,
    headers: Optional[dict] = None,
) -> HTTPException:
    """Build an HTTPException whose detail follows the error_code contract."""
    return HTTPException(
        status_code=status_code,
        detail={"error_code": error_code, "message": message},
        headers=headers,
    )


def raise_api_error(
    status_code: int,
    error_code: str,
    message: str,
    headers: Optional[dict] = None,
) -> NoReturn:
    """Raise :func:`api_error` (convenience for call sites)."""
    raise api_error(status_code, error_code, message, headers=headers)
