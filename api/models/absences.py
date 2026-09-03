"""
Models for the absence & vacation management module (Fase 1 / MVP).

Clones the style of ``models/change_requests.py``: an enum status, a clear
separation between the payload a worker sends to create a request and the
full document stored in MongoDB, plus a worker-facing response that omits
``admin_internal_notes``.
"""
from datetime import date, time
from enum import Enum
from typing import List, Literal, Optional

from pydantic import AwareDatetime, BaseModel, EmailStr, Field

DayPortion = Literal["full", "morning", "afternoon"]
ComputationMode = Literal["business_days", "calendar_days"]
ReferenceYearMode = Literal["calendar", "hire_date"]


class AbsenceStatus(str, Enum):
    """Absence request status enum."""
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Absence policy
# ---------------------------------------------------------------------------


class BlackoutPeriod(BaseModel):
    """A date range in which new absences cannot be requested (e.g. peak season)."""
    name: str = Field(..., min_length=1, max_length=200)
    start_date: date
    end_date: date


class AbsenceType(BaseModel):
    """A configurable absence type within a company's catalogue."""
    code: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=200)
    deducts_balance: bool = False
    is_paid: bool = True
    requires_attachment: bool = False
    max_days: Optional[float] = None
    color: str = "#3B82F6"


def default_absence_types() -> List[AbsenceType]:
    """Default, editable absence type catalogue seeded for every new policy."""
    return [
        AbsenceType(
            code="vacation", name="Vacaciones",
            deducts_balance=True, is_paid=True, requires_attachment=False,
            color="#3B82F6",
        ),
        AbsenceType(
            code="personal_matters", name="Asuntos propios",
            deducts_balance=True, is_paid=True, requires_attachment=False,
            color="#8B5CF6",
        ),
        AbsenceType(
            code="paid_leave", name="Permiso retribuido",
            deducts_balance=False, is_paid=True, requires_attachment=True,
            color="#10B981",
        ),
        AbsenceType(
            code="justified_absence", name="Ausencia justificada",
            deducts_balance=False, is_paid=True, requires_attachment=True,
            color="#F59E0B",
        ),
        AbsenceType(
            code="unjustified_absence", name="Ausencia no justificada",
            deducts_balance=False, is_paid=False, requires_attachment=False,
            color="#EF4444",
        ),
    ]


class AbsencePolicyBase(BaseModel):
    """Fields shared by create/update/response policy schemas."""
    annual_vacation_days: float = Field(22, ge=0, le=365)
    computation: ComputationMode = "business_days"
    reference_year: ReferenceYearMode = "calendar"
    min_advance_days: int = Field(0, ge=0)
    allow_half_day: bool = True
    allow_hourly: bool = False
    max_overlap_per_company: Optional[int] = Field(None, ge=1)
    blackout_periods: List[BlackoutPeriod] = Field(default_factory=list)
    absence_types: List[AbsenceType] = Field(default_factory=default_absence_types)


class AbsencePolicyCreate(AbsencePolicyBase):
    """Schema for creating (or replacing) a company's absence policy."""
    pass


class AbsencePolicyUpdate(BaseModel):
    """Schema for partially updating a company's absence policy."""
    annual_vacation_days: Optional[float] = Field(None, ge=0, le=365)
    computation: Optional[ComputationMode] = None
    reference_year: Optional[ReferenceYearMode] = None
    min_advance_days: Optional[int] = Field(None, ge=0)
    allow_half_day: Optional[bool] = None
    allow_hourly: Optional[bool] = None
    max_overlap_per_company: Optional[int] = Field(None, ge=1)
    blackout_periods: Optional[List[BlackoutPeriod]] = None
    absence_types: Optional[List[AbsenceType]] = None


class AbsencePolicyResponse(AbsencePolicyBase):
    """Absence policy response model (converts _id to id)."""
    id: str
    company_id: str
    created_at: AwareDatetime
    updated_at: AwareDatetime


# ---------------------------------------------------------------------------
# Absence request lifecycle
# ---------------------------------------------------------------------------


class AbsenceRequestCreate(BaseModel):
    """Schema for a worker to create an absence request (email/password auth)."""
    email: EmailStr
    password: str
    company_id: str
    absence_type_code: str
    start_date: date
    end_date: date
    is_partial: bool = False
    day_portion: DayPortion = "full"
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    worker_comment: Optional[str] = Field(None, max_length=1000)
    attachment_id: Optional[str] = None


class AbsenceUpdate(BaseModel):
    """Schema for an admin to approve/reject an absence request."""
    status: AbsenceStatus
    admin_internal_notes: Optional[str] = None
    admin_public_comment: Optional[str] = None


class AbsenceValidationIssue(BaseModel):
    """A single validation finding for an absence request."""
    code: Literal[
        "OVERLAP_ABSENCE",
        "BLACKOUT_PERIOD",
        "MIN_ADVANCE_NOT_MET",
        "INSUFFICIENT_BALANCE",
        "ATTACHMENT_REQUIRED",
        "MAX_OVERLAP_EXCEEDED",
        "TIME_RECORDS_EXIST",
    ]
    message: str
    blocking: bool


