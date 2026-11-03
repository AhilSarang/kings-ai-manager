"""API-side watchdog: alert the owner if the worker stops (at most once per hour)."""

from __future__ import annotations

import asyncio
import logging

from fastapi.concurrency import run_in_threadpool

from app.db.session import session_scope
from app.services import health, outbound, users
from app.services.common import utcnow

log = logging.getLogger(__name__)
CHECK_EVERY_SECONDS = 60


def check_worker_once() -> bool:
    """Returns True if an alert was sent."""
    with session_scope() as s:
        if not health.worker_is_stale(s):
            return False
        if health.last_seen(s) is None:
            return False  # worker never started yet (fresh install)
        owner = users.get_owner(s)
        if owner is None or not health.claim_notification(s, f"worker_down:{utcnow():%Y%m%d%H}"):
            return False
        seen = health.last_seen(s)
        minutes = int((utcnow() - seen).total_seconds() // 60)
        outbound.send_direct(
            s, owner,
            f"🚨 AI Manager alert: the background worker has not checked in for {minutes} minutes. "
            "Messages are being saved but not processed. It should restart automatically; "
            "if this repeats, check the server (docs/RUNBOOK.md).",
        )
        log.error("worker stale; owner alerted", extra={"minutes": minutes})
        return True


async def watch_worker() -> None:
    while True:
        await asyncio.sleep(CHECK_EVERY_SECONDS)
        try:
            await run_in_threadpool(check_worker_once)
        except Exception:  # noqa: BLE001
            log.exception("worker watchdog error")
