"""APScheduler with jobs persisted in Postgres.

Scheduled functions are tiny: they either do cheap housekeeping directly or
enqueue a job (with an idempotency key, so a double fire is harmless).
Later milestones add entries to PERIODIC via `periodic()`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.db.session import get_engine, session_scope
from app.jobs import queue
from app.services import health

log = logging.getLogger(__name__)

JOBSTORE_TABLE = "apscheduler_jobs"


@dataclass
class Periodic:
    id: str
    func: str  # "module:function" textual reference (picklable)
    trigger: str  # "interval" | "cron"
    args: dict[str, Any] = field(default_factory=dict)


PERIODIC: dict[str, Periodic] = {}


def periodic(id: str, trigger: str, **args: Any):
    def register(fn):
        PERIODIC[id] = Periodic(id=id, func=f"{fn.__module__}:{fn.__name__}", trigger=trigger, args=args)
        return fn

    return register


# ---- housekeeping ----
@periodic("heartbeat", "interval", minutes=5)
def heartbeat_tick() -> None:
    with session_scope() as s:
        health.beat(s, details=queue.counts(s))


@periodic("recover_stuck_jobs", "interval", minutes=1)
def recover_stuck_jobs() -> None:
    with session_scope() as s:
        n = queue.release_stuck(s)
    if n:
        log.warning("released stuck jobs", extra={"count": n})


@periodic("wake_llm_jobs", "interval", minutes=1)
def wake_llm_jobs() -> None:
    from app.llm.gate import is_llm_paused

    with session_scope() as s:
        if not is_llm_paused(s):
            n = queue.wake_waiting_llm(s)
            if n:
                log.info("woke parked LLM jobs", extra={"count": n})


def _load_periodic_modules() -> None:
    """Import modules that register periodic entries."""
    import importlib

    for mod in _PERIODIC_MODULES:
        importlib.import_module(mod)


_PERIODIC_MODULES: list[str] = ["app.jobs.periodic"]


def build_scheduler() -> BackgroundScheduler:
    settings = get_settings()
    _load_periodic_modules()
    scheduler = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(engine=get_engine(), tablename=JOBSTORE_TABLE)},
        timezone=settings.tz,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    sync_jobs(scheduler)
    return scheduler


def sync_jobs(scheduler: BackgroundScheduler) -> None:
    """Add or update every PERIODIC entry. Call before start()."""
    for p in PERIODIC.values():
        scheduler.add_job(p.func, p.trigger, id=p.id, replace_existing=True, **p.args)


def prune_stale(scheduler: BackgroundScheduler) -> None:
    """Remove persisted jobs that no longer exist in code. Call after start()."""
    for job in scheduler.get_jobs():
        if job.id not in PERIODIC:
            log.info("removing stale scheduled job", extra={"id": job.id})
            job.remove()