class AbsenceInDB(BaseModel):
    """Full absence request model as stored in MongoDB."""
    worker_id: str
    worker_email: str
    worker_first_name: str
    worker_last_name: str

    company_id: str
    company_name: str

    absence_type_code: str
    absence_type_name: str
    # Snapshot of the type's deducts_balance at request time, so later edits
    # to the catalogue don't retroactively change historical balances.
    deducts_balance: bool

    start_date: date
    end_date: date
    is_partial: bool = False
    day_portion: DayPortion = "full"
    start_time: Optional[time] = None
    end_time: Optional[time] = None

    worker_comment: Optional[str] = None
    attachment_id: Optional[str] = None
    days_computed: float

    status: AbsenceStatus = AbsenceStatus.PENDING
    created_at: AwareDatetime
    created_by: str
    updated_at: AwareDatetime
    updated_by: str

    # Aprobación/Rechazo (auditoría inline)
    reviewed_by_admin_id: Optional[str] = None
    reviewed_by_admin_email: Optional[str] = None
    reviewed_at: Optional[AwareDatetime] = None
    admin_internal_notes: Optional[str] = None  # Solo admin
    admin_public_comment: Optional[str] = None  # Se envía en email


class AbsenceResponse(BaseModel):
    """Absence response model for admins (converts _id to id)."""
    id: str
    worker_id: str
    worker_email: str
    worker_first_name: str
    worker_last_name: str

    company_id: str
    company_name: str

    absence_type_code: str
    absence_type_name: str
    deducts_balance: bool

    start_date: date
    end_date: date
    is_partial: bool
    day_portion: DayPortion
    start_time: Optional[time] = None
    end_time: Optional[time] = None

    worker_comment: Optional[str] = None
    attachment_id: Optional[str] = None
    days_computed: float

    status: str
    created_at: AwareDatetime
    updated_at: AwareDatetime

    reviewed_by_admin_id: Optional[str] = None
    reviewed_by_admin_email: Optional[str] = None
    reviewed_at: Optional[AwareDatetime] = None
    admin_internal_notes: Optional[str] = None
    admin_public_comment: Optional[str] = None

    # Solo presente en GET /absences/{id} cuando la solicitud sigue PENDING
    validation_errors: Optional[List[AbsenceValidationIssue]] = None


class WorkerAbsenceResponse(BaseModel):
    """Absence response for the worker — excludes admin_internal_notes."""
    id: str
    company_id: str
    company_name: str
    absence_type_code: str
    absence_type_name: str
    start_date: date
    end_date: date
    is_partial: bool
    day_portion: DayPortion
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    worker_comment: Optional[str] = None
    attachment_id: Optional[str] = None
    days_computed: float
    status: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    reviewed_at: Optional[AwareDatetime] = None
    admin_public_comment: Optional[str] = None


# ---------------------------------------------------------------------------
# Worker-facing request wrappers (email + password authentication)
# ---------------------------------------------------------------------------


class WorkerAbsencesRequest(BaseModel):
    """Request body for a worker to retrieve their own absence history."""
    email: EmailStr
    password: str
    company_id: Optional[str] = None
    status_filter: Optional[AbsenceStatus] = None
    limit: int = Field(50, ge=1, le=100)


class WorkerAbsenceBalanceRequest(BaseModel):
    """Request body for a worker to retrieve their own vacation balance."""
    email: EmailStr
    password: str
    company_id: str
    year: Optional[int] = Field(None, ge=2020, le=2035)


class WorkerAbsenceTypesRequest(BaseModel):
    """Request body for a worker to retrieve their company's absence type catalogue."""
    email: EmailStr
    password: str
    company_id: str


class WorkerAbsenceCancelRequest(BaseModel):
    """Request body for a worker to cancel one of their own pending requests."""
    email: EmailStr
    password: str


class WorkerAbsenceCalendarRequest(BaseModel):
    """Request body for a worker to view the team calendar (privacy-limited)."""
    email: EmailStr
    password: str
    company_id: str
    start_date: date
    end_date: date


# ---------------------------------------------------------------------------
# Balance & calendar
# ---------------------------------------------------------------------------


class AbsenceBalance(BaseModel):
    """Vacation balance for a worker, computed on the fly for a reference year."""
    year: int
    reference_year_mode: ReferenceYearMode
    period_start: date
    period_end: date
    total_days: float
    taken_days: float
    pending_days: float
    available_days: float


class AbsenceCalendarEntryAdmin(BaseModel):
    """Full calendar entry — admin view (includes type and worker identity)."""
    absence_id: str
    worker_id: str
    worker_name: str
    absence_type_code: str
    absence_type_name: str
    start_date: date
    end_date: date
    status: str


class AbsenceCalendarEntryWorker(BaseModel):
    """Privacy-limited calendar entry — worker view (no type/reason)."""
    worker_name: str
    start_date: date
    end_date: date
