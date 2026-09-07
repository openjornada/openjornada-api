import re
from pydantic import BaseModel, Field, field_validator
from typing import Dict, List, Optional, Literal
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# ============================================================================
# Shared Constants
# ============================================================================

# Reminder templates by locale (global per tenant, differentiated by locale).
# The recipient company's ``notification_language`` selects which one is used.
#
# All texts are pure GSM-7 (no á í ó ú or other UCS-2 characters) so the 160
# character single-segment budget is preserved. Keep the four markers intact:
# ``{%worker_name%}``, ``{%company_name%}``, ``{%hours_open%}``,
# ``{%reminder_number%}``.
DEFAULT_SMS_TEMPLATES: Dict[str, str] = {
    "es": (
        "OpenJornada: Hola {%worker_name%}, llevas {%hours_open%}h con jornada "
        "abierta en {%company_name%}. Registra tu salida. "
        "Aviso {%reminder_number%}."
    ),
    "en": (
        "OpenJornada: Hi {%worker_name%}, your shift at {%company_name%} is "
        "open {%hours_open%}h. If done, register your exit. "
        "Reminder {%reminder_number%}."
    ),
    "ca": (
        "OpenJornada: Hola {%worker_name%}, portes {%hours_open%}h de jornada "
        "oberta a {%company_name%}. Registra la sortida. "
        "Avis {%reminder_number%}."
    ),
}

# Kept for backwards compatibility with any caller that still expects the
# single Spanish text. New code must use ``DEFAULT_SMS_TEMPLATES``.
DEFAULT_SMS_TEMPLATE = DEFAULT_SMS_TEMPLATES["es"]

SMS_MARKER_TAGS = ("{%worker_name%}", "{%company_name%}", "{%hours_open%}", "{%reminder_number%}")

# Legacy field name (single global string template) kept for the lazy
# migration to the per-locale map.
LEGACY_SMS_TEMPLATE_FIELD = "sms_reminder_template"
SMS_TEMPLATES_FIELD = "sms_reminder_templates"


def resolve_sms_reminder_templates(settings_doc: Optional[dict]) -> Dict[str, str]:
    """Lazily migrate the legacy single template into the per-locale map.

    Reads a raw MongoDB Settings document and returns the *customized* locale
    map. If the legacy ``sms_reminder_template`` string exists and the map has
    no entry for ``es``, the legacy text is treated as the ``es`` template.
    No write is performed (idempotent, migration-free).
    """
    if not settings_doc:
        return {}
    templates = dict(settings_doc.get(SMS_TEMPLATES_FIELD) or {})
    legacy = settings_doc.get(LEGACY_SMS_TEMPLATE_FIELD)
    if isinstance(legacy, str) and legacy and "es" not in templates:
        templates["es"] = legacy
    return templates


def resolve_reminder_template(
    custom_templates: Dict[str, str],
    locale: Optional[str],
) -> str:
    """Reminder template text for *locale*.

    Chain: custom[locale] -> DEFAULT_SMS_TEMPLATES[locale] ->
    DEFAULT_SMS_TEMPLATES["es"].
    """
    text = custom_templates.get(locale or "")
    if isinstance(text, str) and text:
        return text
    return DEFAULT_SMS_TEMPLATES.get(locale or "", DEFAULT_SMS_TEMPLATES["es"])


AVAILABLE_TAGS = [
    {"tag": "{%worker_name%}", "description": "Nombre completo del trabajador", "example": "Juan García"},
    {"tag": "{%company_name%}", "description": "Nombre de la empresa", "example": "HappyAndroids"},
    {"tag": "{%hours_open%}", "description": "Horas con jornada abierta", "example": "4.5"},
    {"tag": "{%reminder_number%}", "description": "Número del recordatorio", "example": "2"},
]

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ============================================================================
# SMS Log Models
# ============================================================================

class SmsSendRequest(BaseModel):
    """Request body for sending a custom SMS to a worker."""
    message: str = Field(..., min_length=1, max_length=480)


class SmsSendResponse(BaseModel):
    """Response for a custom SMS send attempt."""
    success: bool
    error_message: Optional[str] = None


class SmsMessage(BaseModel):
    """Frontend-facing SMS message/log entry (used in history)."""
    id: str
    worker_id: str
    worker_name: Optional[str] = None
    worker_id_number: Optional[str] = None  # DNI
    phone_number: str
    message: Optional[str] = None
    status: str
    sent_at: Optional[datetime] = None  # maps from created_at
    delivered_at: Optional[datetime] = None
    error_message: Optional[str] = None


class SmsLogResponse(BaseModel):
    """API response for an SMS log entry (legacy / admin routes)."""
    id: str
    worker_id: str
    company_id: str
    phone_number: str
    time_record_entry_id: str
    message_type: str
    reminder_number: int
    status: str
    provider: str
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None
    cost_credits: float
    worker_name: Optional[str] = None
    worker_id_number: Optional[str] = None
    message: Optional[str] = None
    created_at: datetime
    delivered_at: Optional[datetime] = None


class SmsLogListResponse(BaseModel):
    """Paginated list of SMS log entries (legacy / admin routes)."""
    items: list[SmsLogResponse]
    total: int
    page: int
    page_size: int


