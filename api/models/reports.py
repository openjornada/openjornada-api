from enum import Enum
from pydantic import BaseModel, Field, AwareDatetime, EmailStr
from typing import Optional, List, Literal
from datetime import date


class ExportFormat(str, Enum):
    """Supported export formats for labour inspection reports."""

    CSV = "csv"
    XLSX = "xlsx"
    PDF = "pdf"


class DailyWorkSummary(BaseModel):
    """Daily work summary for a single worker."""

    date: date
    worker_id: str
    worker_name: str
    worker_id_number: str  # DNI/NIE del trabajador
    company_id: str
    company_name: str

    first_entry: Optional[AwareDatetime] = None
    last_exit: Optional[AwareDatetime] = None

    total_worked_minutes: float = 0.0
    total_pause_minutes: float = 0.0       # Outside-shift pauses (not counted as work)
    total_break_minutes: float = 0.0       # Inside-shift breaks (counted as work)

    records_count: int = 0
    has_open_session: bool = False
    is_modified: bool = False


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
    timezone: str = "Europe/Madrid"


class ExportRequest(BaseModel):
    """Request body for report export endpoints."""

    company_id: str
    year: int = Field(..., ge=2020, le=2035)
    month: int = Field(..., ge=1, le=12)
    worker_id: Optional[str] = None
    format: ExportFormat = ExportFormat.PDF
    timezone: str = "Europe/Madrid"


class WorkerReportRequest(BaseModel):
    """Request body for a worker to access their own monthly report (email + password auth)."""

    email: EmailStr
    password: str
    company_id: str
    year: int = Field(..., ge=2020, le=2035)
    month: int = Field(..., ge=1, le=12)


class MonthlySignatureRequest(BaseModel):
    """Request body for a worker to digitally sign their monthly report."""

    email: EmailStr
    password: str
    company_id: str
    year: int = Field(..., ge=2020, le=2035)
    month: int = Field(..., ge=1, le=12)


class MonthlySignatureResponse(BaseModel):
    """Response returned after a worker successfully signs their monthly report."""

    id: str
    worker_id: str
    company_id: str
    year: int
    month: int
    status: Literal["signed"]
    signed_at: AwareDatetime


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
