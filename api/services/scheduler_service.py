"""
Scheduler service for automated backups and SMS reminders using APScheduler.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from bson import ObjectId

from ..database import db

logger = logging.getLogger(__name__)


class SchedulerService:
    """Manages scheduled backup and SMS reminder jobs."""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._backup_job_id = "scheduled_backup"
        self._sms_check_job_id = "check_open_shifts"
        self._started = False

    async def start(self):
        """Start scheduler and load existing schedule from settings."""
        if self._started:
            return

        self.scheduler.start()
        self._started = True
        await self.reload_schedule()
        self._start_sms_check_job()
        logger.info("Scheduler service started")

    def stop(self):
        """Stop scheduler."""
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False
            logger.info("Scheduler service stopped")

    async def reload_schedule(self):
        """Reload backup schedule from settings."""
        # Remove existing job if any
        if self.scheduler.get_job(self._backup_job_id):
            self.scheduler.remove_job(self._backup_job_id)
            logger.info("Removed existing backup job")

        # Get settings
        settings = await db.Settings.find_one()
        if not settings:
            logger.info("No settings found, backup scheduling skipped")
            return

        backup_config = settings.get("backup_config", {})
        if not backup_config.get("enabled"):
            logger.info("Scheduled backups disabled")
            return

        schedule = backup_config.get("schedule", {})
        if not schedule:
            logger.info("No schedule configured")
            return

        frequency = schedule.get("frequency", "daily")
        time_str = schedule.get("time", "00:00")

        try:
            hour, minute = map(int, time_str.split(":"))
        except ValueError:
            logger.error(f"Invalid time format: {time_str}")
            return

        # Build cron trigger based on frequency
        if frequency == "daily":
            trigger = CronTrigger(hour=hour, minute=minute)
            schedule_desc = f"diario a las {time_str} UTC"
        elif frequency == "weekly":
            day_of_week = schedule.get("day_of_week", 0)
            trigger = CronTrigger(
                day_of_week=day_of_week,
                hour=hour,
                minute=minute
            )
            days = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
            schedule_desc = f"semanal ({days[day_of_week]}) a las {time_str} UTC"
        elif frequency == "monthly":
            day_of_month = schedule.get("day_of_month", 1)
            trigger = CronTrigger(
                day=day_of_month,
                hour=hour,
                minute=minute
            )
            schedule_desc = f"mensual (día {day_of_month}) a las {time_str} UTC"
        else:
            logger.warning(f"Unknown frequency: {frequency}")
            return

        # Add job
        self.scheduler.add_job(
            self._run_scheduled_backup,
            trigger,
            id=self._backup_job_id,
            name="Scheduled MongoDB Backup",
            replace_existing=True
        )

        logger.info(f"Backup programado: {schedule_desc}")

    async def _run_scheduled_backup(self):
        """Execute scheduled backup."""
        logger.info("Iniciando backup programado...")

        try:
            # Import here to avoid circular imports
            from .backup_service import backup_service

            await backup_service.create_backup(trigger="scheduled")

            # Cleanup old backups after successful backup
            await backup_service.cleanup_old_backups()

            logger.info("Backup programado completado")

        except Exception as e:
            logger.error(f"Backup programado fallido: {e}")

    def get_next_run_time(self) -> datetime | None:
        """Get next scheduled backup time."""
        job = self.scheduler.get_job(self._backup_job_id)
        if job:
            return job.next_run_time
        return None

    def is_backup_scheduled(self) -> bool:
        """Check if backup job is scheduled."""
        return self.scheduler.get_job(self._backup_job_id) is not None

    # -------------------------------------------------------------------------
    # SMS reminder job
    # -------------------------------------------------------------------------

    def _start_sms_check_job(self):
        """Register the open-shift SMS check job (every 5 minutes)."""
        if self.scheduler.get_job(self._sms_check_job_id):
            return  # Already registered

        self.scheduler.add_job(
            self._check_open_shifts,
            IntervalTrigger(minutes=5),
            id=self._sms_check_job_id,
            name="Check open shifts for SMS reminders",
            replace_existing=True
        )
        logger.info("SMS check job registered (every 5 minutes)")

    async def _check_open_shifts(self):
        """
        Periodically checks for open shifts and sends SMS reminders when due.

        Logic per company with SMS enabled:
          1. Find all open time record entries (type="entry" with no matching exit).
          2. For each open shift:
             - Verify worker has sms_enabled=True and has a phone number.
             - Check company active hours (in company timezone).
             - Calculate how many reminders have already been sent.
             - Send if time elapsed >= first_reminder_minutes/60 + (n * reminder_frequency_minutes/60)
               and reminder count < max_reminders_per_day.
        """
        logger.debug("[SMS-CHECK] Running open shifts check")

        try:
            from .sms_service import sms_service

            if not sms_service.is_enabled():
                logger.debug("[SMS-CHECK] SMS service disabled, skipping")
                return

            # Get all companies with SMS enabled
            companies_cursor = db.Companies.find({
                "deleted_at": None,
                "sms_config.enabled": True
            })

            async for company in companies_cursor:
                try:
                    await self._process_company_sms(company, sms_service)
                except Exception as e:
                    logger.error(f"[SMS-CHECK] Error processing company {company.get('_id')}: {e}")

        except Exception as e:
            logger.error(f"[SMS-CHECK] Unexpected error in _check_open_shifts: {e}")

    async def _process_company_sms(self, company: dict, sms_service) -> None:
        """Process SMS reminders for all open shifts in a single company."""
        company_id = str(company["_id"])
        sms_config = company.get("sms_config", {})

        # Fields stored in minutes; convert to hours for elapsed-time comparisons
        first_reminder_minutes: int = sms_config.get("first_reminder_minutes", 240)
        reminder_frequency_minutes: int = sms_config.get("reminder_frequency_minutes", 60)
        first_after_hours: float = first_reminder_minutes / 60.0
        repeat_interval_hours: float = reminder_frequency_minutes / 60.0
        max_reminders: int = sms_config.get("max_reminders_per_day", 5)
        active_start: str = sms_config.get("active_hours_start", "08:00")
        active_end: str = sms_config.get("active_hours_end", "23:00")
        tz_name: str = sms_config.get("timezone", "Europe/Madrid")

        # Check if current time is within active hours
        try:
            company_tz = ZoneInfo(tz_name)
            now_local = datetime.now(company_tz)
            current_minutes = now_local.hour * 60 + now_local.minute

            start_h, start_m = map(int, active_start.split(":"))
            end_h, end_m = map(int, active_end.split(":"))
            active_start_minutes = start_h * 60 + start_m
            active_end_minutes = end_h * 60 + end_m

            if not (active_start_minutes <= current_minutes <= active_end_minutes):
                logger.debug(f"[SMS-CHECK] Company {company_id} outside active hours ({active_start}-{active_end})")
                return
        except Exception as e:
            logger.warning(f"[SMS-CHECK] Error checking active hours for company {company_id}: {e}")
            return

        now_utc = datetime.now(timezone.utc)

        # Find open entry records for this company:
        # An open shift = a "entry" record that has no corresponding "exit" for the same worker
        # We look for entry records, then check if there's a later exit
        open_entries_cursor = db.TimeRecords.find({
            "company_id": company_id,
            "type": "entry"
        }).sort("timestamp", -1)

        async for entry_record in open_entries_cursor:
            try:
                worker_id = entry_record.get("worker_id")
                entry_id = str(entry_record["_id"])
                entry_timestamp = entry_record.get("timestamp")

                if not worker_id or not entry_timestamp:
                    continue

                # Make timestamp UTC-aware if naive
                if entry_timestamp.tzinfo is None:
                    entry_timestamp = entry_timestamp.replace(tzinfo=timezone.utc)

                # Check if there's a subsequent exit for this worker at this company
                exit_record = await db.TimeRecords.find_one({
                    "worker_id": worker_id,
                    "company_id": company_id,
                    "type": "exit",
                    "timestamp": {"$gt": entry_timestamp}
                })

                if exit_record:
                    # Shift is closed, no reminder needed
                    continue

                # Calculate hours since entry
                elapsed_seconds = (now_utc - entry_timestamp).total_seconds()
                hours_elapsed = elapsed_seconds / 3600.0

                if hours_elapsed < first_after_hours:
                    # Not enough time has passed for even the first reminder
                    continue

                # Count reminders already sent for this entry
                reminders_sent = await db.SmsLogs.count_documents({
                    "worker_id": worker_id,
                    "company_id": company_id,
                    "time_record_entry_id": entry_id,
                    "status": {"$in": ["sent", "delivered"]}
                })

                if reminders_sent >= max_reminders:
                    logger.debug(
                        f"[SMS-CHECK] Max reminders ({max_reminders}) reached for "
                        f"entry {entry_id}, worker {worker_id}"
                    )
                    continue

                # Calculate when the next reminder is due
                # Reminder N is due at: first_after_hours + (N-1) * repeat_interval_hours
                # reminders_sent = N already sent, so next is N+1
                next_reminder_number = reminders_sent + 1
                hours_threshold = first_after_hours + (reminders_sent * repeat_interval_hours)

                if hours_elapsed < hours_threshold:
                    continue

                # Get worker details
                try:
                    worker = await db.Workers.find_one({
                        "_id": ObjectId(worker_id),
                        "deleted_at": None
                    })
                except Exception:
                    continue

                if not worker:
                    continue

                # Check worker opt-in (field renamed from opted_in to sms_enabled)
                worker_sms_config = worker.get("sms_config", {})
                if not worker_sms_config.get("sms_enabled", True):
                    logger.debug(f"[SMS-CHECK] Worker {worker_id} has opted out of SMS")
                    continue

                # Use worker's phone number
                phone_number = worker.get("phone_number")
                if not phone_number:
                    logger.debug(f"[SMS-CHECK] Worker {worker_id} has no phone number")
                    continue

                worker_name = f"{worker.get('first_name', '')} {worker.get('last_name', '')}".strip()
                worker_id_number: str | None = worker.get("id_number")
                company_name = company.get("name", "")

                logger.info(
                    f"[SMS-CHECK] Sending reminder #{next_reminder_number} to worker {worker_id} "
                    f"for entry {entry_id} ({hours_elapsed:.1f}h elapsed)"
                )

                await sms_service.send_shift_reminder(
                    worker_id=worker_id,
                    company_id=company_id,
                    time_record_entry_id=entry_id,
                    phone_number=phone_number,
                    worker_name=worker_name,
                    company_name=company_name,
                    hours_open=hours_elapsed,
                    reminder_number=next_reminder_number,
                    worker_id_number=worker_id_number,
                )

            except Exception as e:
                logger.error(
                    f"[SMS-CHECK] Error processing entry {entry_record.get('_id')} "
                    f"in company {company_id}: {e}"
                )


# Singleton
scheduler_service = SchedulerService()
