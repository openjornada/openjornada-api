from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime


# ============================================================================
# SMS Log Models
# ============================================================================

class SmsLogEntry(BaseModel):
    """Record of each SMS sent."""
    worker_id: str
    company_id: str
    phone_number: str
    time_record_entry_id: str
    message_type: Literal["shift_reminder", "custom"] = "shift_reminder"
    reminder_number: int  # 1st, 2nd, 3rd, etc.
    status: Literal["sent", "delivered", "failed"] = "sent"
    provider: str = "labsmobile"
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None
    cost_credits: float = 1.0
    # Denormalized worker fields stored at send time for history display
    worker_name: Optional[str] = None
    worker_id_number: Optional[str] = None  # DNI
    message: Optional[str] = None  # Full SMS text
    created_at: datetime
    delivered_at: Optional[datetime] = None


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


class SmsCompanyConfigUpdate(BaseModel):
    """Partial update for company SMS config."""
    enabled: Optional[bool] = None
    first_reminder_minutes: Optional[int] = Field(default=None, ge=30, le=1440)
    reminder_frequency_minutes: Optional[int] = Field(default=None, ge=30, le=720)
    max_reminders_per_day: Optional[int] = Field(default=None, ge=1, le=20)
    active_hours_start: Optional[str] = None
    active_hours_end: Optional[str] = None
    timezone: Optional[str] = None


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
    api_token: str  # LabsMobile: Base64(username:api_key)
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
    """Response for SMS reminder template."""
    template: str
    default_template: str
    available_tags: list[dict]


class SmsTemplateUpdate(BaseModel):
    """Request body for updating the SMS reminder template."""
    template: str = Field(..., max_length=480)
