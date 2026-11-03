"""Email threads: ref lookup and status changes."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EMAIL_THREAD_STATUSES, EmailThread
from app.services import audit
from app.services.common import Actor, NotFound, ServiceError

REF_RE = re.compile(r"^\s*E-?(\d+)\s*$", re.I)


class InvalidThreadStatus(ServiceError):
    pass


def normalize_ref(ref: str) -> str:
    m = REF_RE.match(ref or "")
    if not m:
        raise NotFound(f"'{ref}' is not an email thread reference (expected like E-12)")
    return f"E-{int(m.group(1))}"


def get_by_ref(session: Session, ref: str) -> EmailThread:
    norm = normalize_ref(ref)
    thread = session.execute(select(EmailThread).where(EmailThread.ref == norm)).scalar_one_or_none()
    if thread is None:
        raise NotFound(f"I can't find email thread {norm}")
    return thread


def set_status(session: Session, thread: EmailThread, status: str, actor: Actor, *, reason: str | None = None) -> None:
    """Any status can move to any other; handled/ignored stop follow-up tracking."""
    if status not in EMAIL_THREAD_STATUSES:
        raise InvalidThreadStatus(f"Unknown email thread status '{status}'")
    old = thread.status
    if old == status:
        return
    thread.status = status
    if status in ("handled", "ignored"):
        thread.follow_up_due_at = None
        thread.needs_reply = False
    audit.record(session, actor, "email_thread_status_changed", target_type="email_thread", target_id=thread.ref,
                 details={"from": old, "to": status, "reason": reason})
    session.flush()
