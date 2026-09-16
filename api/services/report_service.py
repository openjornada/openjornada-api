"""
ReportService - Service for generating work hour reports for labour inspection compliance.

Generates monthly summaries per worker and per company, plus overtime reports.
All timestamps stored in MongoDB are UTC. Timezone conversion is done only for
grouping records by calendar day (local time) and for display purposes.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Iterable, Mapping, Optional

import pytz
from bson import ObjectId
from fastapi import HTTPException, status

from ..database import db
from ..models.reports import (
    AbsenceSummaryEntry,
    CompanyMonthlySummary,
    DailyWorkSummary,
    ModificationEntry,
    OvertimeReport,
    WorkerMonthlySummary,
    WorkerOvertimeSummary,
)

logger = logging.getLogger(__name__)


def ensure_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Return a UTC-aware datetime. Naive datetimes are assumed to be UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=dt_timezone.utc)
    return dt


def _to_iso(dt) -> str:
    """Convert a datetime (or None) to an ISO 8601 UTC string. Returns '' for None."""
    if dt is None:
        return ""
    dt = ensure_utc_aware(dt)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Calendar period helpers (day / ISO week / calendar month)
# ---------------------------------------------------------------------------


def iso_week_range(d: date) -> tuple[date, date]:
    """
    Return the Monday and Sunday of the ISO 8601 week containing ``d``.

    ISO 8601 weeks start on Monday and end on Sunday. ``date.weekday()``
    already follows the ISO convention (Monday == 0), so no locale logic is
    needed.
    """
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def month_range(d: date) -> tuple[date, date]:
    """Return the first and last calendar day of the month containing ``d``."""
    first = d.replace(day=1)
    if first.month == 12:
        next_first = date(first.year + 1, 1, 1)
    else:
        next_first = date(first.year, first.month + 1, 1)
    return first, next_first - timedelta(days=1)


def _sum_daily_range(daily_minutes: Mapping[date, float], start: date, end: date) -> float:
    """Sum the minutes of every day in the inclusive [start, end] range."""
    total = 0.0
    current = start
    while current <= end:
        total += float(daily_minutes.get(current, 0.0))
        current += timedelta(days=1)
    return total


def derive_period_totals(
    daily_minutes: Mapping[date, float], day: date
) -> tuple[float, float, float]:
    """
    Derive (daily, weekly, monthly) worked minutes for ``day``.

    ``daily_minutes`` maps a local calendar date to the minutes worked that
    day (already net of pauses). Weekly totals cover the full ISO week
    (Monday-Sunday) and monthly totals the full calendar month, regardless of
    the interval actually used to build the map.
    """
    week_start, week_end = iso_week_range(day)
    month_start, month_end = month_range(day)
    return (
        float(daily_minutes.get(day, 0.0)),
        _sum_daily_range(daily_minutes, week_start, week_end),
        _sum_daily_range(daily_minutes, month_start, month_end),
    )


def totals_query_range(days: Iterable[date]) -> tuple[date, date]:
    """
    Return the minimum local date range needed to compute period totals.

    Given the local dates of the rows being returned, the totals for each row
    require both its ISO week and its calendar month in full. The minimum
    range is therefore the union of the earliest week/month bounds and the
    latest week/month bounds.

    Raises:
        ValueError: If ``days`` is empty.
    """
    days = list(days)
    if not days:
        raise ValueError("totals_query_range requires at least one date")

    first, last = min(days), max(days)
    first_month_start, _ = month_range(first)
    first_week_start, _ = iso_week_range(first)
    _, last_month_end = month_range(last)
    _, last_week_end = iso_week_range(last)

    return min(first_month_start, first_week_start), max(last_month_end, last_week_end)


class ReportService:
    """Service for generating work hour reports for labour inspection compliance."""

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    async def get_worker_monthly_summary(
        self,
        company_id: str,
        worker_id: str,
        year: int,
        month: int,
        timezone: str = "Europe/Madrid",
    ) -> WorkerMonthlySummary:
        """
        Build a full monthly summary for a single worker.

        Args:
            company_id: MongoDB _id (string) of the company.
            worker_id: MongoDB _id (string) of the worker.
            year: Calendar year (e.g. 2026).
            month: Calendar month 1-12.
            timezone: IANA timezone name for grouping by local calendar day.

        Returns:
            WorkerMonthlySummary with daily_details populated.

        Raises:
            HTTPException 404: If company or worker is not found.
        """
        company = await self._get_company_or_404(company_id)
        worker = await self._get_worker_or_404(worker_id)

        tz = pytz.timezone(timezone)
        start_utc, end_utc = self._month_utc_range(year, month, tz)

        records = await db.TimeRecords.find(
            {
                "worker_id": worker_id,
                "company_id": company_id,
                "timestamp": {"$gte": start_utc, "$lt": end_utc},
            }
        ).sort("timestamp", 1).to_list(10_000)

        worker_info = {
            "worker_id": worker_id,
            "worker_name": f"{worker.get('first_name', '')} {worker.get('last_name', '')}".strip(),
            "worker_id_number": worker.get("id_number", ""),
        }
        company_info = {
            "company_id": company_id,
            "company_name": company.get("name", ""),
        }

        grouped = self._group_records_by_day(records, tz)

        daily_details: list[DailyWorkSummary] = []
        for day_date in sorted(grouped):
            day_summary = self._process_day_records(
                grouped[day_date], day_date, worker_info, company_info
            )
            daily_details.append(day_summary)

        # Absence & vacation management (Fase 1): only touch the report when the
        # company has opted in (D9). Approved absences mark the affected days
        # (creating a zero-hours day when there were no time records at all) so
        # they don't count as missing work, and are listed in `absences`.
        absences_summary: list[AbsenceSummaryEntry] = []
        if company.get("absence_management_enabled", False):
            absences_summary = await self._merge_absences_into_days(
                company_id, worker_id, year, month, daily_details, worker_info, company_info,
            )

        # Days that actually have at least one record are counted as worked.
        # We exclude days where the only situation is an open session with no
        # minutes logged yet (has_open_session=True, total_worked_minutes=0).
        days_worked = sum(
            1
            for d in daily_details
            if d.total_worked_minutes > 0 or (d.has_open_session and d.first_entry is not None)
        )
        total_worked = sum(d.total_worked_minutes for d in daily_details)
        total_pause = sum(d.total_pause_minutes for d in daily_details)

        daily_expected_minutes = 480.0  # 8 h
        overtime = max(0.0, total_worked - days_worked * daily_expected_minutes)

        signature_doc = await db.MonthlySignatures.find_one(
            {
                "worker_id": worker_id,
                "company_id": company_id,
                "year": year,
                "month": month,
            }
        )
        if signature_doc:
            signature_status = "signed"
            signed_at = ensure_utc_aware(signature_doc.get("signed_at"))
        else:
            signature_status = "pending"
            signed_at = None

        return WorkerMonthlySummary(
            worker_id=worker_info["worker_id"],
            worker_name=worker_info["worker_name"],
            worker_id_number=worker_info["worker_id_number"],
            company_id=company_info["company_id"],
            company_name=company_info["company_name"],
            year=year,
            month=month,
            total_days_worked=days_worked,
            total_worked_minutes=total_worked,
            total_pause_minutes=total_pause,
            total_overtime_minutes=overtime,
            daily_details=daily_details,
            absences=absences_summary,
            signature_status=signature_status,
            signed_at=signed_at,
            generated_at=datetime.now(dt_timezone.utc),
        )

    async def get_company_monthly_summary(
        self,
        company_id: str,
        year: int,
        month: int,
        timezone: str = "Europe/Madrid",
    ) -> CompanyMonthlySummary:
        """
        Build a monthly summary for all active workers in a company.

        Workers with zero days worked in the requested month are excluded from
        the result to keep the report concise.

        Args:
            company_id: MongoDB _id (string) of the company.
            year: Calendar year.
            month: Calendar month 1-12.
            timezone: IANA timezone name.

        Returns:
            CompanyMonthlySummary with workers list populated (active, with records).

        Raises:
            HTTPException 404: If company is not found.
        """
        company = await self._get_company_or_404(company_id)

        active_workers = await db.Workers.find(
            {"company_ids": company_id, "deleted_at": None}
        ).to_list(10_000)

        worker_summaries: list[WorkerMonthlySummary] = []
        for w in active_workers:
            wid = str(w["_id"])
            try:
                summary = await self.get_worker_monthly_summary(
                    company_id=company_id,
                    worker_id=wid,
                    year=year,
                    month=month,
                    timezone=timezone,
                )
            except HTTPException:
                logger.warning("Skipping worker %s due to lookup error.", wid)
                continue

            if summary.total_days_worked == 0:
                continue

            worker_summaries.append(summary)

        return CompanyMonthlySummary(
            company_id=company_id,
            company_name=company.get("name", ""),
            year=year,
            month=month,
            total_workers=len(worker_summaries),
            workers=worker_summaries,
            generated_at=datetime.now(dt_timezone.utc),
        )

    async def get_overtime_report(
        self,
        company_id: str,
        year: int,
        month: int,
        daily_expected_minutes: float = 480.0,
        timezone: str = "Europe/Madrid",
    ) -> OvertimeReport:
        """
        Build an overtime report for all workers in a company.

        Only workers whose total worked minutes exceed their expected minutes
        (days_worked * daily_expected_minutes) are included.

        Args:
            company_id: MongoDB _id (string) of the company.
            year: Calendar year.
            month: Calendar month 1-12.
            daily_expected_minutes: Expected minutes per working day (default 480 = 8 h).
            timezone: IANA timezone name.

        Returns:
            OvertimeReport with workers_with_overtime list.

        Raises:
            HTTPException 404: If company is not found.
        """
        company_summary = await self.get_company_monthly_summary(
            company_id=company_id, year=year, month=month, timezone=timezone
        )

        overtime_workers: list[WorkerOvertimeSummary] = []

        for worker in company_summary.workers:
            expected = worker.total_days_worked * daily_expected_minutes
            overtime = worker.total_worked_minutes - expected

            if overtime <= 0:
                continue

            days_with_overtime = sum(
                1
                for d in worker.daily_details
                if d.total_worked_minutes > daily_expected_minutes
            )

            overtime_workers.append(
                WorkerOvertimeSummary(
                    worker_id=worker.worker_id,
                    worker_name=worker.worker_name,
                    worker_id_number=worker.worker_id_number,
                    total_worked_minutes=worker.total_worked_minutes,
                    expected_minutes=expected,
                    overtime_minutes=overtime,
                    days_with_overtime=days_with_overtime,
                )
            )

        return OvertimeReport(
            company_id=company_summary.company_id,
            company_name=company_summary.company_name,
            year=year,
            month=month,
            workers_with_overtime=overtime_workers,
            generated_at=datetime.now(dt_timezone.utc),
        )

    # ---------------------------------------------------------------------------
    # Private helpers
    # ---------------------------------------------------------------------------

    def _process_day_records(
        self,
        records: list[dict],
        target_date: date,
        worker_info: dict,
        company_info: dict,
    ) -> DailyWorkSummary:
        """
        Derive a DailyWorkSummary from the records belonging to a single day.

        Records must already be sorted by timestamp ascending (guaranteed by the
        MongoDB query). The method uses the pre-computed ``duration_minutes``
        stored on each ``exit`` record, which already accounts for
        outside-shift pauses deducted at clock-out time.

        Args:
            records: List of raw MongoDB documents for one day, sorted by timestamp.
            target_date: The calendar date (local time) these records belong to.
            worker_info: Dict with worker_id, worker_name, worker_id_number.
            company_info: Dict with company_id, company_name.

        Returns:
            DailyWorkSummary for this day.
        """
        first_entry: Optional[datetime] = None
        last_exit: Optional[datetime] = None
        total_worked_minutes: float = 0.0
        total_pause_minutes: float = 0.0
        total_break_minutes: float = 0.0
        # Distinct non-null center snapshots in chronological order: a worker
        # can change centers mid-day, so the day is attributed to all of them.
        work_center_names: list[str] = []

        for record in records:
            rtype = record.get("type")
            ts = ensure_utc_aware(record.get("timestamp"))

            record_center = record.get("work_center_name")
            if record_center and record_center not in work_center_names:
                work_center_names.append(record_center)

            if rtype == "entry" and first_entry is None:
                first_entry = ts

            if rtype == "exit":
                last_exit = ts
                # duration_minutes on exit already has outside-shift pauses deducted.
                worked = record.get("duration_minutes")
                if worked is not None:
                    total_worked_minutes += float(worked)

            if rtype == "pause_end":
                duration = record.get("duration_minutes")
                if duration is not None:
                    counts_as_work = record.get("pause_counts_as_work", False)
                    if counts_as_work:
                        total_break_minutes += float(duration)
                    else:
                        total_pause_minutes += float(duration)

        last_record_type = records[-1].get("type") if records else None
        has_open_session = last_record_type not in ("exit", None)

        is_modified = any(record.get("modified_by_admin_id") for record in records)

        modifications = []
        for rec in records:
            if rec.get("modified_by_admin_id"):
                modifications.append(ModificationEntry(
                    record_id=str(rec.get("_id", "")),
                    record_type=rec.get("type", ""),
                    original_timestamp=_to_iso(rec.get("original_timestamp")),
                    new_timestamp=_to_iso(rec.get("timestamp")),
                    modified_at=_to_iso(rec.get("modified_at")),
                    modified_by_admin_email=rec.get("modified_by_admin_email", ""),
                    modification_reason=rec.get("modification_reason", ""),
                ))

        return DailyWorkSummary(
            date=target_date,
            worker_id=worker_info["worker_id"],
            worker_name=worker_info["worker_name"],
            worker_id_number=worker_info["worker_id_number"],
            company_id=company_info["company_id"],
            company_name=company_info["company_name"],
            work_center_name=" / ".join(work_center_names) if work_center_names else None,
            first_entry=first_entry,
            last_exit=last_exit,
            total_worked_minutes=total_worked_minutes,
            total_pause_minutes=total_pause_minutes,
            total_break_minutes=total_break_minutes,
            records_count=len(records),
            has_open_session=has_open_session,
            is_modified=is_modified,
            modifications=modifications,
        )

    async def _merge_absences_into_days(
        self,
        company_id: str,
        worker_id: str,
        year: int,
        month: int,
        daily_details: list[DailyWorkSummary],
        worker_info: dict,
        company_info: dict,
    ) -> list[AbsenceSummaryEntry]:
        """
        Mark days covered by an ACCEPTED absence as ``is_absence`` and append
        a zero-hours day for any absence day that has no time records at all
        (so it shows up in the report instead of being silently omitted).

        Mutates ``daily_details`` in place (adds missing days, re-sorts).

        Args:
            company_id: MongoDB _id (string) of the company.
            worker_id: MongoDB _id (string) of the worker.
            year: Calendar year of the report.
            month: Calendar month of the report (1-12).
            daily_details: Days already built from time records; mutated in place.
            worker_info: Dict with worker_id, worker_name, worker_id_number.
            company_info: Dict with company_id, company_name.

        Returns:
            List of AbsenceSummaryEntry for the period (relación de ausencias).
        """
        month_start = date(year, month, 1)
        month_end = (
            date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        ) - timedelta(days=1)

        query = {
            "worker_id": worker_id,
            "company_id": company_id,
            "status": "accepted",
            "start_date": {"$lte": datetime.combine(month_end, datetime.min.time())},
            "end_date": {"$gte": datetime.combine(month_start, datetime.min.time())},
        }
        absences = await db.Absences.find(query).sort("start_date", 1).to_list(1_000)

        day_index: dict[date, DailyWorkSummary] = {d.date: d for d in daily_details}
        summary_entries: list[AbsenceSummaryEntry] = []

        for absence in absences:
            a_start = absence.get("start_date")
            a_end = absence.get("end_date")
            if isinstance(a_start, datetime):
                a_start = a_start.date()
            if isinstance(a_end, datetime):
                a_end = a_end.date()
            if a_start is None or a_end is None:
                continue

            absence_type_name = absence.get("absence_type_name") or absence.get("absence_type_code", "")

            range_start = max(a_start, month_start)
            range_end = min(a_end, month_end)

            current = range_start
            while current <= range_end:
                existing = day_index.get(current)
                if existing is not None:
                    existing.is_absence = True
                    existing.absence_type = absence_type_name
                else:
                    new_day = DailyWorkSummary(
                        date=current,
                        worker_id=worker_info["worker_id"],
                        worker_name=worker_info["worker_name"],
                        worker_id_number=worker_info["worker_id_number"],
                        company_id=company_info["company_id"],
                        company_name=company_info["company_name"],
                        is_absence=True,
                        absence_type=absence_type_name,
                    )
                    daily_details.append(new_day)
                    day_index[current] = new_day
                current += timedelta(days=1)

            summary_entries.append(AbsenceSummaryEntry(
                absence_type=absence_type_name,
                start_date=a_start,
                end_date=a_end,
                days_computed=float(absence.get("days_computed", 0.0)),
            ))

        daily_details.sort(key=lambda d: d.date)
        return summary_entries

    def _group_records_by_day(
        self, records: list[dict], tz: pytz.BaseTzInfo
    ) -> dict[date, list[dict]]:
        """
        Group a list of MongoDB records by their local calendar day.

        All timestamps in the database are UTC. This method converts each
        timestamp to local time before extracting the date, so that a worker
        clocking in at 23:50 UTC in CET (UTC+1) is correctly placed on the
        next calendar day.

        Args:
            records: List of raw MongoDB documents sorted by timestamp ascending.
            tz: pytz timezone object for the desired local timezone.

        Returns:
            Dict mapping each local date to its list of records, preserving
            the original sort order within each day.
        """
        grouped: dict[date, list[dict]] = defaultdict(list)

        for record in records:
            ts = ensure_utc_aware(record.get("timestamp"))
            if ts is None:
                logger.warning("Record missing timestamp, skipping: %s", record.get("_id"))
                continue
            local_date = ts.astimezone(tz).date()
            grouped[local_date].append(record)

        return dict(grouped)

    # ---------------------------------------------------------------------------
    # Database lookups
    # ---------------------------------------------------------------------------

    @staticmethod
    async def _get_company_or_404(company_id: str) -> dict:
        """Fetch a company document or raise HTTPException 404."""
        try:
            oid = ObjectId(company_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Company not found: {company_id}",
            )
        company = await db.Companies.find_one({"_id": oid, "deleted_at": None})
        if company is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Company not found: {company_id}",
            )
        return company

    @staticmethod
    async def _get_worker_or_404(worker_id: str) -> dict:
        """Fetch a worker document or raise HTTPException 404."""
        try:
            oid = ObjectId(worker_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worker not found: {worker_id}",
            )
        worker = await db.Workers.find_one({"_id": oid, "deleted_at": None})
        if worker is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worker not found: {worker_id}",
            )
        return worker

    # ---------------------------------------------------------------------------
    # Worked-minutes aggregation (time-records totals)
    # ---------------------------------------------------------------------------

    async def get_exit_minutes_by_day_multi(
        self,
        worker_ids: Iterable[str],
        company_id: Optional[str],
        start_date: date,
        end_date: date,
        tz: pytz.BaseTzInfo,
    ) -> dict[str, dict[date, float]]:
        """
        Return per-worker ``{worker_id: {local_date: minutes}}`` maps in one query.

        One single aggregation covers every requested worker, replacing the
        previous one-query-per-worker pattern (N+1). Only closed shifts
        contribute: the pipeline filters ``type == "exit"`` and sums the
        persisted ``duration_minutes`` (already net of pauses), so an ``entry``
        without a matching ``exit`` contributes nothing.

        The local calendar day is computed inside the pipeline with
        ``$dateToString`` using the requested timezone, so it matches the day
        used on the Python side to derive week/month totals. The range is
        interpreted in local time (``tz``) and converted to UTC for the
        ``$match``, mirroring ``_month_utc_range``. When ``company_id`` is None
        the total spans all companies for each worker, matching an unfiltered
        listing.

        Args:
            worker_ids: MongoDB _id (string) of every worker in the result set.
            company_id: Optional MongoDB _id (string) of the company.
            start_date: First local day of the global range (inclusive).
            end_date: Last local day of the global range (inclusive).
            tz: pytz timezone used to group records by local calendar day.

        Returns:
            Dict mapping each worker_id to its ``{local_date: minutes}`` map.
            Workers without closed shifts in the range are absent.
        """
        worker_ids = list(worker_ids)
        if not worker_ids:
            return {}

        start_utc, end_utc = self._local_days_utc_range(start_date, end_date, tz)

        match: dict = {
            "worker_id": {"$in": worker_ids},
            "type": "exit",
            "timestamp": {"$gte": start_utc, "$lt": end_utc},
        }
        if company_id:
            match["company_id"] = company_id

        # Mongo accepts IANA names; pytz exposes the original name as ``.zone``
        # (e.g. "Europe/Madrid"), and ``str(pytz.UTC)`` is "UTC".
        timezone_name = getattr(tz, "zone", None) or str(tz)

        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": {
                        "worker_id": "$worker_id",
                        "local_day": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": "$timestamp",
                                "timezone": timezone_name,
                            }
                        },
                    },
                    "minutes": {"$sum": "$duration_minutes"},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "worker_id": "$_id.worker_id",
                    "date": "$_id.local_day",
                    "minutes": 1,
                }
            },
        ]

        result: dict[str, dict[date, float]] = defaultdict(dict)
        async for doc in db.TimeRecords.aggregate(pipeline):
            worker_id = doc.get("worker_id")
            day_str = doc.get("date")
            if worker_id is None or day_str is None:
                continue
            result[worker_id][date.fromisoformat(day_str)] = float(doc.get("minutes", 0.0))

        return dict(result)

    # ---------------------------------------------------------------------------
    # Date range utilities
    # ---------------------------------------------------------------------------

    @staticmethod
    def _local_days_utc_range(
        start_date: date, end_date: date, tz: pytz.BaseTzInfo
    ) -> tuple[datetime, datetime]:
        """
        Return (start_utc, end_utc) covering whole local days [start, end].

        ``start_utc`` is midnight local time on ``start_date`` and ``end_utc``
        is midnight local time on the day after ``end_date``, both converted to
        UTC (start inclusive, end exclusive).
        """
        start_local = tz.localize(datetime.combine(start_date, datetime.min.time()))
        end_local = tz.localize(
            datetime.combine(end_date + timedelta(days=1), datetime.min.time())
        )
        return start_local.astimezone(dt_timezone.utc), end_local.astimezone(dt_timezone.utc)

    @staticmethod
    def _month_utc_range(
        year: int, month: int, tz: pytz.BaseTzInfo
    ) -> tuple[datetime, datetime]:
        """
        Return (start_utc, end_utc) covering the full calendar month in local time.

        ``start_utc`` is midnight on the first day of the month in local time,
        converted to UTC.  ``end_utc`` is midnight on the first day of the
        following month in local time, converted to UTC.

        Args:
            year: Calendar year.
            month: Calendar month 1-12.
            tz: pytz timezone for the company/worker.

        Returns:
            Tuple of two UTC-aware datetimes (start inclusive, end exclusive).
        """
        start_local = tz.localize(datetime(year, month, 1, 0, 0, 0))

        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1

        end_local = tz.localize(datetime(next_year, next_month, 1, 0, 0, 0))

        return start_local.astimezone(dt_timezone.utc), end_local.astimezone(dt_timezone.utc)
