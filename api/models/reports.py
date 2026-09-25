from enum import Enum
from pydantic import BaseModel, Field, AwareDatetime, EmailStr, AfterValidator
from typing import Annotated, Optional, List, Literal
from datetime import date
import pytz


def _validate_timezone(value: str) -> str:
    """Reject IANA names the bundled pytz database does not know about."""
    if value not in pytz.all_timezones_set:
        raise ValueError(f"Unknown IANA timezone: {value}")
    return value


# Timezone chosen by an admin/inspector on the report query params. Validated
# here so an unknown zone answers 422 instead of blowing up with an
# UnknownTimeZoneError deeper in the report pipeline. Not used on the worker
# request models: there the window is resolved server-side from the worker's
# own record (api/utils/timezones.py), so an unknown zone must degrade rather
# than lock the worker out of their month.
# On a query param it MUST be written as ``Annotated[IanaTimezone, Query(...)]``
# with the default after the ``=``: in the ``timezone: IanaTimezone =
# Query(...)`` form FastAPI overwrites the field annotation with the bare
# ``str`` and the validator never runs.
IanaTimezone = Annotated[str, AfterValidator(_validate_timezone)]

# Timezone a worker's client may still send: accepted and ignored, so already
# deployed webapp versions keep working while the server delimits the month.
_IGNORED_TIMEZONE_DESC = (
    "Ignorado: la ventana del mes la resuelve el servidor con la zona horaria "
    "configurada en la ficha del trabajador. Se acepta por compatibilidad con "
    "clientes ya desplegados."
)


class ExportFormat(str, Enum):
    """Supported export formats for labour inspection reports."""

    CSV = "csv"
    XLSX = "xlsx"
    PDF = "pdf"


class ModificationEntry(BaseModel):
    """Audit record for a single admin-approved timestamp change on a time record."""

    record_id: str
    record_type: str                  # "entry" | "exit"
    original_timestamp: str           # ISO datetime UTC
    new_timestamp: str                # ISO datetime UTC (valor actual del registro)
    modified_at: str                  # cuándo se aprobó el cambio (ISO UTC)
    modified_by_admin_email: str      # email del admin que lo aprobó
    modification_reason: str          # motivo enviado por el trabajador


class DailyWorkSummary(BaseModel):
    """Daily work summary for a single worker."""

    date: date
    worker_id: str
    worker_name: str
    worker_id_number: str  # DNI/NIE del trabajador
    company_id: str
    company_name: str

    # Centro(s) de trabajo del día: valores distintos en orden cronológico,
    # unidos con " / " si el trabajador cambió de centro durante la jornada.
    work_center_name: Optional[str] = None

    first_entry: Optional[AwareDatetime] = None
    last_exit: Optional[AwareDatetime] = None

    total_worked_minutes: float = 0.0
    total_pause_minutes: float = 0.0       # Outside-shift pauses (not counted as work)
    total_break_minutes: float = 0.0       # Inside-shift breaks (counted as work)

    records_count: int = 0
    has_open_session: bool = False
    is_modified: bool = False
    modifications: List[ModificationEntry] = []

    # Absence & vacation management (Fase 1) — only populated when the
    # worker's company has the module active (absence-reporting spec).
    is_absence: bool = False
    absence_type: Optional[str] = None


class AbsenceSummaryEntry(BaseModel):
    """Summary of a single approved absence overlapping the reported period."""

    absence_type: str
    start_date: date
    end_date: date
    days_computed: float


class WorkerMonthlySummary(BaseModel):
    """Monthly work summary for a single worker."""

    worker_id: str
    worker_name: str
    worker_id_number: str
    company_id: str
    company_name: str

    year: int
    month: int

    total_days_worked: int = 0
    total_worked_minutes: float = 0.0
    total_pause_minutes: float = 0.0
    total_overtime_minutes: float = 0.0

    @property
    def total_worked_hours(self) -> float:
        """Total worked time expressed in hours."""
        return round(self.total_worked_minutes / 60, 2)

    daily_details: List[DailyWorkSummary] = Field(default_factory=list)

    # Only populated when the company has the absence module active.
    absences: List[AbsenceSummaryEntry] = Field(default_factory=list)

    signature_status: Literal["pending", "signed", "not_required"] = "pending"
    signed_at: Optional[AwareDatetime] = None
    generated_at: AwareDatetime


class CompanyMonthlySummary(BaseModel):
    """Monthly work summary for all workers in a company."""

    company_id: str
    company_name: str
    year: int
    month: int
    total_workers: int = 0

    workers: List[WorkerMonthlySummary] = Field(default_factory=list)
    generated_at: AwareDatetime


class WorkerOvertimeSummary(BaseModel):
    """Overtime summary for a single worker within a period."""

    worker_id: str
    worker_name: str
    worker_id_number: str

    total_worked_minutes: float = 0.0
    expected_minutes: float = 0.0
    overtime_minutes: float = 0.0
    days_with_overtime: int = 0

    @property
    def overtime_hours(self) -> float:
        """Total overtime expressed in hours."""
        return round(self.overtime_minutes / 60, 2)


