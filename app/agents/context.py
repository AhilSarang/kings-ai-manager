"""Builds the context block given to the Supervisor LLM."""

from __future__ import annotations

import html

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import File, Message, User
from app.services import agents as agents_svc
from app.services import memory, summaries, tasks
from app.services.common import utcnow
from app.services.formatting import fmt_dt, local, task_line, truncate

RECENT_MESSAGES = 15
MAX_OPEN_TASKS = 40


def _esc(text: str) -> str:
    from app.services.commands import mask_codes

    return html.escape(mask_codes(text or ""), quote=False)


def recent_conversation(session: Session, user: User, before: Message) -> list[Message]:
    rows = session.execute(
        select(Message)
        .where(Message.user_id == user.id, Message.id != before.id, Message.created_at <= before.created_at,
               Message.status.not_in(("failed", "ignored")))
        .order_by(Message.created_at.desc())
        .limit(RECENT_MESSAGES)
    ).scalars().all()
    return list(reversed(rows))


def build_supervisor_prompt(session: Session, user: User, message: Message) -> str:
    now = local(utcnow())
    parts: list[str] = [f"Current time: {now.strftime('%A %Y-%m-%d %H:%M')} Eastern Time"]

    agents = agents_svc.list_all(session)
    parts.append("<agents>\n" + "\n".join(
        f"- {a.key}: {a.status}" + ("" if a.status == "active" else f" ({agents_svc.inactive_reason(a)})")
        for a in agents) + "\n</agents>")

    open_tasks = tasks.list_open(session, limit=MAX_OPEN_TASKS)
    parts.append("<open_tasks>\n" + ("\n".join(
        f"- {_esc(task_line(t, show_agent=True))}" for t in open_tasks) or "(none)") + "\n</open_tasks>")

    approvals = summaries.pending_approvals(session)
    parts.append("<pending_approvals>\n" + ("\n".join(
        f"- {a.ref}: {_esc(truncate(a.summary, 120))} (risk {a.risk_level}, expires {fmt_dt(a.expires_at)})"
        for a in approvals) or "(none)") + "\n</pending_approvals>")

    mem = memory.active_for(session, "supervisor")
    parts.append("<company_memory>\n" + ("\n".join(
        f"- [{m.category}] {_esc(m.title)}: {_esc(truncate(m.content, 400))}" for m in mem) or "(none)")
        + "\n</company_memory>")

    parts.append(_email_block(session))

    convo = recent_conversation(session, user, message)
    if convo:
        lines = []
        for m in convo:
            who = "the owner" if m.direction == "inbound" else "You"
            lines.append(f"[{fmt_dt(m.created_at)}] {who}: {_esc(truncate(m.body, 400))}")
        parts.append("<recent_conversation>\n" + "\n".join(lines) + "\n</recent_conversation>")

    reply_to = (message.raw_payload or {}).get("context", {}) or {}
    if reply_to.get("id"):
        quoted = session.execute(
            select(Message).where(Message.provider_message_id == reply_to["id"])
        ).scalar_one_or_none()
        if quoted:
            parts.append(f"<quoted_message>{_esc(truncate(quoted.body, 800))}</quoted_message>")

    attachment = _attachment_note(session, message)
    if attachment:
        parts.append(f"<attachment>{_esc(attachment)}</attachment>")

    parts.append(f"<owner_message>{_esc(message.body) or '(no text)'}</owner_message>")
    return "\n\n".join(parts)


def _attachment_note(session: Session, message: Message) -> str | None:
    payload = message.raw_payload or {}
    if payload.get("media_error"):
        return f"the owner sent a file that could not be accepted: {payload['media_error']}"
    if message.media_file_id:
        f = session.get(File, message.media_file_id)
        name = f.original_name or "unnamed"
        return (f"the owner attached a file ({f.mime_type}, {f.size_bytes // 1024} KB, name: {name}). "
                "You cannot see its contents. Use attach_current_file to link it to a task.")
    if payload.get("_type") not in (None, "text"):
        return f"the owner sent a {payload.get('_type')} message that has no readable text."
    return None


def build_task_prompt(session: Session, task) -> str:
    mem = memory.active_for(session, "supervisor")
    parts = [
        f"Current time: {local(utcnow()).strftime('%A %Y-%m-%d %H:%M')} Eastern Time",
        "<company_memory>\n" + ("\n".join(
            f"- [{m.category}] {_esc(m.title)}: {_esc(truncate(m.content, 400))}" for m in mem) or "(none)")
        + "\n</company_memory>",
        f"<task ref=\"{task.ref}\" priority=\"{task.priority}\">\nTitle: {_esc(task.title)}\n"
        f"Details: {_esc(task.description) or '(none)'}\n</task>",
    ]
    notes = [e for e in tasks.recent_events(session, task, limit=10) if e.event_type == "note"]
    if notes:
        parts.append("<task_notes>\n" + "\n".join(f"- {_esc(n.data.get('note', ''))}" for n in notes)
                     + "\n</task_notes>")
    return "\n\n".join(parts)


def _email_block(session: Session) -> str:
    """Metadata only (no bodies) so the owner can refer to emails by sender or subject."""
    from app.db.models import EmailDraft, EmailThread

    threads = session.execute(
        select(EmailThread).where(EmailThread.status != "ignored")
        .order_by(EmailThread.last_message_at.desc().nulls_last()).limit(12)
    ).scalars().all()
    drafts = session.execute(
        select(EmailDraft).where(EmailDraft.status.in_(("draft", "pending_approval", "send_failed")))
        .order_by(EmailDraft.created_at.desc()).limit(8)
    ).scalars().all()
    lines = [f"- {t.ref} [{t.status}{', needs reply' if t.needs_reply else ''}] {_esc(truncate(t.subject, 80))}"
             f" — with {_esc(', '.join((t.participants or [])[:2]))} ({t.category or 'untriaged'})" for t in threads]
    lines += [f"- draft {d.ref} ({d.thread.ref if d.thread else 'new email'}) [{d.status}] "
              f"to {_esc(', '.join(d.to))}" for d in drafts]
    return "<recent_email>\n" + ("\n".join(lines) or "(none)") + "\n</recent_email>"
