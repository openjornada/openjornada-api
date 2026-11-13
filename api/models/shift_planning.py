"""
Pydantic schemas for shift planning (module opt-in per company).

Vocabulary note: "shift planning" uses *shift type* / *planned shift* / *published
plan*. The 5-state clock model (`WorkerShiftStates`, "shift state") is a different
concept and is untouched by this module.

Dates are stored as "YYYY-MM-DD" strings and times as "HH:MM" local strings —
no timezone anywhere (design D4).
"""

from datetime import date as date_type
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

# Regex patterns reused across the module
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


def _parse_date(value: str) -> date_type:
    return date_type.fromisoformat(value)


class ShiftTiming(BaseModel):
    """Start/end/break copied into every planned shift at assignment time (D2)."""

    start_time: str = Field(..., pattern=TIME_PATTERN, description="Start time HH:MM")
    end_time: str = Field(..., pattern=TIME_PATTERN, description="End time HH:MM (next day if <= start_time)")
    break_minutes: int = Field(0, ge=0, le=1440, description="Break minutes (not charged to the worker)")


class ShiftTypeBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    short_code: str = Field(..., min_length=1, max_length=4)
    color: str = Field(..., pattern=r"^#[0-9A-Fa-f]{6}$")


class ShiftTypeCreate(ShiftTypeBase, ShiftTiming):
    company_id: str


class ShiftTypeUpdate(BaseModel):
    """Partial update; planned shifts keep their copied values (D2)."""

    start_time: Optional[str] = Field(None, pattern=TIME_PATTERN)
    end_time: Optional[str] = Field(None, pattern=TIME_PATTERN)
    break_minutes: Optional[int] = Field(None, ge=0, le=1440)
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    short_code: Optional[str] = Field(None, min_length=1, max_length=4)
    color: Optional[str] = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")


class ShiftTypeResponse(ShiftTypeBase, ShiftTiming):
    id: str
    company_id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CellShift(BaseModel):
    """
    A shift stored inside a roster cell (times copied at assignment, D2).

    Type-based shifts carry ``shift_type_id`` and a copy of the type's name,
    code and color; custom ones (spec: "horas personalizadas sin tipo") have
    none of those.
    """

    shift_type_id: Optional[str] = None
    name: Optional[str] = None
    short_code: Optional[str] = None
    color: Optional[str] = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    start_time: str = Field(..., pattern=TIME_PATTERN)
    end_time: str = Field(..., pattern=TIME_PATTERN)
    break_minutes: int = Field(0, ge=0, le=1440)


class CellWriteItem(BaseModel):
    """
    One shift of a cell replacement. With ``shift_type_id`` the type's name,
    code, color and hours are copied server-side (D2); the optional fields
    override that copy, and a shift with no type is fully custom ("sin tipo").
    """

    shift_type_id: Optional[str] = None
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    short_code: Optional[str] = Field(None, min_length=1, max_length=4)
    color: Optional[str] = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    start_time: Optional[str] = Field(None, pattern=TIME_PATTERN)
    end_time: Optional[str] = Field(None, pattern=TIME_PATTERN)
    break_minutes: Optional[int] = Field(None, ge=0, le=1440)


class CellWriteRequest(BaseModel):
    """Full replacement of a cell's shifts (PUT semantics, D3)."""

    company_id: str
    worker_id: str
    date: str = Field(..., pattern=DATE_PATTERN)
    shifts: List[CellWriteItem] = Field(default_factory=list, max_length=10)

    @field_validator("date")
    @classmethod
    def valid_date(cls, v: str) -> str:
        _parse_date(v)
        return v


class RosterWorker(BaseModel):
    """
    One row of the roster grid.

    ``cells`` maps "YYYY-MM-DD" to that day's draft shifts (only days that
    have a cell); ``planned_minutes`` is the total in the range.
    """

    id: str
    first_name: str
    last_name: str
    cells: dict = Field(default_factory=dict, description="Draft shifts by date: {\"YYYY-MM-DD\": [CellShift]}")
    planned_minutes: int = 0
    pending_dates: List[str] = Field(default_factory=list, description="Dates in the range whose draft differs from the published version")


class WarningItem(BaseModel):
    worker_id: str
    date: str
    code: str = Field(..., pattern=r"^(rest_under_12h|absence_overlap)$")
    message: str


class RosterResponse(BaseModel):
    company_id: str
    start_date: str
    end_date: str
    pending_changes: int = Field(..., description="Cells that differ from what was published (whole company)")
    workers: List[RosterWorker]
    warnings: List[WarningItem] = Field(default_factory=list)


class ShiftPlanPublishRequest(BaseModel):
    company_id: str
    start_date: str = Field(..., pattern=DATE_PATTERN)
    end_date: str = Field(..., pattern=DATE_PATTERN)
    acknowledge_warnings: bool = False
    notify: bool = True

    @field_validator("start_date", "end_date")
    @classmethod
    def valid_date(cls, v: str) -> str:
        _parse_date(v)
        return v


class CopyWeekRequest(BaseModel):
    company_id: str
    source_week_start: str = Field(..., pattern=DATE_PATTERN, description="Monday of the week to copy from")
    target_week_start: str = Field(..., pattern=DATE_PATTERN, description="Monday of the week to copy into")
    conflict: str = Field("skip", pattern=r"^(skip|overwrite)$")

    @field_validator("source_week_start", "target_week_start")
    @classmethod
    def valid_date(cls, v: str) -> str:
        _parse_date(v)
        return v


class CopyWeekResult(BaseModel):
    copied: int = 0
    skipped: int = 0


class PublicationChange(BaseModel):
    """Audit entry of one cell, before and after publish (D8)."""

    worker_id: str
    date: str
    before: List[CellShift] = Field(default_factory=list)
    after: List[CellShift] = Field(default_factory=list)


class PublicationResponse(BaseModel):
    id: str
    company_id: str
    start_date: str
    end_date: str
    published_at: str
    published_by: Optional[str] = None
    notified: bool
    changes: List[PublicationChange] = Field(default_factory=list)
    acknowledged_warnings: List[WarningItem] = Field(default_factory=list)


class ShiftPlanDay(BaseModel):
    """One day of the worker-facing view: published shifts + type color."""

    date: str
    shifts: List[CellShift] = Field(default_factory=list)


class MyShiftPlanRequest(BaseModel):
    email: str
    password: str
    company_id: str
    start_date: str = Field(..., pattern=DATE_PATTERN)
    end_date: str = Field(..., pattern=DATE_PATTERN)

    @field_validator("start_date", "end_date")
    @classmethod
    def valid_date(cls, v: str) -> str:
        _parse_date(v)
        return v


class MyShiftPlanResponse(BaseModel):
    company_id: str
    company_name: str
    start_date: str
    end_date: str
    days: List[ShiftPlanDay] = Field(default_factory=list)
