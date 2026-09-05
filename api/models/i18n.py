"""
Shared i18n primitives for the API (single extensible point).

Adding a new language means adding its code to ``SUPPORTED_LOCALES`` /
``SupportedLocale`` here, plus its email template directory and its SMS
default template. No resolution logic has to change.

Fallback policy (see openspec ``add-multilanguage-i18n`` design):

- Global system fallback: ``es``.
- Notification locale (email / SMS / push to workers):
  ``Company.notification_language`` -> ``es``.
- Worker UI locale: ``Worker.language`` -> ``Company.notification_language``
  -> ``es``.
- Admin UI locale: ``APIUser.language`` -> browser detection (client-side)
  -> ``es``; the API only exposes the stored preference.

All resolvers tolerate documents created before these fields existed (missing
attribute or ``None``), so no data migration is required.
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel

SupportedLocale = Literal["es", "en", "ca"]

SUPPORTED_LOCALES: list[str] = ["es", "en", "ca"]

DEFAULT_LOCALE: str = "es"


def is_supported_locale(value: Any) -> bool:
    """True when *value* is a locale code the system supports."""
    return isinstance(value, str) and value in SUPPORTED_LOCALES


def _field(obj: Any, name: str) -> Any:
    """Read *name* from a Pydantic model or a raw MongoDB dict.

    Returns ``None`` when the attribute/key is absent, which is how documents
    predating the field present.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _valid_or_default(value: Any) -> str:
    """Return *value* when it is a supported locale, else the global default."""
    return value if is_supported_locale(value) else DEFAULT_LOCALE


def resolve_notification_locale(company: Any) -> str:
    """Locale for content sent *to* a worker of *company*.

    Chain: ``company.notification_language`` -> ``es``. Never depends on the
    language of whoever triggers the action.
    """
    return _valid_or_default(_field(company, "notification_language"))


def resolve_worker_ui_locale(worker: Any, company: Any = None) -> str:
    """Effective UI locale of a worker, server-side part of the chain.

    Chain: ``worker.language`` -> ``company.notification_language`` -> ``es``.
    Browser detection sits between the company language and the default and is
    performed by the client, so it is not modelled here.
    """
    language = _field(worker, "language")
    if is_supported_locale(language):
        return language
    return resolve_notification_locale(company)


def resolve_admin_ui_locale(user: Any) -> Optional[str]:
    """Stored UI preference of an admin user (``None`` when unset).

    The admin frontend applies its own browser detection when this is ``None``;
    the final fallback is ``es``.
    """
    language = _field(user, "language")
    return language if is_supported_locale(language) else None


class LanguagePreferenceUpdate(BaseModel):
    """Request body for self-service language preference endpoints.

    ``language=None`` clears the preference (worker: inherit the company
    language; admin: fall back to browser detection / default). The value is a
    plain ``str`` on purpose: unsupported codes are rejected by the endpoints
    with a stable ``error_code`` (422) instead of a raw schema error.
    """

    language: Optional[str] = None
