"""Outbound sending, owner notices, held-message release, media download."""

from __future__ import annotations

import logging
import uuid

from app.channels import get_channel
from app.channels.base import ChannelError
from app.db.models import Message, User
from app.jobs import queue
from app.jobs.errors import PermanentError, RetryableError
from app.jobs.handlers import handler
from app.services import audit, files, outbound, users
from app.services.common import SYSTEM, Actor

log = logging.getLogger(__name__)


@handler("send_messages", critical=True)
def send_messages(session, payload, job) -> None:
    result = outbound.deliver(
        session, payload["message_ids"],
        urgent=outbound.payload_flag(payload, "urgent", False),
        respect_quiet_hours=outbound.payload_flag(payload, "respect_quiet_hours", True),
    )
    log.info("delivery", extra={"result": result, "count": len(payload["message_ids"])})


@handler("send_owner_message", critical=True)
def send_owner_message(session, payload, job) -> None:
    recipients = users.owners(session)
    if not recipients:
        raise PermanentError("No active owner user")
    related = payload.get("related_task_id")
    for owner in recipients:
        outbound.queue_text(
            session, owner, payload["text"],
            urgent=outbound.payload_flag(payload, "urgent", False),
            related_task_id=uuid.UUID(related) if related else None,
            respect_quiet_hours=outbound.payload_flag(payload, "respect_quiet_hours", True),
        )


@handler("release_held_messages", critical=True)
def release_held_messages(session, payload, job) -> None:
    user = session.get(User, uuid.UUID(payload["user_id"]))
    if user:
        outbound.release_held(session, user, tuple(payload.get("reasons") or ("window",)))


@handler("download_media")
def download_media(session, payload, job) -> None:
    msg = session.get(Message, uuid.UUID(payload["message_id"]))
    if msg is None:
        raise PermanentError("Inbound message not found")
    media_error = None
    if msg.media_file_id is None:
        try:
            media = get_channel().download_media(payload["media_id"])
            record = files.save_file(
                session, media.data, mime_type=payload.get("mime_type") or media.mime_type,
                source="whatsapp", actor=Actor.user(msg.user_id),
                original_name=payload.get("filename") or media.filename,
                uploaded_by_user_id=msg.user_id,
            )
            msg.media_file_id = record.id
        except files.FileRejected as exc:
            media_error = str(exc)
        except ChannelError as exc:
            if exc.retryable and job.attempts < job.max_attempts:
                raise RetryableError(str(exc)) from exc
            media_error = "I couldn't download that file from WhatsApp"
        if media_error:
            msg.raw_payload = {**(msg.raw_payload or {}), "media_error": media_error}
            audit.record(session, SYSTEM, "media_rejected", target_type="message", target_id=msg.id,
                         details={"error": media_error})
    queue.enqueue(session, "process_inbound_message", {"message_id": str(msg.id)},
                  idempotency_key=f"inbound:{msg.provider_message_id}", priority=20)
