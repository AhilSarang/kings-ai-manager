"""WhatsApp alerts for important email (brief Section 7)."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EmailAlertRule, EmailMessage, EmailThread, Job
from app.email.triage import IMPORTANCE_ORDER
from app.jobs import queue
from app.services import audit, outbound, users
from app.services.common import Actor, utcnow
from app.services.formatting import truncate

log = logging.getLogger(__name__)

BUILTIN_NAMES = {"A": "Amazon account health / policy", "B": "Legal notice", "C": "Customer / B2B order problem",
                 "D": "Supplier problem"}
COMBINE_AT = 3
COMBINE_WINDOW = timedelta(minutes=10)
AGENT = Actor.agent("email")


def rule_info(session: Session, key: str) -> tuple[str, bool]:
    """(display name, bypasses quiet hours) for a rule key."""
    if key == "phishing":
        return "Suspicious email", True
    if key in BUILTIN_NAMES:
        rule = session.execute(select(EmailAlertRule).where(
            EmailAlertRule.rule_type == "builtin_category", EmailAlertRule.value == key)).scalar_one_or_none()
        return BUILTIN_NAMES[key], bool(rule and rule.bypass_quiet_hours)
    prefix = key.removeprefix("custom:")
    rule = None
    try:
        rule = session.get(EmailAlertRule, uuid.UUID(prefix))
    except ValueError:
        rule = session.execute(select(EmailAlertRule).where(
            EmailAlertRule.rule_type == "custom_description")).scalars().all()
        rule = next((r for r in rule if str(r.id).startswith(prefix)), None)
    if rule is None:
        return "Your alert rule", False
    return rule.name, rule.bypass_quiet_hours


def new_rules(thread: EmailThread, rules: list[str], importance: str) -> list[str]:
    """Rules worth alerting now: never alerted for this thread, or importance went up."""
    alerted = set(thread.alerted_rules or [])
    rose = IMPORTANCE_ORDER.get(importance, 0) > IMPORTANCE_ORDER.get(thread.alerted_importance or "low", 0) \
        and bool(alerted)
    fresh = [r for r in rules if r not in alerted]
    if fresh:
        return fresh
    return list(rules) if rose else []


def format_single(session: Session, thread: EmailThread, msg: EmailMessage, rules: list[str], kind: str) -> str:
    sender = f"{msg.from_name} <{msg.from_address}>" if msg.from_name else msg.from_address
    if kind == "phishing":
        return (f"⚠️ SUSPICIOUS EMAIL ({thread.ref}) — this looks like a FAKE Amazon message\n"
                f"From: {sender}\nSubject: {truncate(msg.subject, 150)}\n"
                "It failed Amazon's sender verification and asks you to act. Don't click its links, "
                f"log in, or reply. Reply \"IGNORE {thread.ref}\" to dismiss it.")
    names = " + ".join(rule_info(session, r)[0] for r in rules)
    lines = [f"📧 EMAIL ALERT — {names} ({thread.ref})", f"From: {sender}",
             f"Subject: {truncate(msg.subject, 150)}", f"Summary: {truncate(thread.summary or msg.snippet, 400)}"]
    if thread.suggested_action:
        lines.append(f"Suggested: {truncate(thread.suggested_action, 200)}")
    if "payment_fraud_risk" in (thread.risk_flags or []):
        lines.append("⚠️ Mentions changed bank/payment details — verify by phone before paying anything.")
    lines.append(f'Reply "{thread.ref}" for details or "draft {thread.ref} …" to write a reply.')
    return "\n".join(lines)


def format_combined(session: Session, items: list[tuple[EmailThread, EmailMessage, list[str], str]]) -> str:
    lines = [f"📧 {len(items)} EMAIL ALERTS"]
    for i, (thread, msg, rules, kind) in enumerate(items, start=1):
        label = "Suspicious (possible fake)" if kind == "phishing" else \
            " + ".join(rule_info(session, r)[0] for r in rules)
        who = msg.from_name or msg.from_address
        lines.append(f"{i}) {label} — {thread.ref} — {truncate(who, 40)}: {truncate(thread.summary or msg.subject, 140)}")
    lines.append("Reply with a ref (e.g. " + items[0][0].ref + ") for details.")
    return "\n".join(lines)


def process(session: Session, job: Job) -> str:
    owner = users.get_owner(session)
    if owner is None:
        return "no_owner"
    now = utcnow()
    others = list(session.execute(
        select(Job).where(Job.type == "send_email_alert", Job.status == "pending", Job.id != job.id,
                          Job.run_after <= now + COMBINE_WINDOW)
        .order_by(Job.run_after).with_for_update(skip_locked=True)
    ).scalars())
    items = []
    for j in [job, *others]:
        p = j.payload
        thread = session.get(EmailThread, uuid.UUID(p["thread_id"]))
        msg = session.get(EmailMessage, uuid.UUID(p["message_id"]))
        if thread is None or msg is None or thread.status == "ignored":
            continue
        rules = new_rules(thread, p["rules"], p.get("importance", "normal"))
        if not rules:
            continue
        thread.alerted_rules = sorted(set(thread.alerted_rules or []) | set(rules))
        if IMPORTANCE_ORDER.get(p.get("importance"), 0) > IMPORTANCE_ORDER.get(thread.alerted_importance or "low", 0) \
                or thread.alerted_importance is None:
            thread.alerted_importance = p.get("importance", "normal")
        thread.alert_sent_at = now
        items.append((thread, msg, rules, p.get("kind", "alert")))
    for j in others:
        j.status = "done"
        j.last_error = f"combined into alert job {job.id}"
    if not items:
        return "duplicate"

    def urgent_for(rules: list[str]) -> bool:
        return any(rule_info(session, r)[1] for r in rules)

    if len(items) >= COMBINE_AT:
        text = format_combined(session, items)
        outbound.queue_text(session, owner, text, urgent=any(urgent_for(i[2]) for i in items))
    else:
        for thread, msg, rules, kind in items:
            outbound.queue_text(session, owner, format_single(session, thread, msg, rules, kind),
                                urgent=urgent_for(rules), related_task_id=thread.linked_task_id)
    for thread, msg, rules, kind in items:
        audit.record(session, AGENT, "email_alert_sent", target_type="email_thread", target_id=thread.ref,
                     details={"rules": rules, "kind": kind, "combined": len(items) >= COMBINE_AT})
    return f"sent:{len(items)}"


def defer(session: Session, job: Job, minutes: int = 15) -> None:
    queue.enqueue(session, job.type, dict(job.payload), run_after=utcnow() + timedelta(minutes=minutes),
                  priority=job.priority)