class SmsHistoryResponse(BaseModel):
    """Frontend-facing paginated SMS history response."""
    messages: list[SmsMessage]
    total: int
    skip: int
    limit: int


# ============================================================================
# SMS Credits Models
# ============================================================================

class SmsCreditsResponse(BaseModel):
    """Frontend-facing SMS credits response."""
    balance: float
    currency: str = "EUR"
    unlimited: bool = False
    provider_enabled: bool = False
    last_updated: Optional[str] = None  # ISO 8601 string


# ============================================================================
# SMS Config Models
# ============================================================================

class SmsCompanyConfig(BaseModel):
    """Per-company SMS reminder configuration."""
    enabled: bool = False
    first_reminder_minutes: int = Field(default=240, ge=30, le=1440)
    reminder_frequency_minutes: int = Field(default=60, ge=30, le=720)
    max_reminders_per_day: int = Field(default=5, ge=1, le=20)
    active_hours_start: str = "08:00"  # HH:MM
    active_hours_end: str = "23:00"    # HH:MM
    timezone: str = "Europe/Madrid"

    @field_validator("active_hours_start", "active_hours_end")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        if not _TIME_PATTERN.match(v):
            raise ValueError("El formato de hora debe ser HH:MM (00:00-23:59)")
        return v

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(f"Zona horaria inválida: {v}")
        return v


class SmsCompanyConfigUpdate(BaseModel):
    """Partial update for company SMS config."""
    enabled: Optional[bool] = None
    first_reminder_minutes: Optional[int] = Field(default=None, ge=30, le=1440)
    reminder_frequency_minutes: Optional[int] = Field(default=None, ge=30, le=720)
    max_reminders_per_day: Optional[int] = Field(default=None, ge=1, le=20)
    active_hours_start: Optional[str] = None
    active_hours_end: Optional[str] = None
    timezone: Optional[str] = None

    @field_validator("active_hours_start", "active_hours_end")
    @classmethod
    def validate_time_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _TIME_PATTERN.match(v):
            raise ValueError("El formato de hora debe ser HH:MM (00:00-23:59)")
        return v

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(f"Zona horaria inválida: {v}")
        return v


class SmsWorkerConfig(BaseModel):
    """Per-worker SMS opt-in/out configuration."""
    worker_id: Optional[str] = None
    sms_enabled: bool = True


class SmsWorkerConfigUpdate(BaseModel):
    """Partial update for worker SMS config."""
    sms_enabled: Optional[bool] = None


# ============================================================================
# SMS Provider Config (stored in Settings)
# ============================================================================

class SmsProviderConfigInput(BaseModel):
    """SMS provider configuration input (plain credentials)."""
    provider: Literal["labsmobile"] = "labsmobile"
    api_token: Optional[str] = None  # LabsMobile: Base64(username:api_key)
    sender_id: str = "OpenJornada"
    enabled: bool = True


class SmsProviderConfigStored(BaseModel):
    """SMS provider configuration as stored in DB (encrypted credentials)."""
    provider: Literal["labsmobile"] = "labsmobile"
    api_token_encrypted: str
    sender_id: str = "OpenJornada"
    enabled: bool = True


class SmsProviderConfigResponse(BaseModel):
    """SMS provider configuration response (hides credentials)."""
    provider: str
    sender_id: str
    enabled: bool
    configured: bool = False


# ============================================================================
# SMS Stats / Dashboard Models
# ============================================================================

class SmsStats(BaseModel):
    """Frontend-facing SMS statistics."""
    sent_today: int
    failed_today: int
    pending: int
    sent_this_month: int


class SmsDashboardCompanyStats(BaseModel):
    """Per-company stats for SMS dashboard."""
    company_id: str
    company_name: str
    sent_today: int
    sent_this_week: int
    sent_this_month: int
    failed_this_month: int


class SmsDashboardResponse(BaseModel):
    """Aggregate SMS dashboard statistics."""
    total_sent_today: int
    total_sent_this_week: int
    total_sent_this_month: int
    total_failed_this_month: int
    unlimited_balance: bool
    companies: list[SmsDashboardCompanyStats]
    provider_enabled: bool
    provider_name: str


# ============================================================================
# SMS Template Models
# ============================================================================

class SmsTemplateResponse(BaseModel):
    """Response for SMS reminder templates (map by locale).

    ``templates`` contains only the locales the admin has customized (plus the
    lazily migrated legacy ``es`` text); any locale absent from it uses its
    entry in ``default_templates``.
    """
    templates: Dict[str, str]
    default_templates: Dict[str, str]
    supported_locales: List[str]
    available_tags: list[dict]


class SmsTemplateUpdate(BaseModel):
    """Request body for updating the SMS reminder template of one locale.

    ``locale`` is optional for backwards compatibility: when omitted the
    update targets ``es``. The single-segment fit (<=160 GSM-7 / <=70 UCS-2
    chars) is validated by the router so failures come back as a proper
    HTTP error instead of a 422 schema error.
    """
    locale: Optional[str] = Field(None, min_length=2, max_length=5)
    template: str = Field(..., min_length=10)
