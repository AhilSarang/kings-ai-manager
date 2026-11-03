"""Worker process: pulls jobs from Postgres and runs the scheduler.

Lanes (threads):
- general: every job with uses_llm = false (commands, sends, downloads, housekeeping)
- llm:     jobs with uses_llm = true, `llm_concurrency` threads (default 1),
           so LLM calls are serialized and never block outbound messages.

Run: python -m app.jobs.worker
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
import uuid

from app.config import get_settings
from app.db.models import Job
from app.db.session import get_sessionmaker, session_scope
from app.jobs import queue
from app.jobs.errors import LLMUnavailable, PermanentError
from app.jobs.handlers import CRITICAL_TYPES, FAILURE_HOOKS, load_handlers
from app.logging_setup import configure_logging
from app.services import health, settings_store

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, worker_id: str | None = None, poll_interval: float = 1.0):
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.poll_interval = poll_interval
        self.handlers = load_handlers()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- single step (used by tests and the loops) ----
    def run_one(self, *, uses_llm: bool | None = None, only_types: list[str] | None = None) -> bool:
        """Claim and execute one job. Returns False if nothing was ready."""
        Session = get_sessionmaker()
        with Session() as s:
            types = only_types
            if settings_store.is_paused(s):
                allowed = CRITICAL_TYPES & set(only_types) if only_types else CRITICAL_TYPES
                if not allowed:
                    s.rollback()
                    return False
                types = sorted(allowed)
            if uses_llm is not False and _llm_gate_closed(s):
                if uses_llm:
                    s.rollback()
                    return False
                uses_llm = False  # "any job" callers skip LLM jobs while the gate is closed
            job = queue.claim(s, self.worker_id, uses_llm=uses_llm, types=types)
            if job is None:
                return False
            job_id = job.id
        self._execute(job_id)
        return True

    def _execute(self, job_id: uuid.UUID) -> None:
        Session = get_sessionmaker()
        with Session() as s:
            job = s.get(Job, job_id)
            fn = self.handlers.get(job.type)
            ctx = {"job_id": str(job.id), "job_type": job.type, "attempt": job.attempts}
            if fn is None:
                queue.fail(s, job, f"No handler registered for job type '{job.type}'", retryable=False)
                s.commit()
                log.error("unknown job type", extra=ctx)
                return
            started = time.monotonic()
            try:
                fn(s, dict(job.payload or {}), job)
                queue.complete(s, job)
                s.commit()
                log.info("job done", extra={**ctx, "ms": int((time.monotonic() - started) * 1000)})
                return
            except LLMUnavailable as exc:
                s.rollback()
                job = s.get(Job, job_id)
                queue.park_waiting_llm(s, job, exc.retry_at, str(exc))
                s.commit()
                log.warning("job parked: LLM unavailable", extra={**ctx, "retry_at": exc.retry_at})
                return
            except PermanentError as exc:
                s.rollback()
                error, retryable = f"{type(exc).__name__}: {exc}", False
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                error, retryable = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}", True
            job = s.get(Job, job_id)
            queue.fail(s, job, error, retryable=retryable)
            s.commit()
            final = job.status == "failed"
            log.error("job failed", extra={**ctx, "final": final, "error": error.splitlines()[0]})
            if final and job.type in FAILURE_HOOKS:
                try:
                    FAILURE_HOOKS[job.type](job, error)
                except Exception:  # noqa: BLE001
                    log.exception("failure hook crashed", extra=ctx)

    # ---- loops ----
    def _loop(self, uses_llm: bool) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_one(uses_llm=uses_llm)
            except Exception:  # noqa: BLE001  (DB down etc.)
                log.exception("worker loop error")
                worked = False
            if not worked:
                self._stop.wait(self.poll_interval)

    def start(self, llm_lanes: int = 1, general_lanes: int = 1) -> None:
        for i in range(general_lanes):
            self._spawn(f"general-{i}", False)
        for i in range(llm_lanes):
            self._spawn(f"llm-{i}", True)

    def _spawn(self, name: str, uses_llm: bool) -> None:
        t = threading.Thread(target=self._loop, args=(uses_llm,), name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self, timeout: float = 50) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)


def _llm_gate_closed(session) -> bool:
    from app.llm.gate import is_llm_paused

    return is_llm_paused(session)


def main() -> None:
    from app.jobs.scheduler import build_scheduler, prune_stale

    settings = get_settings()
    configure_logging(settings.log_level)
    with session_scope() as s:
        concurrency = int(settings_store.get(s, "llm_concurrency") or settings.llm_concurrency)
        queue.release_stuck(s)  # recover anything a previous crash left behind
        health.beat(s, details={"event": "start"})

    worker = Worker()
    worker.start(llm_lanes=concurrency)
    scheduler = build_scheduler()
    scheduler.start()
    prune_stale(scheduler)
    log.info("worker started", extra={"worker_id": worker.worker_id, "llm_backend": settings.llm_backend,
                                      "llm_lanes": concurrency})

    stop = threading.Event()

    def _handle(signum, _frame):
        log.info("worker stopping", extra={"signal": signum})
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    stop.wait()
    scheduler.shutdown(wait=False)
    worker.stop()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
