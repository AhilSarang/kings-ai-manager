"""Deterministic email views for WhatsApp commands and the daily summary."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import EmailAccount, EmailAttachment, EmailDraft, EmailMessage, EmailThread, Task
from app.email import drafting, followup
from app.email.triage import IMPORTANCE_ORDER
from app.services.common import utcnow
from app.services.formatting import fmt_dt, truncate

NEEDS_REPLY_BUSINESS_DAYS = 2
IMP_MARK = {"critical": "‼️ ", "high": "❗ ", "normal": "", "low": ""}


def _who(thread: EmailThread) -> str:
    return truncate(", ".join((thread.participants or [])[:2]) or "unknown", 45)


def thread_line(t: EmailThread, extra: str = "") -> str:
    flags = " ⚠️" if t.risk_flags else ""
    return f"{t.ref} {IMP_MARK.get(t.importance or '', '')}{truncate(t.subject or '(no subject)', 60)} — {_who(t)}" \
           f"{flags}{extra}"


def needs_reply(session: Session, *, only_overdue: bool = False, limit: int = 20) -> list[EmailThread]:
    rows = list(session.execute(
        select(EmailThread).where(EmailThread.needs_reply.is_(True), EmailThread.status == "open",
                                  EmailThread.last_message_from_owner.is_(False))
    ).scalars())
    now = utcnow()
    if only_overdue:
        rows = [t for t in rows if t.last_message_at and
                followup.business_days_between(t.last_message_at, now) >= NEEDS_REPLY_BUSINESS_DAYS]
    rows.sort(key=lambda t: (-IMPORTANCE_ORDER.get(t.importance or "normal", 1), t.last_message_at or now))
    return rows[:limit]


def waiting(session: Session, *, overdue_only: bool = False, limit: int = 20) -> list[EmailThread]:
    # participants excludes our own address, so an empty list means a note the owner sent to himself
    q = select(EmailThread).where(EmailThread.status == "waiting_on_them", EmailThread.follow_up_tracking.is_(True),
                                  func.jsonb_array_length(EmailThread.participants) > 0)
    if overdue_only:
        q = q.where(EmailThread.follow_up_due_at.is_not(None), EmailThread.follow_up_due_at < utcnow())
    return list(session.execute(q.order_by(EmailThread.last_message_at).limit(limit)).scalars())


def emails_command(session: Session) -> str:
    replies = needs_reply(session, limit=12)
    recent = list(session.execute(
        select(EmailThread).where(EmailThread.alert_sent_at >= utcnow() - timedelta(hours=48),
                                  EmailThread.status != "ignored")
        .order_by(EmailThread.alert_sent_at.desc()).limit(8)
    ).scalars())
    recent = [t for t in recent if t not in replies]
    if not replies and not recent:
        return "📧 No emails need your attention right now."
    lines = []
    if replies:
        lines.append(f"📧 *Needs your reply* ({len(replies)})")
        lines += [f"• {thread_line(t, f' · {fmt_dt(t.last_message_at)}')}" for t in replies]
    if recent:
        lines.append("\n*Recent alerts*")
        lines += [f"• {thread_line(t)}" for t in recent]
    lines.append("\nReply with a ref (e.g. E-12) for details.")
    return "\n".join(lines)


def rules_command(session: Session) -> str:
    from app.email import rules as rules_svc

    mine = rules_svc.owner_rules(session)
    lines = ["*Your email rules*"]
    lines += [f"{i}) {rules_svc.describe(r)}" for i, r in enumerate(mine, start=1)] or ["(none yet)"]
    lines += ["", "*Always on:* instant alerts for Amazon account health, legal notices, big customer order "
              "problems and supplier problems.", "",
              'Remove one with "REMOVE RULE 2". Add one in plain words, e.g. "ignore emails from alibaba.com".']
    return "\n".join(lines)


def waiting_command(session: Session) -> str:
    rows = waiting(session)
    if not rows:
        return "📧 Nothing is waiting on other people."
    now = utcnow()
    lines = ["📧 *Waiting on replies*"]
    for t in rows:
        days = followup.business_days_between(t.last_message_at or now, now)
        overdue = " — OVERDUE" if t.follow_up_due_at and t.follow_up_due_at < now else ""
        lines.append(f"• {thread_line(t, f' · {days} business day{"s" if days != 1 else ""}{overdue}')}")
    return "\n".join(lines)


def drafts_command(session: Session) -> str:
    rows = list(session.execute(
        select(EmailDraft).where(EmailDraft.status.in_(("draft", "pending_approval", "send_failed")))
        .order_by(EmailDraft.created_at.desc()).limit(15)
    ).scalars())
    if not rows:
        return "No open drafts."
    status = {"draft": "draft", "pending_approval": "waiting for APPROVE", "send_failed": "send failed"}
    return "✍️ *Open drafts*\n" + "\n".join(
        f"• {d.ref} → {', '.join(d.to)} — {truncate(d.subject, 50)} ({status[d.status]}, {drafting.where(d)})"
        for d in rows) + "\nReply with a draft ref (e.g. D-3) to see it."


def thread_detail(session: Session, thread: EmailThread) -> str:
    lines = [f"*{thread.ref}* {thread.subject or '(no subject)'}",
             f"With: {', '.join(thread.participants or []) or 'unknown'}",
             f"Category: {thread.category or 'not triaged'} · Importance: {thread.importance or '-'} · "
             f"Status: {thread.status.replace('_', ' ')}"]
    if thread.risk_flags:
        labels = {"possible_phishing": "possible phishing (failed sender checks)",
                  "payment_fraud_risk": "asks to change bank/payment details — verify by phone"}
        lines.append("⚠️ " + "; ".join(labels.get(f, f) for f in thread.risk_flags))
    if thread.summary:
        lines.append(f"Summary: {truncate(thread.summary, 400)}")
    if thread.suggested_action:
        lines.append(f"Suggested: {truncate(thread.suggested_action, 200)}")
    if thread.follow_up_due_at and thread.status == "waiting_on_them":
        lines.append(f"Follow-up due: {fmt_dt(thread.follow_up_due_at)}")
    msgs = list(session.execute(
        select(EmailMessage).where(EmailMessage.thread_id == thread.id)
        .order_by(EmailMessage.sent_at.desc().nulls_last()).limit(3)
    ).scalars())
    if msgs:
        lines.append("\nLatest messages:")
        for m in msgs:
            who = "You" if m.is_from_owner else (m.from_name or m.from_address)
            from app.email.parse import strip_quoted

            lines.append(f"— {who}, {fmt_dt(m.sent_at)}:\n{truncate(strip_quoted(m.body_text) or m.snippet, 300)}")
    atts = list(session.execute(
        select(EmailAttachment).join(EmailMessage, EmailMessage.id == EmailAttachment.message_id)
        .where(EmailMessage.thread_id == thread.id)
    ).scalars())
    if atts:
        lines.append("\n📎 " + ", ".join(
            a.filename + (f" (not saved: {a.skipped_reason})" if a.skipped_reason else "") for a in atts[:8]))
    if thread.linked_task_id:
        task = session.get(Task, thread.linked_task_id)
        if task:
            lines.append(f"Task: {task.ref} ({task.status.replace('_', ' ')})")
    drafts = list(session.execute(
        select(EmailDraft).where(EmailDraft.thread_id == thread.id, EmailDraft.status != "discarded")
        .order_by(EmailDraft.created_at.desc()).limit(3)).scalars())
    if drafts:
        lines.append("Drafts: " + ", ".join(f"{d.ref} ({d.status.replace('_', ' ')})" for d in drafts))
    lines.append(f"\nReply \"draft {thread.ref} …\", \"HANDLED {thread.ref}\" or \"IGNORE {thread.ref}\".")
    return "\n".join(lines)


def draft_detail(session: Session, draft: EmailDraft) -> str:
    return drafting.draft_message(session, draft) if draft.status in ("draft", "send_failed") else \
        f"{draft.ref} is {draft.status.replace('_', ' ')}.\n\n" + drafting.draft_message(session, draft)


def search(session: Session, *, sender=None, subject=None, since_days=None, category=None,
           needs_reply_only=False, limit: int = 10) -> str:
    q = select(EmailThread).join(EmailMessage, EmailMessage.thread_id == EmailThread.id)
    if sender:
        like = f"%{sender.strip()}%"
        q = q.where(or_(EmailMessage.from_address.ilike(like), EmailMessage.from_name.ilike(like)))
    if subject:
        q = q.where(EmailThread.subject.ilike(f"%{subject.strip()}%"))
    if since_days:
        q = q.where(EmailThread.last_message_at >= utcnow() - timedelta(days=int(since_days)))
    if category:
        q = q.where(EmailThread.category == category)
    if needs_reply_only:
        q = q.where(EmailThread.needs_reply.is_(True))
    rows, seen = [], set()
    for t in session.execute(q.order_by(EmailThread.last_message_at.desc().nulls_last()).limit(100)).scalars():
        if t.id not in seen:
            seen.add(t.id)
            rows.append(t)
    if not rows:
        return "No matching emails found."
    head = f"📧 {len(rows)} matching thread{'s' if len(rows) != 1 else ''}" + (" (showing 10)" if len(rows) > limit else "")
    return head + "\n" + "\n".join(f"• {thread_line(t, f' · {fmt_dt(t.last_message_at)}')}"
                                   for t in rows[:limit])


def period_summary(session: Session, period: str) -> str:
    hours = {"today": None, "24h": 24, "week": 24 * 7}.get(period, 24)
    if hours is None:
        from app.services.summaries import day_bounds

        since, _ = day_bounds()
    else:
        since = utcnow() - timedelta(hours=hours)
    rows = list(session.execute(
        select(EmailThread).where(EmailThread.last_message_at >= since, EmailThread.last_message_from_owner.is_(False),
                                  EmailThread.category != "marketing_spam", EmailThread.status != "ignored")
    ).scalars())
    rows.sort(key=lambda t: -IMPORTANCE_ORDER.get(t.importance or "normal", 1))
    important = [t for t in rows if t.importance in ("critical", "high")]
    label = {"today": "today", "24h": "in the last 24 hours", "week": "this week"}.get(period, "recently")
    if not rows:
        return f"📧 No new business email {label}."
    lines = [f"📧 {len(rows)} business email thread{'s' if len(rows) != 1 else ''} {label}"
             f"{f', {len(important)} important' if important else ''}:"]
    for t in rows[:10]:
        lines.append(f"• {thread_line(t)}" + (f"\n  {truncate(t.summary, 160)}" if t.summary else ""))
    return "\n".join(lines)


def daily_sections(session: Session) -> list[str]:
    account = session.execute(select(EmailAccount)).scalars().first()
    if account is None:
        return []
    now = utcnow()
    out: list[str] = []
    if account.status != "active":
        state = "needs re-connecting" if account.status == "auth_error" else "paused"
        out.append(f"\n*Email* ⚠️ Gmail {state} — mail isn't being checked. Nothing is lost; "
                   "it catches up once fixed (see RUNBOOK).")

    def block(title, rows, fmt, limit=8):
        if not rows:
            return
        out.append(f"\n*{title}* ({len(rows)})")
        out.extend(f"• {fmt(r)}" for r in rows[:limit])
        if len(rows) > limit:
            out.append(f"  …and {len(rows) - limit} more")

    alerted = list(session.execute(
        select(EmailThread.ref).where(EmailThread.alert_sent_at >= now - timedelta(hours=24))
        .order_by(EmailThread.alert_sent_at)).scalars())
    if alerted:
        out.append(f"\n*Email alerts (24h)*: {len(alerted)} — {', '.join(alerted[:12])}")
    block("Emails needing your reply", needs_reply(session, only_overdue=True),
          lambda t: thread_line(t, f" · since {fmt_dt(t.last_message_at, with_time=False)}"))
    block("Waiting on replies (follow up)", waiting(session, overdue_only=True),
          lambda t: thread_line(t, f" · sent {fmt_dt(t.last_message_at, with_time=False)}"))
    suspicious = [t for t in session.execute(
        select(EmailThread).where(EmailThread.last_message_at >= now - timedelta(days=7),
                                  EmailThread.status != "ignored")).scalars() if t.risk_flags]
    block("Suspicious / high-risk emails", suspicious,
          lambda t: thread_line(t, " · " + ", ".join(f.replace("_", " ") for f in t.risk_flags)))
    failed = list(session.execute(
        select(EmailThread).join(EmailMessage, EmailMessage.thread_id == EmailThread.id)
        .where(EmailMessage.triage_status == "failed", EmailMessage.created_at >= now - timedelta(days=7))
        .distinct()).scalars())
    block("Emails I couldn't sort — please check", failed, thread_line)
    return out
