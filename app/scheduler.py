"""Background scheduler: periodically checks items whose next check is due."""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.config import settings
from app.db import engine
from app.services import run_due_checks

logger = logging.getLogger("pricemonitor.scheduler")

scheduler = BackgroundScheduler(timezone="UTC")


def _sweep() -> None:
    with Session(engine) as session:
        run_due_checks(session)


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(
        _sweep,
        trigger="interval",
        seconds=settings.scheduler_interval_seconds,
        id="due-sweep",
        coalesce=True,  # collapse missed runs into one
        max_instances=1,  # never overlap sweeps
        next_run_time=datetime.utcnow(),  # run once shortly after startup
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "scheduler started; sweeping every %ss", settings.scheduler_interval_seconds
    )


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("scheduler stopped")
