"""Postgres-backed job queue.

Claiming uses `SELECT ... FOR UPDATE SKIP LOCKED`, so any number of worker
threads or processes can pull from the same table without double-processing.
Jobs survive restarts because they only live in the database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Job
from app.services.common import utcnow

BACKOFF_BASE_SECONDS = 15
BACKOFF_CAP_SECONDS = 30 * 60
STUCK_AFTER = timedelta(minutes=10)
MAX_ERROR_LEN = 4000
# A job in one of these is over, so its idempotency key can be freed for the next run.
FINISHED = ("done", "failed", "cancelled")


def enqueue(
    session: Session,
    type: str,
    payload: dict[str, Any] | None = None,
    *,
    run_after: datetime | None = None,
    max_attempts: int = 5,
    priority: int = 100,
    idempotency_key: str | None = None,
    coalesce: bool = False,
    uses_llm: bool = False,
) -> Job | None:
    """Add a job. With an idempotency key, a duplicate is silently ignored (returns None).

    An idempotency key means "once, ever" — which is right for a key naming a period
    (`amazon_sales:2026-09-25`) and wrong for periodic housekeeping. A constant key like
    `amazon_sync_listings` meant the hourly sync could be queued exactly **once in the lifetime of
    the database**: nothing clears the key and nothing prunes finished jobs, so every later enqueue
    hit the unique index and was silently dropped.

    `coalesce=True` is the periodic meaning: at most one *outstanding* job for that key, queueable
    again once it has finished. Finished rows keep their history and give up their key.
    """
    if idempotency_key and coalesce:
        session.execute(update(Job).where(Job.idempotency_key == idempotency_key,
                                          Job.status.in_(FINISHED)).values(idempotency_key=None))
    values = dict(
        id=uuid.uuid4(), type=type, payload=payload or {}, status="pending",
        run_after=run_after or utcnow(), max_attempts=max_attempts, priority=priority,
        idempotency_key=idempotency_key, uses_llm=uses_llm,
    )
    stmt = insert(Job).values(**values)
    if idempotency_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Job.idempotency_key])
    result = session.execute(stmt.returning(Job.id))
    job_id = result.scalar_one_or_none()
    session.flush()
    return session.get(Job, job_id) if job_id else None


def claim(session: Session, worker_id: str, *, uses_llm: bool | None = None,
          types: list[str] | None = None) -> Job | None:
    """Atomically lock the next ready job and mark it running. Commits."""
    q = (
        select(Job)
        .where(Job.status == "pending", Job.run_after <= utcnow())
        .order_by(Job.priority, Job.run_after, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if uses_llm is not None:
        q = q.where(Job.uses_llm.is_(uses_llm))
    if types:
        q = q.where(Job.type.in_(types))
    job = session.execute(q).scalar_one_or_none()
    if job is None:
        session.rollback()
        return None
    job.status = "running"
    job.locked_by = worker_id
    job.locked_at = utcnow()
    job.attempts += 1
    session.commit()
    return job


def complete(session: Session, job: Job) -> None:
    job.status = "done"
    job.locked_by = None
    job.locked_at = None
    job.last_error = None


def backoff_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_CAP_SECONDS))


def fail(session: Session, job: Job, error: str, *, retryable: bool = True) -> None:
    job.last_error = error[:MAX_ERROR_LEN]
    job.locked_by = None
    job.locked_at = None
    if retryable and job.attempts < job.max_attempts:
        job.status = "pending"
        job.run_after = utcnow() + backoff_delay(job.attempts)
    else:
        job.status = "failed"


def park_waiting_llm(session: Session, job: Job, retry_at: datetime, error: str) -> None:
    """LLM unavailable: park without consuming an attempt. Nothing is lost."""
    job.status = "waiting_llm"
    job.attempts = max(job.attempts - 1, 0)
    job.run_after = retry_at
    job.last_error = error[:MAX_ERROR_LEN]
    job.locked_by = None
    job.locked_at = None


def wake_waiting_llm(session: Session, *, now: datetime | None = None, force: bool = False) -> int:
    """Move parked LLM jobs back to pending once their retry time has passed."""
    now = now or utcnow()
    q = update(Job).where(Job.status == "waiting_llm")
    if not force:
        q = q.where(Job.run_after <= now)
    return session.execute(q.values(status="pending", run_after=func.least(Job.run_after, func.now()))).rowcount


def release_stuck(session: Session, *, older_than: timedelta = STUCK_AFTER, now: datetime | None = None) -> int:
    """Jobs locked too long (worker crashed/killed) go back to pending, or failed if out of attempts."""
    cutoff = (now or utcnow()) - older_than
    base = update(Job).where(Job.status == "running", Job.locked_at < cutoff)
    failed = session.execute(
        base.where(Job.attempts >= Job.max_attempts).values(
            status="failed", locked_by=None, locked_at=None,
            last_error="Worker stopped while running this job; no attempts left")
    ).rowcount
    released = session.execute(
        base.where(Job.attempts < Job.max_attempts).values(
            status="pending", locked_by=None, locked_at=None,
            last_error="Released after worker stopped mid-job")
    ).rowcount
    return released + failed


def cancel_pending(session: Session, *, keep_types: set[str]) -> int:
    """Cancel queued work (STOP ALL). Types in keep_types are left alone."""
    return session.execute(
        update(Job)
        .where(Job.status.in_(("pending", "waiting_llm")), Job.type.not_in(keep_types))
        .values(status="cancelled", last_error="Cancelled by STOP ALL")
    ).rowcount


def counts(session: Session) -> dict[str, int]:
    from sqlalchemy import func

    return dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
