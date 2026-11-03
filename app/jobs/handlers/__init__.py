"""Job handler registry.

A handler is `fn(session, payload, job) -> None`. Register with
`@handler("type")`. Handler modules are imported by `load_handlers()`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Job

Handler = Callable[[Session, dict[str, Any], Job], None]
HANDLERS: dict[str, Handler] = {}
# Called once when a job fails for the last time: fn(job, error).
FAILURE_HOOKS: dict[str, Callable[[Job, str], None]] = {}

# Job types that keep running during STOP ALL.
CRITICAL_TYPES: set[str] = set()

_MODULES: list[str] = [
    "app.jobs.handlers.maintenance",
    "app.jobs.handlers.messaging",
    "app.jobs.handlers.inbound",
    "app.jobs.handlers.approvals",
    "app.jobs.handlers.daily",
    "app.jobs.handlers.email",
    "app.jobs.handlers.amazon",
    "app.jobs.handlers.ppc",
]


def handler(
    job_type: str, *, critical: bool = False, on_final_failure: Callable[[Job, str], None] | None = None
) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        if job_type in HANDLERS and HANDLERS[job_type] is not fn:
            raise RuntimeError(f"Duplicate handler for job type {job_type}")
        HANDLERS[job_type] = fn
        if critical:
            CRITICAL_TYPES.add(job_type)
        if on_final_failure:
            FAILURE_HOOKS[job_type] = on_final_failure
        return fn

    return register


def load_handlers() -> dict[str, Handler]:
    for mod in _MODULES:
        importlib.import_module(mod)
    return HANDLERS
