"""Daily summary, due-date reminders, and quiet-hours release (Section 10)."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.jobs import queue
from app.llm import gate
from app.services import health, outbound, settings_store, summaries, tasks, users
from app.services.common import SYSTEM, utcnow
from app.services.formatting import fmt_dt, is_overdue, local, task_line

log = logging.getLogger(__name__)
FALLBACK_DELAY = timedelta(minutes=10)
CATCH_UP_WINDOW = timedelta(hours=4)  # after downtime, still send if we're back within this window
REMINDERS_NOT_BEFORE = time(9, 0)


class PrioritiesNote(BaseModel):
    note: str = Field(max_length=600)


def summary_time(session: Session) -> time:
    h, m = str(settings_store.get(session, "daily_summary_time")).split(":")
    return time(int(h), int(m))


def _today(now: datetime) -> str:
    return local(now).date().isoformat()


def summary_due(session: Session, now: datetime | None = None) -> bool:
    """True from the configured time until CATCH_UP_WINDOW later (same day)."""
    now_local = local(now or utcnow())
    start = datetime.combine(now_local.date(), summary_time(session), tzinfo=now_local.tzinfo)
    return start <= now_local < start + CATCH_UP_WINDOW


def maybe_start_daily_summary(session: Session, now: datetime | None = None) -> bool:
    """Called every minute. Starts today's summary once, at or after the configured time."""
    now = now or utcnow()
    if settings_store.is_paused(session) or not summary_due(session, now):
        return False
    day = _today(now)
    if not health.claim_notification(session, f"daily_summary_started:{day}"):
        return False
    want_note = settings_store.get(session, "daily_summary_llm_note", True)
    if want_note and not gate.is_llm_paused(session, now):
        queue.enqueue(session, "daily_summary_note", {"day": day}, uses_llm=True, max_attempts=1,
                      idempotency_key=f"daily_note:{day}", priority=30)
        run_after = now + FALLBACK_DELAY  # send without the note if the LLM is slow
    else:
        run_after = now
    queue.enqueue(session, "daily_summary_send", {"day": day}, run_after=run_after,
                  idempotency_key=f"daily_send_fallback:{day}", priority=30)
    log.info("daily summary started", extra={"day": day, "with_note": bool(want_note)})
    return True


def already_sent(session: Session, day: str) -> bool:
    from app.db.models import Notification

    return session.get(Notification, f"daily_summary_sent:{day}") is not None


def send_daily_summary(session: Session, day: str, note: str | None = None) -> bool:
    if not health.claim_notification(session, f"daily_summary_sent:{day}"):
        return False
    owner = users.get_owner(session)
    if owner is None:
        return False
    text = summaries.daily_summary(session, priorities_note=note)
    outbound.queue_text(session, owner, text)
    from app.services import audit

    audit.record(session, SYSTEM, "daily_summary_sent", target_type="user", target_id=owner.id,
                 details={"day": day, "with_note": bool(note)})
    return True


def send_due_reminders(session: Session, now: datetime | None = None) -> int:
    """Remind about tasks due today and overdue tasks, at most once per task per day."""
    now = now or utcnow()
    if settings_store.is_paused(session):
        return 0
    day = _today(now)
    if local(now).time() < REMINDERS_NOT_BEFORE and not already_sent(session, day):
        return 0  # let the morning summary go first
    _, end_of_today = summaries.day_bounds()
    candidates = [t for t in tasks.list_open(session, limit=500) if t.due_at and t.due_at < end_of_today]
    overdue, due_today = [], []
    for t in candidates:
        if not health.claim_notification(session, f"reminder:{t.id}:{day}"):
            continue
        (overdue if is_overdue(t, now) else due_today).append(t)
        tasks.record_event(session, t, "reminder_sent", SYSTEM, {"day": day})
    if not overdue and not due_today:
        return 0
    lines = ["⏰ Task reminder"]
    if overdue:
        lines.append("\n*Overdue*")
        lines += [f"• {task_line(t, show_status=False)}" for t in overdue[:15]]
    if due_today:
        lines.append("\n*Due today*")
        lines += [f"• {t.ref} {t.title} — {fmt_dt(t.due_at)}" for t in due_today[:15]]
    lines.append("\nReply DONE T-n when finished.")
    owner = users.get_owner(session)
    if owner:
        outbound.queue_text(session, owner, "\n".join(lines))
    return len(overdue) + len(due_today)


def release_quiet_hour_holds(session: Session, now: datetime | None = None) -> int:
    if outbound.in_quiet_hours(session, now):
        return 0
    owner = users.get_owner(session)
    return outbound.release_held(session, owner, ("quiet_hours",)) if owner else 0
