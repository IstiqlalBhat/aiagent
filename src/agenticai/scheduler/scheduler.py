"""Scheduler for automated calls using APScheduler."""

import asyncio
from typing import Any, Callable, Awaitable

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..core.config import load_schedules

logger = structlog.get_logger(__name__)


class CallScheduler:
    """Scheduler for automated phone calls.

    Loads schedules from schedules.yaml and executes calls
    based on cron expressions.
    """

    def __init__(
        self,
        call_handler: Callable[[str, str, dict], Awaitable[str]],
        schedules_path: str | None = None,
    ):
        """Initialize the scheduler.

        Args:
            call_handler: Async function to initiate calls.
                          Signature: (to_number, prompt, metadata) -> call_id
            schedules_path: Path to schedules.yaml file
        """
        self._call_handler = call_handler
        self._schedules_path = schedules_path
        self._scheduler = AsyncIOScheduler()
        self._schedules: list[dict] = []
        self._is_running = False

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._is_running

    def load_schedules(self) -> None:
        """Load schedules from configuration file."""
        config = load_schedules(self._schedules_path)
        self._schedules = config.get("schedules", [])
        logger.info("Loaded schedules", count=len(self._schedules))

    def start(self) -> None:
        """Start the scheduler."""
        if self._is_running:
            return

        logger.info("Starting scheduler")

        # Load schedules if not already loaded
        if not self._schedules:
            self.load_schedules()

        # Add jobs for each enabled schedule
        for schedule in self._schedules:
            if schedule.get("enabled", False):
                self._add_schedule_job(schedule)

        self._scheduler.start()
        self._is_running = True

        logger.info(
            "Scheduler started",
            active_jobs=len(self._scheduler.get_jobs()),
        )

    def stop(self) -> None:
        """Stop the scheduler."""
        if not self._is_running:
            return

        logger.info("Stopping scheduler")
        self._scheduler.shutdown(wait=False)
        self._is_running = False
        logger.info("Scheduler stopped")

    def _add_schedule_job(self, schedule: dict) -> None:
        """Add a job for a schedule.

        Args:
            schedule: Schedule configuration
        """
        name = schedule.get("name", "unnamed")
        cron_expr = schedule.get("cron", "")
        calls = schedule.get("calls", [])

        if not cron_expr or not calls:
            logger.warning("Invalid schedule, skipping", name=name)
            return

        # Parse cron expression
        try:
            cron_parts = cron_expr.split()
            if len(cron_parts) >= 5:
                trigger = CronTrigger(
                    minute=cron_parts[0],
                    hour=cron_parts[1],
                    day=cron_parts[2],
                    month=cron_parts[3],
                    day_of_week=cron_parts[4],
                )
            else:
                logger.error("Invalid cron expression", name=name, cron=cron_expr)
                return
        except Exception as e:
            logger.error("Failed to parse cron", name=name, error=str(e))
            return

        # Add job
        self._scheduler.add_job(
            self._execute_schedule,
            trigger=trigger,
            id=f"schedule_{name}",
            name=name,
            kwargs={"schedule": schedule},
            replace_existing=True,
        )

        logger.info("Added schedule job", name=name, cron=cron_expr)

    async def _execute_schedule(self, schedule: dict) -> None:
        """Execute a scheduled set of calls.

        Args:
            schedule: Schedule configuration
        """
        name = schedule.get("name", "unnamed")
        calls = schedule.get("calls", [])

        logger.info("Executing schedule", name=name, call_count=len(calls))

        for call_config in calls:
            to_number = call_config.get("to_number", "")
            prompt = call_config.get("prompt", "")
            metadata = call_config.get("metadata", {})

            if not to_number:
                logger.warning("Missing to_number in schedule call", name=name)
                continue

            try:
                call_id = await self._call_handler(to_number, prompt, metadata)
                logger.info(
                    "Scheduled call initiated",
                    schedule=name,
                    to_number=to_number,
                    call_id=call_id,
                )
            except Exception as e:
                logger.error(
                    "Failed to initiate scheduled call",
                    schedule=name,
                    to_number=to_number,
                    error=str(e),
                )

    async def run_schedule_now(self, name: str) -> list[str]:
        """Manually run a schedule immediately.

        Args:
            name: Schedule name to run

        Returns:
            List of initiated call IDs
        """
        # Find schedule by name
        schedule = None
        for s in self._schedules:
            if s.get("name") == name:
                schedule = s
                break

        if not schedule:
            raise ValueError(f"Schedule not found: {name}")

        logger.info("Running schedule manually", name=name)

        call_ids = []
        for call_config in schedule.get("calls", []):
            to_number = call_config.get("to_number", "")
            prompt = call_config.get("prompt", "")
            metadata = call_config.get("metadata", {})

            if to_number:
                try:
                    call_id = await self._call_handler(to_number, prompt, metadata)
                    call_ids.append(call_id)
                except Exception as e:
                    logger.error("Failed to run call", error=str(e))

        return call_ids

    def list_schedules(self) -> list[dict]:
        """List all configured schedules.

        Returns:
            List of schedule configurations
        """
        return [
            {
                "name": s.get("name", "unnamed"),
                "cron": s.get("cron", ""),
                "enabled": s.get("enabled", False),
                "call_count": len(s.get("calls", [])),
            }
            for s in self._schedules
        ]

    def get_next_run_times(self) -> dict[str, str]:
        """Get next run times for all scheduled jobs.

        Returns:
            Dict mapping schedule name to next run time
        """
        result = {}
        for job in self._scheduler.get_jobs():
            next_run = job.next_run_time
            result[job.name] = str(next_run) if next_run else "Not scheduled"
        return result
