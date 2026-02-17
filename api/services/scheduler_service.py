"""
Scheduler service for automated backups using APScheduler.
"""
import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..database import db

logger = logging.getLogger(__name__)


class SchedulerService:
    """Manages scheduled backup jobs."""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._backup_job_id = "scheduled_backup"
        self._started = False

    async def start(self):
        """Start scheduler and load existing schedule from settings."""
        if self._started:
            return

        self.scheduler.start()
        self._started = True
        await self.reload_schedule()
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


# Singleton
scheduler_service = SchedulerService()
