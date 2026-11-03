"""Plain-text formatting for WhatsApp. All times shown in Eastern Time."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.models import Task
from app.services.common import utcnow
from app.services.tasks import status_reason

PRIORITY_MARK = {"urgent": "‼️ ", "high": "❗ ", "normal": "", "low": ""}
STATUS_LABEL = {
    "new": "new", "queued": "queued", "in_progress": "in progress", "waiting_approval": "waiting approval",
    "waiting_owner": "waiting on you", "blocked": "blocked", "done": "done", "cancelled": "cancelled",
    "failed": "failed",
}


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def local(dt: datetime | None) -> datetime | None:
    return dt.astimezone(tz()) if dt else None


def fmt_dt(dt: datetime | None, with_time: bool = True) -> str:
    if dt is None:
        return ""
    d = local(dt)
    now = local(utcnow())
    day = "today" if d.date() == now.date() else d.strftime("%a %b %-d")
    if with_time and (d.hour, d.minute) != (17, 0):
        return f"{day} {d.strftime('%-I:%M %p')}"
    return day


def is_overdue(task: Task, now: datetime | None = None) -> bool:
    return bool(task.due_at and task.due_at < (now or utcnow()) and task.status not in ("done", "cancelled"))


def task_line(task: Task, *, show_status: bool = True, show_agent: bool = False) -> str:
    parts = [f"{task.ref} {PRIORITY_MARK.get(task.priority, '')}{task.title}"]
    meta = []
    if show_status:
        label = STATUS_LABEL.get(task.status, task.status)
        reason = status_reason(task)
        meta.append(f"{label} ({reason})" if reason and task.status in ("blocked", "failed") else label)
    if show_agent and task.agent:
        meta.append(task.agent.key)
    if task.due_at:
        meta.append(("OVERDUE, was due " if is_overdue(task) else "due ") + fmt_dt(task.due_at))
    if meta:
        parts.append(" · ".join(meta))
    return " — ".join(parts)


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
