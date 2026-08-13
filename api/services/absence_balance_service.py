"""
AbsenceBalanceService - Computes a worker's vacation balance on the fly for
a reference year (see design decision D3: never stored, recomputed from the
policy and the worker's absences on every request).
"""
from datetime import date, datetime, timedelta
from typing import Optional

from bson.objectid import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..models.absences import AbsenceBalance, AbsenceStatus


def _safe_date(year: int, month: int, day: int) -> date:
    """Build a date, falling back to the last valid day of the month (e.g. 29/02 on non-leap years)."""
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1


def resolve_reference_period(
    policy: dict,
    worker: dict,
    as_of: Optional[date] = None,
    year: Optional[int] = None,
) -> tuple[date, date]:
    """
    Resolve the (period_start, period_end) reference year window.

    - ``calendar``: the literal calendar year (``year`` if given, else the
      year of ``as_of``).
    - ``hire_date``: a 12-month cycle anchored on the worker's hire date
      anniversary. Fase 1 has no dedicated ``hire_date`` field on Workers
      (see report to the human); the worker's ``created_at`` is used as the
      best available proxy.

    Args:
        policy: Raw AbsencePolicies document.
        worker: Raw Workers document.
        as_of: Date used to pick which cycle is "current" (default: today).
        year: Explicit calendar year override (``calendar`` mode only).

    Returns:
        (period_start, period_end), both inclusive.
    """
    as_of = as_of or datetime.now().date()
    reference_year = policy.get("reference_year", "calendar")

    if reference_year != "hire_date":
        target_year = year or as_of.year
        return date(target_year, 1, 1), date(target_year, 12, 31)

    hire_dt = worker.get("created_at")
    if isinstance(hire_dt, datetime):
        hire_date = hire_dt.date()
    elif isinstance(hire_dt, date):
        hire_date = hire_dt
    else:
        hire_date = as_of

    anniversary_this_year = _safe_date(as_of.year, hire_date.month, hire_date.day)
    if anniversary_this_year <= as_of:
        period_start = anniversary_this_year
    else:
        period_start = _safe_date(as_of.year - 1, hire_date.month, hire_date.day)

    period_end = _safe_date(period_start.year + 1, hire_date.month, hire_date.day) - timedelta(days=1)
    return period_start, period_end


async def get_absence_balance(
    db: AsyncIOMotorDatabase,
    worker_id: str,
    company_id: str,
    policy: dict,
    worker: dict,
    year: Optional[int] = None,
    as_of: Optional[date] = None,
    exclude_absence_id: Optional[str] = None,
) -> AbsenceBalance:
    """
    Compute a worker's vacation balance for the resolved reference period.

    Only absences whose snapshotted ``deducts_balance`` is True are counted;
    ``ACCEPTED`` absences count as taken, ``PENDING`` ones as pending.

    Args:
        db: Motor database handle.
        worker_id: Worker MongoDB _id (string).
        company_id: Company MongoDB _id (string).
        policy: Raw AbsencePolicies document for this company.
        worker: Raw Workers document.
        year: Explicit calendar year override (``calendar`` reference mode only).
        as_of: Date used to resolve the "current" cycle (default: today).
        exclude_absence_id: Absence _id to leave out of the sums (used when
            re-validating a request against its own pre-existing balance impact).

    Returns:
        AbsenceBalance with total/taken/pending/available days.
    """
    period_start, period_end = resolve_reference_period(policy, worker, as_of=as_of, year=year)

    query: dict = {
        "worker_id": worker_id,
        "company_id": company_id,
        "deducts_balance": True,
        "status": {"$in": [AbsenceStatus.ACCEPTED.value, AbsenceStatus.PENDING.value]},
        "start_date": {"$lte": datetime.combine(period_end, datetime.min.time())},
        "end_date": {"$gte": datetime.combine(period_start, datetime.min.time())},
    }
    if exclude_absence_id:
        try:
            query["_id"] = {"$ne": ObjectId(exclude_absence_id)}
        except Exception:
            pass

    cursor = db.Absences.find(query)

    taken_days = 0.0
    pending_days = 0.0
    async for absence in cursor:
        days = float(absence.get("days_computed", 0.0))
        if absence.get("status") == AbsenceStatus.ACCEPTED.value:
            taken_days += days
        elif absence.get("status") == AbsenceStatus.PENDING.value:
            pending_days += days

    total_days = float(policy.get("annual_vacation_days", 0))
    available_days = total_days - taken_days - pending_days

    return AbsenceBalance(
        year=period_start.year,
        reference_year_mode=policy.get("reference_year", "calendar"),
        period_start=period_start,
        period_end=period_end,
        total_days=total_days,
        taken_days=taken_days,
        pending_days=pending_days,
        available_days=available_days,
    )