class OvertimeReport(BaseModel):
    """Overtime report for all workers in a company for a given month."""

    company_id: str
    company_name: str
    year: int
    month: int

    workers_with_overtime: List[WorkerOvertimeSummary] = Field(default_factory=list)
    generated_at: AwareDatetime


class ReportFilters(BaseModel):
    """Common query filters for report endpoints."""

    company_id: str
    year: int = Field(..., ge=2020, le=2035)
    month: int = Field(..., ge=1, le=12)
    worker_id: Optional[str] = None
    timezone: IanaTimezone = "Europe/Madrid"


class ExportRequest(BaseModel):
    """Request body for report export endpoints."""

    company_id: str
    year: int = Field(..., ge=2020, le=2035)
    month: int = Field(..., ge=1, le=12)
    worker_id: Optional[str] = None
    format: ExportFormat = ExportFormat.PDF
    timezone: IanaTimezone = "Europe/Madrid"


class WorkerReportRequest(BaseModel):
    """Request body for a worker to access their own monthly report (email + password auth)."""

    email: EmailStr
    password: str
    company_id: str
    year: int = Field(..., ge=2020, le=2035)
    month: int = Field(..., ge=1, le=12)
    timezone: Optional[str] = Field(None, description=_IGNORED_TIMEZONE_DESC)


class MonthlySignatureRequest(BaseModel):
    """Request body for a worker to digitally sign their monthly report."""

    email: EmailStr
    password: str
    company_id: str
    year: int = Field(..., ge=2020, le=2035)
    month: int = Field(..., ge=1, le=12)
    timezone: Optional[str] = Field(None, description=_IGNORED_TIMEZONE_DESC)


class MonthlySignatureResponse(BaseModel):
    """Response returned after a worker successfully signs their monthly report."""

    id: str
    worker_id: str
    company_id: str
    year: int
    month: int
    status: Literal["signed"]
    signed_at: AwareDatetime
    content_hash: str        # SHA-256 digest of the month's records at signing time


class SignatureStatusResponse(BaseModel):
    """Signature status grouped by month for a worker."""

    pending: List[dict] = Field(
        default_factory=list,
        description="Months pending signature. Each item: {year, month, status}",
    )
    signed: List[dict] = Field(
        default_factory=list,
        description="Signed months. Each item: {year, month, status, signed_at}",
    )


class RecordIntegrity(BaseModel):
    """Result of an integrity check for a single time record."""

    record_id: str
    integrity_hash: str    # Hash stored in the database at creation time
    computed_hash: str     # Hash recomputed from current record fields
    verified: bool         # True when integrity_hash == computed_hash
    status: Optional[Literal["verified", "tampered", "legacy"]] = None
    # "verified": hash present and matches. "tampered": hash present but does
    # not match. "legacy": record predates this capability, no hash stored.


class AuditedCorrection(BaseModel):
    """Approved change-request applied to a signed month after the signature."""

    change_request_id: str
    reviewed_by_admin_email: str    # Admin who approved the correction
    reviewed_at: AwareDatetime      # When the correction was applied (UTC)
    date: str                       # ISO date (YYYY-MM-DD) of the affected record
    reason: str                     # Reason given by the worker


class MonthlySignatureVerification(BaseModel):
    """Result of verifying a monthly signature against the current records."""

    signature_id: str
    status: Literal["verified", "mismatch", "legacy", "unsupported_version"]
    # "verified": recomputed digest matches the signed one. "mismatch": it
    # does not — check audited_corrections to tell an audited fix from an
    # unexplained alteration. "legacy": signature predates this capability,
    # no digest was ever stored, nothing can be asserted about it.
    # "unsupported_version": the digest was written by an algorithm version
    # this code cannot recompute, so nothing was compared (reporting a
    # mismatch would read as an alteration that never happened).
    content_hash: str              # Digest persisted at signing ("" on legacy)
    computed_hash: str             # Digest recomputed from current records ("" on legacy)
    content_hash_version: Optional[str] = None
    timezone: Optional[str] = None  # IANA timezone the signed month was delimited with
    worker_id: str
    company_id: str
    year: int
    month: int
    signed_at: AwareDatetime
    signed_record_count: Optional[int] = None   # Records covered when signed (None on legacy)
    current_record_count: Optional[int] = None  # Records covered now (None on legacy)
    audited_corrections: List[AuditedCorrection] = Field(default_factory=list)


class WorkerExportRequest(BaseModel):
    """Request body for a worker to export their own monthly report."""

    email: EmailStr
    password: str
    company_id: str
    year: int = Field(..., ge=2020, le=2035)
    month: int = Field(..., ge=1, le=12)
    format: Literal["pdf", "csv"] = "pdf"
    timezone: Optional[str] = Field(None, description=_IGNORED_TIMEZONE_DESC)
