from __future__ import annotations

from datetime import datetime, timezone

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..db import get_session_factory
from .cleanup import dispatch_cleanups
from .download import dispatch_downloads
from .lineage import dispatch_lineage

log = structlog.get_logger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


async def _run_downloads() -> None:
    factory = get_session_factory()
    async with factory() as session:
        try:
            await dispatch_downloads(session)
        except Exception:  # noqa: BLE001
            log.exception("dispatch_downloads_crashed")
            await session.rollback()


async def _run_lineage() -> None:
    factory = get_session_factory()
    async with factory() as session:
        try:
            await dispatch_lineage(session)
        except Exception:  # noqa: BLE001
            log.exception("dispatch_lineage_crashed")
            await session.rollback()


async def _run_cleanups() -> None:
    factory = get_session_factory()
    async with factory() as session:
        try:
            await dispatch_cleanups(session)
        except Exception:  # noqa: BLE001
            log.exception("dispatch_cleanups_crashed")
            await session.rollback()


def start_scheduler() -> None:
    if scheduler.running:
        return
    now = datetime.now(timezone.utc)
    scheduler.add_job(
        _run_downloads,
        "interval",
        seconds=1,
        id="dispatch_downloads",
        max_instances=1,
        coalesce=True,
        next_run_time=now,
        replace_existing=True,
    )
    scheduler.add_job(
        _run_lineage,
        "interval",
        seconds=1,
        id="dispatch_lineage",
        max_instances=1,
        coalesce=True,
        next_run_time=now,
        replace_existing=True,
    )
    scheduler.add_job(
        _run_cleanups,
        "interval",
        seconds=5,
        id="dispatch_cleanups",
        max_instances=1,
        coalesce=True,
        next_run_time=now,
        replace_existing=True,
    )
    scheduler.start()
    log.info("scheduler_started")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")
