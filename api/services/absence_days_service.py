"""
AbsenceDaysService - Computes ``days_computed`` for an absence request
according to the company policy's computation mode (Fase 1: no holiday
calendar yet, see design decision D5).
"""
from datetime import date

from ..models.absences import ComputationMode, DayPortion


def compute_absence_days(
    start_date: date,
    end_date: date,
    computation: ComputationMode,
    is_partial: bool,
    day_portion: DayPortion,
    is_hourly: bool = False,
) -> float:
    """
    Compute the number of days an absence request consumes.

    Args:
        start_date: First day of the absence (inclusive).
        end_date: Last day of the absence (inclusive).
        computation: ``business_days`` (Mon-Fri) or ``calendar_days``.
        is_partial: Whether the request is for half a day.
        day_portion: ``full``, ``morning`` or ``afternoon``.
        is_hourly: Whether the request only specifies a time slot (``start_time``/
            ``end_time``). Fase 1 has no per-employee working hours yet, so
            hourly requests are tracked but, by default, don't consume full
            days from the balance (design decision D5).

    Returns:
        Number of days (0.5 for a half day, 0 for an hourly-only request,
        otherwise the count of days in range per the computation mode).
    """
    if is_hourly:
        return 0.0

    if is_partial and day_portion in ("morning", "afternoon"):
        return 0.5

    if computation == "calendar_days":
        return float((end_date - start_date).days + 1)

    # business_days: count Monday(0)-Friday(4) inclusive, no holidays (Fase 1).
    business_days = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            business_days += 1
        current = date.fromordinal(current.toordinal() + 1)
    return float(business_days)
