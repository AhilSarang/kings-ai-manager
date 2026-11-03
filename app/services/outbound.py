"""Outbound WhatsApp delivery rules.

Order of checks for each queued update:
1. quiet hours (non-urgent only)      -> held until quiet hours end
2. 24-hour customer-service window    -> held; one template sent to reopen it
3. outbound rate limit                -> held; one alert per hour
Otherwise each chunk is sent and recorded. Held messages are released by
`release_held()` (on the owner's next message, or when quiet hours end).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.channels import get_channel
from app.channels.base import MAX_TEXT_LEN, ChannelError
from app.config import get_settings
from app.db.models import AuditLog, Message, User
from app.jobs import queue
from app.jobs.errors import PermanentError, RetryableError
from app.services import audit, health, settings_store
from app.services.common import SYSTEM, utcnow

log = logging.getLogger(__name__)

CHUNK_LIMIT = 3900  # below WhatsApp's 4096 to leave room for "(1/3)" markers
WINDOW = timedelta(hours=24) - timedelta(minutes=5)
RATE_WINDOW = timedelta(minutes=10)


# ---------- pure helpers ----------
def split_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = max(rest.rfind("\n\n", 0, limit), rest.rfind("\n", 0, limit))
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    total = len(chunks)
    out = [f"{c}\n({i}/{total})" for i, c in enumerate(chunks, 1)]
    assert all(len(c) <= MAX_TEXT_LEN for c in out)
    return out


def _parse_hhmm(v: str) -> time:
    h, m = v.split(":")
    return time(int(h), int(m))


def in_quiet_hours(session: Session, now: datetime | None = None) -> bool:
    cfg = settings_store.get(session, "quiet_hours")
    if not cfg or not cfg.get("start") or not cfg.get("end"):
        return False
    local = (now or utcnow()).astimezone(ZoneInfo(get_settings().timezone)).time()
    start, end = _parse_hhmm(cfg["start"]), _parse_hhmm(cfg["end"])
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


def can_send_freeform(user: User, now: datetime | None = None) -> bool:
    return user.last_inbound_at is not None and (now or utcnow()) - user.last_inbound_at < WINDOW


def recent_send_count(session: Session, now: datetime | None = None) -> int:
    since = (now or utcnow()) - RATE_WINDOW
    return session.execute(
        select(func.count()).select_from(AuditLog)
        .where(AuditLog.action == "message_sent", AuditLog.occurred_at >= since)
    ).scalar_one()


# ---------- queueing ----------
def queue_text(
    session: Session, user: User, text: str, *, urgent: bool = False,
    related_task_id: uuid.UUID | None = None, respect_quiet_hours: bool = True,
) -> list[Message]:
    """Create outbound message rows and a job to send them."""
    base = utcnow()
    rows = [
        # Explicit, strictly increasing timestamps keep chunk order stable everywhere.
        Message(direction="outbound", channel="whatsapp", user_id=user.id, body=chunk,
                status="queued", related_task_id=related_task_id, raw_payload={"urgent": urgent},
                created_at=base + timedelta(microseconds=i))
        for i, chunk in enumerate(split_text(text))
    ]
    session.add_all(rows)
    session.flush()
    if rows:
        queue.enqueue(session, "send_messages", {
            "message_ids": [str(r.id) for r in rows], "urgent": urgent,
            "respect_quiet_hours": respect_quiet_hours,
        }, priority=10 if urgent else 40)
    return rows


def queue_document(session: Session, user: User, file, *, caption: str = "", urgent: bool = True) -> Message:
    """Send a stored file (an FBA label, say) to the owner on WhatsApp."""
    row = Message(direction="outbound", channel="whatsapp", user_id=user.id, body=caption,
                  status="queued", media_file_id=file.id, raw_payload={"urgent": urgent, "kind": "document"})
    session.add(row)
    session.flush()
    queue.enqueue(session, "send_messages", {"message_ids": [str(row.id)], "urgent": urgent,
                                             "respect_quiet_hours": False}, priority=10)
    return row


# ---------- delivery (runs inside the send_messages job) ----------
def _send_one(session: Session, channel, user: User, row: Message) -> str:
    """Text, or a document when the message carries a file."""
    if row.media_file_id is None:
        return channel.send_text(user.whatsapp_number, row.body)
    from app.db.models import File
    from app.services import files as files_svc

    stored = session.get(File, row.media_file_id)
    data = files_svc.get_store().get(stored.storage_key)
    return channel.send_document(user.whatsapp_number, data, stored.original_name or "document.pdf",
                                 stored.mime_type, caption=row.body or "")


def deliver(session: Session, message_ids: list[str], *, urgent: bool, respect_quiet_hours: bool) -> str:
    ids = [uuid.UUID(i) for i in message_ids]
    found = {m.id: m for m in session.execute(
        select(Message).where(Message.id.in_(ids), Message.status == "queued")).scalars()}
    rows = [found[i] for i in ids if i in found]  # keep the queued order
    if not rows:
        return "nothing_to_send"
    user = session.get(User, rows[0].user_id)
    if user is None or not user.is_active:
        _mark(session, rows, "failed", "recipient inactive")
        return "recipient_inactive"

    if respect_quiet_hours and not urgent and in_quiet_hours(session):
        _hold(session, rows, "quiet_hours")
        return "held_quiet_hours"
    if not can_send_freeform(user):
        _hold(session, rows, "window")
        _send_window_template(session, user)
        return "held_window"
    limit = get_settings().outbound_max_per_10min
    if recent_send_count(session) + len(rows) > limit:
        _hold(session, rows, "rate_limit")
        _rate_limit_alert(session, user, limit)
        return "held_rate_limit"

    channel = get_channel()
    for row in rows:
        try:
            provider_id = _send_one(session, channel, user, row)
        except ChannelError as exc:
            if exc.outside_window:
                pending = [r for r in rows if r.status == "queued"]
                _hold(session, pending, "window")
                _send_window_template(session, user)
                session.commit()
                return "held_window"
            if exc.retryable:
                session.commit()  # keep already-sent rows
                raise RetryableError(str(exc)) from exc
            _mark(session, [r for r in rows if r.status == "queued"], "failed", str(exc))
            audit.record(session, SYSTEM, "message_send_failed", target_type="message",
                         target_id=row.id, details={"error": str(exc)})
            session.commit()
            raise PermanentError(str(exc)) from exc
        row.provider_message_id = provider_id
        row.status = "sent"
        audit.record(session, SYSTEM, "message_sent", target_type="message", target_id=row.id,
                     details={"to": user.whatsapp_number, "chars": len(row.body or ""),
                              "document": bool(row.media_file_id),
                              "task_id": str(row.related_task_id) if row.related_task_id else None})
        session.commit()  # persist each send immediately: a retry never re-sends it
    return "sent"


def send_direct(session: Session, user: User, text: str) -> None:
    """Bypass the queue (used by the API when the worker is down)."""
    channel = get_channel()
    row = Message(direction="outbound", channel="whatsapp", user_id=user.id, body=text[:CHUNK_LIMIT],
                  status="queued", raw_payload={"direct": True})
    session.add(row)
    session.flush()
    try:
        if can_send_freeform(user):
            row.provider_message_id = channel.send_text(user.whatsapp_number, row.body)
        else:
            s = get_settings()
            row.provider_message_id = channel.send_template(
                user.whatsapp_number, s.whatsapp_template_name, s.whatsapp_template_lang,
                ["1", "a system alert"])
        row.status = "sent"
        audit.record(session, SYSTEM, "message_sent", target_type="message", target_id=row.id,
                     details={"direct": True})
    except ChannelError as exc:
        row.status = "failed"
        audit.record(session, SYSTEM, "message_send_failed", target_type="message", target_id=row.id,
                     details={"error": str(exc), "direct": True})
        log.error("direct send failed", extra={"error": str(exc)})


def release_held(session: Session, user: User, reasons: tuple[str, ...]) -> int:
    rows = list(session.execute(
        select(Message).where(Message.user_id == user.id, Message.direction == "outbound",
                              Message.status == "held")
        .order_by(Message.created_at, Message.id)
    ).scalars())
    rows = [r for r in rows if (r.raw_payload or {}).get("hold_reason") in reasons]
    if not rows:
        return 0
    for r in rows:
        r.status = "queued"
        r.raw_payload = {**(r.raw_payload or {}), "released_from": r.raw_payload.get("hold_reason"),
                         "hold_reason": None}
    session.flush()
    queue.enqueue(session, "send_messages", {
        "message_ids": [str(r.id) for r in rows], "urgent": False, "respect_quiet_hours": False,
    }, priority=30)
    audit.record(session, SYSTEM, "held_messages_released", target_type="user", target_id=user.id,
                 details={"count": len(rows), "reasons": list(reasons)})
    return len(rows)


def held_count(session: Session, user: User) -> int:
    return session.execute(
        select(func.count()).select_from(Message)
        .where(Message.user_id == user.id, Message.direction == "outbound", Message.status == "held")
    ).scalar_one()


def update_status(session: Session, provider_message_id: str, status: str, error: str | None) -> bool:
    order = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}
    row = session.execute(
        select(Message).where(Message.provider_message_id == provider_message_id)
    ).scalar_one_or_none()
    if row is None or row.direction != "outbound":
        return False
    if status == "failed":
        row.status = "failed"
        audit.record(session, SYSTEM, "message_delivery_failed", target_type="message", target_id=row.id,
                     details={"error": error})
    elif status in order and order[status] > order.get(row.status, -1):
        row.status = status
    return True


# ---------- internals ----------
def _mark(session: Session, rows: list[Message], status: str, reason: str) -> None:
    for r in rows:
        r.status = status
        r.raw_payload = {**(r.raw_payload or {}), "error": reason}


def _hold(session: Session, rows: list[Message], reason: str) -> None:
    for r in rows:
        r.status = "held"
        r.raw_payload = {**(r.raw_payload or {}), "hold_reason": reason}
    if rows:
        audit.record(session, SYSTEM, "messages_held", target_type="user", target_id=rows[0].user_id,
                     details={"count": len(rows), "reason": reason})


def template_params(session: Session, user: User) -> list[str]:
    """Values for the approved template body:
    "Kings Distro AI Manager: you have {{1}} new account update(s) about {{2}}. Reply to this message to view the details."
    """
    held = list(session.execute(
        select(Message).where(Message.user_id == user.id, Message.direction == "outbound",
                              Message.status == "held")
        .order_by(Message.created_at, Message.id)
    ).scalars())
    count = max(len(held), 1)
    return [str(count), _topic(session, held[0]) if held else "your account"]


_TOPIC_PREFIXES = [
    ("📧 EMAIL ALERT", "an important email"),
    ("📧", "your email"),
    ("⚠️ SUSPICIOUS EMAIL", "a suspicious email"),
    ("☀️ Daily summary", "your daily summary"),
    ("⏰ Task reminder", "your task reminders"),
    ("⏸️ AI layer paused", "a system notice"),
    ("⌛ Approval", "an expired approval"),
    ("⚠️", "a system notice"),
]


def _topic(session: Session, msg: Message) -> str:
    """Short, single-line description (template parameters can't contain newlines)."""
    body = (msg.body or "").strip()
    m = re.match(r"🔐 Approval needed: (A-\d+)", body)
    if m:
        return f"approval {m.group(1)}"
    m = re.match(r"📧 EMAIL ALERT — .*\((E-\d+)\)", body)
    if m:
        return f"email {m.group(1)}"
    for prefix, topic in _TOPIC_PREFIXES:
        if body.startswith(prefix):
            return topic
    if msg.related_task_id:
        from app.db.models import Task

        task = session.get(Task, msg.related_task_id)
        if task:
            title = re.sub(r"\s+", " ", task.title)[:40].strip()
            return f"task {task.ref} ({title})"
    return "your tasks"


def _send_window_template(session: Session, user: User) -> None:
    key = f"window_template:{user.id}:{user.last_inbound_at.isoformat() if user.last_inbound_at else 'never'}"
    if not health.claim_notification(session, key):
        return
    s = get_settings()
    row = Message(direction="outbound", channel="whatsapp", user_id=user.id, status="queued",
                  body=f"[template {s.whatsapp_template_name}]", raw_payload={"template": True})
    session.add(row)
    session.flush()
    try:
        params = template_params(session, user)
        row.body = f"[template {s.whatsapp_template_name}: {params[0]} update(s) about {params[1]}]"
        row.provider_message_id = get_channel().send_template(
            user.whatsapp_number, s.whatsapp_template_name, s.whatsapp_template_lang, params)
        row.status = "sent"
        audit.record(session, SYSTEM, "message_sent", target_type="message", target_id=row.id,
                     details={"template": s.whatsapp_template_name, "params": params})
    except ChannelError as exc:
        row.status = "failed"
        health.release_notification(session, key)
        audit.record(session, SYSTEM, "message_send_failed", target_type="message", target_id=row.id,
                     details={"error": str(exc), "template": True})
        if exc.retryable:
            session.commit()
            raise RetryableError(str(exc)) from exc


def _rate_limit_alert(session: Session, user: User, limit: int) -> None:
    key = f"rate_limit_alert:{utcnow():%Y%m%d%H}"
    if not health.claim_notification(session, key):
        return
    text = (f"⚠️ Safety limit: more than {limit} messages in 10 minutes, so further updates are "
            f"on hold. Reply HELP or STATUS to continue.")
    send_direct(session, user, text)


def payload_flag(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key)
    return default if value is None else bool(value)
