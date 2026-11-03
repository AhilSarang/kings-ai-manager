"""Housekeeping jobs."""

from __future__ import annotations

import logging

from app.jobs.handlers import handler

log = logging.getLogger(__name__)


@handler("noop")
def noop(session, payload, job) -> None:
    """Used by tests and as a smoke check."""
    log.info("noop job ran", extra={"job_id": str(job.id), "payload": payload})
