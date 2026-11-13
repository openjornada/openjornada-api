"""
Pure time helpers for shift planning (design D4/D5/D6). No database access:
everything here is unit-testable on its own.

Shifts are ``start_time``/``end_time`` "HH:MM" local strings. ``end <= start``
means the shift crosses midnight into the next day. To compare or overlap-test
shifts across days, each shift is projected onto absolute minutes since a fixed
epoch day: ``date.toordinal() * 1440 + HH:MM``.
"""
from datetime import date, timedelta
from typing import Iterable, List, Sequence, Tuple

from fastapi import status

from ..utils.errors import raise_api_error

Interval = Tuple[int, int]  # [start_abs_min, end_abs_min)

MINUTES_PER_DAY = 1440
REST_WARN_MINUTES = 12 * 60  # rest under 12h between days triggers a warning


def to_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def shift_duration_minutes(start_time: str, end_time: str) -> int:
    """Duration of a shift; an end at or before the start crosses midnight."""
    start, end = to_minutes(start_time), to_minutes(end_time)
    return end - start if end > start else MINUTES_PER_DAY - start + end


def planned_minutes(start_time: str, end_time: str, break_minutes: int) -> int:
    """Chargeable minutes of a shift: duration minus the break (D6)."""
    return shift_duration_minutes(start_time, end_time) - break_minutes


def absolute_interval(day: str, start_time: str, end_time: str) -> Interval:
    day_ordinal = date.fromisoformat(day).toordinal()
    start = day_ordinal * MINUTES_PER_DAY + to_minutes(start_time)
    return (start, start + shift_duration_minutes(start_time, end_time))


def validate_shift_timing(start_time: str, end_time: str, break_minutes: int) -> None:
    """
    Business rules shared by shift types and custom cell hours (D6).

    Raises HTTPException (shift_plan.invalid_time_range / shift_plan.invalid_break).
    """
    if start_time == end_time:
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="shift_plan.invalid_time_range",
            message="start_time y end_time no pueden ser iguales",
        )
    duration = shift_duration_minutes(start_time, end_time)
    if break_minutes >= duration:
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="shift_plan.invalid_break",
            message="El descanso debe ser menor que la duración del turno",
        )


def find_overlap(intervals: Sequence[Interval]) -> bool:
    """True if any two intervals intersect (touching at an endpoint does not)."""
    ordered = sorted(intervals)
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt[0] < prev[1]:
            return True
    return False


def cell_intervals(shifts: Iterable[dict], day: str) -> List[Interval]:
    """Absolute intervals of a list of shift dicts (draft or published copies)."""
    return [
        absolute_interval(day, s["start_time"], s["end_time"]) for s in shifts
    ]


def week_dates(week_start: str) -> List[str]:
    """The 7 ISO dates of the week starting on ``week_start`` (Monday)."""
    monday = date.fromisoformat(week_start)
    return [(monday + timedelta(days=i)).isoformat() for i in range(7)]


def rest_warnings(days: Sequence[Tuple[str, List[Interval]]]) -> List[str]:
    """
    Dates where the rest since the previous planned day is under 12h (D7).

    ``days`` is a chronologically sorted list of (date, intervals) with no two
    entries for the same date. The warning lands on the day that *opens* the
    short rest. Same-day splits never warn (they live inside one entry).
    """
    warned: List[str] = []
    for (day_a, shifts_a), (day_b, shifts_b) in zip(days, days[1:]):
        if not shifts_a or not shifts_b:
            continue
        last_end = max(end for _, end in shifts_a)
        first_start = min(start for start, _ in shifts_b)
        if first_start - last_end < REST_WARN_MINUTES:
            warned.append(day_b)
    return warned


def cell_signature(shifts: Sequence[dict]) -> List[tuple]:
    """
    Multiset fingerprint of a cell's shifts, for pending-change detection (D8).

    Two cells are identical when their sorted value tuples match.
    """
    return sorted(
        (
            s["name"],
            s["short_code"],
            s["color"],
            s["start_time"],
            s["end_time"],
            int(s.get("break_minutes", 0)),
        )
        for s in shifts
    )
