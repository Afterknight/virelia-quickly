"""APScheduler singleton with PostgreSQL job store.

The scheduler is created once during application lifespan (main.py) and stored
here as a module-level variable so that routers can access it without threading
the request object everywhere.

Job store:
  - PostgreSQL (sync SQLAlchemy via psycopg2) when a real Postgres URL is configured.
  - Memory (no persistence) when running against SQLite (tests).

Email sending is handled by a single periodic ``run_slot_scan_job`` worker
(registered in main.py) that scans for due QueueSlot rows rather than by
individual per-slot DateTrigger jobs.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger("quickly.scheduler")

# Module-level scheduler instance.  Set by main.py during lifespan startup.
_scheduler: AsyncIOScheduler | None = None


def set_scheduler(scheduler: AsyncIOScheduler) -> None:
    """Register the running scheduler instance (called once from main.py)."""
    global _scheduler
    _scheduler = scheduler


def get_scheduler() -> AsyncIOScheduler | None:
    """Return the running scheduler, or None if not yet initialised."""
    return _scheduler


def build_jobstores(db_url: str) -> dict:
    """Use APScheduler's in-memory job store for the web service.

    Quickly already rebuilds its recurring maintenance jobs during application
    startup. Keeping APScheduler in memory avoids a synchronous PostgreSQL
    connection during startup, which can block Uvicorn from binding Voroa's
    HTTP port when the remote database is slow or temporarily unavailable.
    The application database remains PostgreSQL-backed through SQLAlchemy.
    """
    log.info("build_jobstores: using MemoryJobStore for web service startup")
    return {}
