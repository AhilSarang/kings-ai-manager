"""Deterministic status views and the daily summary, built only from the database."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import OPEN_TASK_STATUSES, Approval, Task
from app.services import agents as agents_svc
from app.services import tasks as tasks_svc
from app.services.common import utcnow
from app.services.formatting import STATUS_LABEL, fmt_dt, is_overdue, local, task_line, truncate, tz

MAX_LINES_PER_GROUP = 8
STATUS_ORDER = ["waiting_owner", "waiting_approval", "in_progress", "queued", "new", "blocked"]


def pending_approvals(session: Session) -> list[Approval]:
    return list(session.execute(
        select(Approval).where(Approval.status == "pending", Approval.expires_at > utcnow())
        .order_by(Approval.created_at)
    ).scalars())


def day_bounds(day_offset: int = 0) -> tuple[datetime, datetime]:
    today = local(utcnow()).date() + timedelta(days=day_offset)
    start = datetime.combine(today, time.min, tzinfo=tz())
    return start, start + timedelta(days=1)


def status_overview(session: Session) -> str:
    open_tasks = tasks_svc.list_open(session, limit=500)
    approvals = pending_approvals(session)
    if not open_tasks and not approvals:
        return "✅ No open tasks and no pending approvals."
    lines = [f"📋 {len(open_tasks)} open task{'s' if len(open_tasks) != 1 else ''}"
             f" · {len(approvals)} pending approval{'s' if len(approvals) != 1 else ''}"]
    overdue = [t for t in open_tasks if is_overdue(t)]
    if overdue:
        lines.append(f"⚠️ {len(overdue)} overdue")
    for status in STATUS_ORDER:
        group = [t for t in open_tasks if t.status == status]
        if not group:
            continue
        lines.append(f"\n*{STATUS_LABEL[status].capitalize()}* ({len(group)})")
        lines += [f"• {task_line(t, show_status=status in ('blocked',))}" for t in group[:MAX_LINES_PER_GROUP]]
        if len(group) > MAX_LINES_PER_GROUP:
            lines.append(f"  …and {len(group) - MAX_LINES_PER_GROUP} more")
    if approvals:
        lines.append("\n*Approvals* — reply APPROVALS for details")
    return "\n".join(lines)


def open_tasks_list(session: Session, *, agent_key: str | None = None, limit: int = 25) -> str:
    tasks = tasks_svc.list_open(session, agent_key=agent_key, limit=limit + 1)
    if not tasks:
        return "✅ No open tasks." if not agent_key else f"No open tasks for {agent_key}."
    lines = [f"• {task_line(t, show_agent=True)}" for t in tasks[:limit]]
    if len(tasks) > limit:
        lines.append(f"…more not shown (limit {limit})")
    return "\n".join(lines)


def filtered_list(session: Session, *, statuses=None, agent_key=None, priority=None, due=None, limit=25) -> str:
    kwargs = dict(statuses=statuses or list(OPEN_TASK_STATUSES), agent_key=agent_key, priority=priority,
                  limit=limit + 1)
    now = utcnow()
    if due == "today":
        _, end = day_bounds()
        kwargs.update(due_before=end, due_after=now)
    elif due == "overdue":
        kwargs.update(due_before=now)
    elif due == "this_week":
        kwargs.update(due_before=now + timedelta(days=7))
    tasks = tasks_svc.list_tasks(session, **kwargs)
    if not tasks:
        return "No matching tasks."
    lines = [f"• {task_line(t, show_agent=True)}" for t in tasks[:limit]]
    if len(tasks) > limit:
        lines.append(f"…more not shown (limit {limit})")
    return "\n".join(lines)


def today_view(session: Session) -> str:
    start, end = day_bounds()
    due_today = [t for t in tasks_svc.list_open(session, limit=500) if t.due_at and t.due_at < end]
    done_today = tasks_svc.list_tasks(session, statuses=["done"], completed_after=start, limit=50)
    lines = []
    if due_today:
        lines.append("*Due today / overdue*")
        lines += [f"• {task_line(t)}" for t in due_today[:15]]
    if done_today:
        lines.append("*Completed today*")
        lines += [f"• {t.ref} {t.title}" for t in done_today[:15]]
    return "\n".join(lines) or "Nothing due or completed today."


def task_detail(session: Session, ref: str) -> str:
    t = tasks_svc.get_by_ref(session, ref)
    lines = [f"*{t.ref}* {t.title}",
             f"Status: {STATUS_LABEL.get(t.status, t.status)}"
             + (f" — {tasks_svc.status_reason(t)}" if tasks_svc.status_reason(t) else ""),
             f"Priority: {t.priority} · Agent: {t.agent.key if t.agent else '-'}"]
    if t.due_at:
        lines.append(("Due: " if not is_overdue(t) else "OVERDUE, was due: ") + fmt_dt(t.due_at))
    lines.append(f"Created: {fmt_dt(t.created_at)}")
    if t.completed_at:
        lines.append(f"Closed: {fmt_dt(t.completed_at)}")
    if t.description:
        lines.append(f"\n{truncate(t.description, 500)}")
    if t.result_summary:
        lines.append(f"\nResult: {truncate(t.result_summary, 800)}")
    if t.subtasks:
        lines.append("\nSubtasks:")
        lines += [f"• {task_line(s)}" for s in t.subtasks[:10]]
    from app.db.models import File

    n_files = session.execute(select(func.count()).select_from(File).where(File.related_task_id == t.id)).scalar_one()
    if n_files:
        lines.append(f"\n📎 {n_files} file{'s' if n_files != 1 else ''} attached")
    events = tasks_svc.recent_events(session, t, limit=6)
    if events:
        lines.append("\nRecent:")
        lines += [f"• {fmt_dt(e.created_at)} {_event_text(e)}" for e in events]
    return "\n".join(lines)


def _event_text(e) -> str:
    d = e.data or {}
    if e.event_type == "status_changed":
        return f"{STATUS_LABEL.get(d.get('from'), d.get('from'))} → {STATUS_LABEL.get(d.get('to'), d.get('to'))}" + (
            f" ({truncate(d['reason'], 60)})" if d.get("reason") else "")
    if e.event_type == "note":
        return f"note: {truncate(d.get('note', ''), 80)}"
    if e.event_type == "file_attached":
        return f"file attached{': ' + d['name'] if d.get('name') else ''}"
    if e.event_type == "updated":
        return "updated " + ", ".join(k for k in d)
    return e.event_type.replace("_", " ")


def agent_scope(session: Session, key: str) -> str:
    agent = agents_svc.get_by_key(session, key)
    head = f"{agent.display_name}: {agent.status}"
    if agent.status != "active":
        head += f" ({agents_svc.inactive_reason(agent)})"
    return head + "\n" + open_tasks_list(session, agent_key=key)


def daily_summary(session: Session, *, priorities_note: str | None = None) -> str:
    now = utcnow()
    y_start, y_end = day_bounds(-1)
    completed = tasks_svc.list_tasks(session, statuses=["done"], completed_after=y_start, limit=100)
    completed = [t for t in completed if t.completed_at < y_end]
    open_tasks = tasks_svc.list_open(session, limit=500)
    in_progress = [t for t in open_tasks if t.status in ("in_progress", "queued", "new")]
    blocked = [t for t in open_tasks if t.status == "blocked"]
    waiting = [t for t in open_tasks if t.status == "waiting_owner"]
    overdue = [t for t in open_tasks if is_overdue(t, now)]
    approvals = pending_approvals(session)

    def block(title: str, items: list, fmt, limit: int = 8) -> list[str]:
        if not items:
            return []
        out = [f"\n*{title}* ({len(items)})"] + [f"• {fmt(i)}" for i in items[:limit]]
        if len(items) > limit:
            out.append(f"  …and {len(items) - limit} more")
        return out

    date = local(now).strftime("%A, %B %-d")
    lines = [f"☀️ Daily summary — {date}"]
    if priorities_note:
        lines.append(f"\n{priorities_note}")
    lines += block("Overdue", overdue, task_line)
    lines += block("Waiting on you", waiting, lambda t: task_line(t, show_status=False))
    lines += block("Pending approvals", approvals,
                   lambda a: f"{a.ref} {truncate(a.summary, 80)} ({a.risk_level})")
    lines += block("In progress / queued", in_progress, lambda t: task_line(t, show_agent=True))
    lines += block("Blocked", blocked, task_line, limit=6)
    lines += block("Completed yesterday", completed, lambda t: f"{t.ref} {t.title}")
    from app.amazon.monitor import daily_sections as amazon_sections
    from app.email.views import daily_sections

    lines += daily_sections(session)
    for section in amazon_sections(session):
        lines.append("")
        lines.append(section)
    if len(lines) == 1 + (1 if priorities_note else 0):
        lines.append("\nNo open tasks, approvals, or completions to report.")
    lines.append("\nReply STATUS, TASKS, or a task ref (e.g. T-12) for details.")
    return "\n".join(lines)
