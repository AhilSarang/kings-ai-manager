"""Task lifecycle: creation, status transitions, notes, lookups.

All status changes go through `change_status`, which enforces the
transition table and writes a task event plus an audit entry.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db.models import OPEN_TASK_STATUSES, TASK_PRIORITIES, TASK_STATUSES, Task, TaskEvent
from app.services import agents as agents_svc
from app.services import audit
from app.services.common import Actor, NotFound, ServiceError, utcnow


class InvalidTransition(ServiceError):
    pass


TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"queued", "in_progress", "waiting_owner", "blocked", "done", "cancelled", "failed"}),
    "queued": frozenset({"in_progress", "waiting_owner", "blocked", "done", "cancelled", "failed"}),
    "in_progress": frozenset({"queued", "waiting_approval", "waiting_owner", "blocked", "done", "cancelled", "failed"}),
    "waiting_approval": frozenset({"in_progress", "blocked", "done", "cancelled", "failed"}),
    "waiting_owner": frozenset({"queued", "in_progress", "blocked", "done", "cancelled"}),
    "blocked": frozenset({"queued", "in_progress", "waiting_owner", "done", "cancelled"}),
    "failed": frozenset({"queued", "cancelled"}),
    "done": frozenset(),
    "cancelled": frozenset(),
}
REASON_REQUIRED = frozenset({"blocked", "failed", "cancelled"})
TERMINAL = frozenset({"done", "cancelled"})
PRIORITY_ORDER = case({p: i for i, p in enumerate(TASK_PRIORITIES)}, value=Task.priority)
REF_RE = re.compile(r"^\s*T-?(\d+)\s*$", re.IGNORECASE)

assert set(TRANSITIONS) == set(TASK_STATUSES)


def normalize_ref(ref: str) -> str:
    m = REF_RE.match(ref or "")
    if not m:
        raise NotFound(f"'{ref}' is not a task reference (expected like T-12)")
    return f"T-{int(m.group(1))}"


def _event(session: Session, task: Task, event_type: str, actor: Actor, data: dict[str, Any] | None = None) -> None:
    session.add(TaskEvent(task_id=task.id, event_type=event_type, actor_type=actor.type,
                          actor_id=actor.id, data=data or {}))


def create_task(
    session: Session,
    *,
    title: str,
    actor: Actor,
    description: str = "",
    priority: str = "normal",
    agent_key: str = "supervisor",
    due_at: datetime | None = None,
    created_by_user_id: uuid.UUID | None = None,
    source_message_id: uuid.UUID | None = None,
    parent_task_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> Task:
    """Create a task and route it.

    Active agent -> status `queued`. Inactive/paused agent -> `blocked` with reason.
    """
    title = (title or "").strip()
    if not title:
        raise ServiceError("Task title is required")
    if priority not in TASK_PRIORITIES:
        raise ServiceError(f"Invalid priority '{priority}'")
    if agent_key not in agents_svc.AGENT_KEYS:
        agent_key = "supervisor"
    agent = agents_svc.get_by_key(session, agent_key)

    task = Task(
        title=title[:300], description=description or "", priority=priority, status="new",
        assigned_agent_id=agent.id, created_by_user_id=created_by_user_id,
        source_message_id=source_message_id, due_at=due_at, parent_task_id=parent_task_id,
        metadata_=metadata or {},
    )
    session.add(task)
    session.flush()
    session.refresh(task)  # load server-generated ref
    _event(session, task, "created", actor, {"title": task.title, "priority": priority, "agent": agent_key})
    _event(session, task, "assigned", actor, {"agent": agent_key})
    audit.record(session, actor, "task_created", target_type="task", target_id=task.ref,
                 details={"agent": agent_key, "priority": priority}, trigger_message_id=source_message_id)

    if agent.status == "active":
        change_status(session, task, "queued", actor)
    else:
        change_status(session, task, "blocked", actor, reason=agents_svc.inactive_reason(agent))
    return task


def change_status(session: Session, task: Task, new_status: str, actor: Actor, *, reason: str | None = None) -> Task:
    if new_status not in TASK_STATUSES:
        raise InvalidTransition(f"Unknown status '{new_status}'")
    old = task.status
    if new_status == old:
        return task
    if new_status not in TRANSITIONS[old]:
        raise InvalidTransition(f"{task.ref} is {old.replace('_', ' ')}; it can't move to {new_status.replace('_', ' ')}")
    if new_status in REASON_REQUIRED and not (reason and reason.strip()):
        raise InvalidTransition(f"A reason is required to mark {task.ref} {new_status}")

    task.status = new_status
    meta = dict(task.metadata_ or {})
    if new_status in ("blocked", "failed"):
        meta["status_reason"] = reason
    else:
        meta.pop("status_reason", None)
    task.metadata_ = meta
    task.completed_at = utcnow() if new_status in TERMINAL else None
    _event(session, task, "status_changed", actor, {"from": old, "to": new_status, "reason": reason})
    audit.record(session, actor, "task_status_changed", target_type="task", target_id=task.ref,
                 details={"from": old, "to": new_status, "reason": reason})
    session.flush()
    return task


def update_task(
    session: Session, task: Task, actor: Actor, *, priority: str | None = None,
    due_at: datetime | None = None, clear_due: bool = False, title: str | None = None,
    description: str | None = None,
) -> Task:
    changes: dict[str, Any] = {}
    if task.status in TERMINAL:
        raise InvalidTransition(f"{task.ref} is {task.status} and can't be edited")
    if priority is not None and priority != task.priority:
        if priority not in TASK_PRIORITIES:
            raise ServiceError(f"Invalid priority '{priority}'")
        changes["priority"] = [task.priority, priority]
        task.priority = priority
    if clear_due or (due_at is not None and due_at != task.due_at):
        new_due = None if clear_due else due_at
        changes["due_at"] = [_iso(task.due_at), _iso(new_due)]
        task.due_at = new_due
    if title and title.strip() != task.title:
        changes["title"] = [task.title, title.strip()[:300]]
        task.title = title.strip()[:300]
    if description is not None and description != task.description:
        changes["description"] = True
        task.description = description
    if changes:
        _event(session, task, "updated", actor, changes)
        audit.record(session, actor, "task_updated", target_type="task", target_id=task.ref, details=changes)
        session.flush()
    return task


def add_note(session: Session, task: Task, actor: Actor, note: str) -> None:
    _event(session, task, "note", actor, {"note": note})
    audit.record(session, actor, "task_note_added", target_type="task", target_id=task.ref)
    session.flush()


def set_result(session: Session, task: Task, actor: Actor, summary: str) -> None:
    task.result_summary = summary
    _event(session, task, "result_added", actor, {"summary": summary[:500]})
    session.flush()


def record_event(session: Session, task: Task, event_type: str, actor: Actor, data: dict[str, Any] | None = None) -> None:
    _event(session, task, event_type, actor, data)
    session.flush()


def get_by_ref(session: Session, ref: str) -> Task:
    norm = normalize_ref(ref)
    task = session.execute(select(Task).where(Task.ref == norm)).scalar_one_or_none()
    if task is None:
        raise NotFound(f"I can't find {norm}")
    return task


def list_open(session: Session, *, agent_key: str | None = None, limit: int = 50) -> list[Task]:
    q = select(Task).where(Task.status.in_(OPEN_TASK_STATUSES))
    if agent_key:
        q = q.where(Task.assigned_agent_id == agents_svc.get_by_key(session, agent_key).id)
    q = q.order_by(PRIORITY_ORDER, Task.due_at.asc().nulls_last(), Task.created_at).limit(limit)
    return list(session.execute(q).scalars().unique())


def list_tasks(
    session: Session, *, statuses: list[str] | None = None, agent_key: str | None = None,
    priority: str | None = None, due_before: datetime | None = None, due_after: datetime | None = None,
    completed_after: datetime | None = None, limit: int = 50,
) -> list[Task]:
    q = select(Task)
    if statuses:
        q = q.where(Task.status.in_(statuses))
    if agent_key:
        q = q.where(Task.assigned_agent_id == agents_svc.get_by_key(session, agent_key).id)
    if priority:
        q = q.where(Task.priority == priority)
    if due_before:
        q = q.where(Task.due_at < due_before)
    if due_after:
        q = q.where(Task.due_at >= due_after)
    if completed_after:
        q = q.where(Task.completed_at >= completed_after)
    q = q.order_by(PRIORITY_ORDER, Task.due_at.asc().nulls_last(), Task.created_at).limit(limit)
    return list(session.execute(q).scalars().unique())


def recent_events(session: Session, task: Task, limit: int = 8) -> list[TaskEvent]:
    q = (select(TaskEvent).where(TaskEvent.task_id == task.id)
         .order_by(TaskEvent.created_at.desc(), TaskEvent.id).limit(limit))
    return list(reversed(session.execute(q).scalars().all()))


def count_by_status(session: Session) -> dict[str, int]:
    rows = session.execute(select(Task.status, func.count()).group_by(Task.status)).all()
    return {status: n for status, n in rows}


def status_reason(task: Task) -> str | None:
    return (task.metadata_ or {}).get("status_reason")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
