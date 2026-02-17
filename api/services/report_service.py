"""
ReportService - Service for generating work hour reports for labour inspection compliance.

Generates monthly summaries per worker and per company, plus overtime reports.
All timestamps stored in MongoDB are UTC. Timezone conversion is done only for
grouping records by calendar day (local time) and for display purposes.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timezone as dt_timezone
from typing import Optional

import pytz
from bson import ObjectId
from fastapi import HTTPException, status

from ..database import db
from ..models.reports import (
    CompanyMonthlySummary,
    DailyWorkSummary,
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

        for record in records:
            rtype = record.get("type")
            ts = ensure_utc_aware(record.get("timestamp"))

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

        return DailyWorkSummary(
            date=target_date,
            worker_id=worker_info["worker_id"],
            worker_name=worker_info["worker_name"],
            worker_id_number=worker_info["worker_id_number"],
            company_id=company_info["company_id"],
            company_name=company_info["company_name"],
            first_entry=first_entry,
            last_exit=last_exit,
            total_worked_minutes=total_worked_minutes,
            total_pause_minutes=total_pause_minutes,
            total_break_minutes=total_break_minutes,
            records_count=len(records),
            has_open_session=has_open_session,
            is_modified=is_modified,
        )

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
    # Date range utilities
    # ---------------------------------------------------------------------------

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
