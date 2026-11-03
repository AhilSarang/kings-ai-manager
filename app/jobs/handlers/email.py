"""Gmail jobs: sync, watch renewal, attachment download."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.db.models import EmailAccount, EmailAttachment, EmailMessage
from app.email import sync
from app.email.client import get_client, handle_auth_failure
from app.email.gmail import GmailAuthError, GmailError, GmailRetryable
from app.jobs.errors import PermanentError, RetryableError
from app.jobs.handlers import handler
from app.services import audit, files
from app.services.common import SYSTEM, Actor

log = logging.getLogger(__name__)


def _account(session, payload) -> EmailAccount | None:
    account = session.get(EmailAccount, uuid.UUID(payload["account_id"]))
    if account is None or account.status != "active":
        return None
    return account


def _auth_failed(session, account_id, exc) -> None:
    session.rollback()
    account = session.get(EmailAccount, account_id)
    handle_auth_failure(session, account, exc)
    session.commit()


@handler("gmail_sync")
def gmail_sync(session, payload, job) -> None:
    if sync.email_processing_paused(session):
        return  # polling queues a new sync after RESUME / EMAIL RESUME
    account = _account(session, payload)
    if account is None:
        return
    account_id = account.id
    try:
        stats = sync.sync_account(session, account, get_client(account))
    except GmailAuthError as exc:
        _auth_failed(session, account_id, exc)
        return
    except GmailRetryable as exc:
        raise RetryableError(str(exc)) from exc
    log.info("gmail sync", extra={"mode": stats.mode, "stored": stats.stored, "skipped": stats.skipped})


@handler("gmail_watch_renew")
def gmail_watch_renew(session, payload, job) -> None:
    account = _account(session, payload)
    if account is None:
        return
    account_id = account.id
    try:
        resp = get_client(account).watch(get_settings().gmail_pubsub_topic)
    except GmailAuthError as exc:
        _auth_failed(session, account_id, exc)
        return
    except GmailRetryable as exc:
        raise RetryableError(str(exc)) from exc
    except GmailError as exc:
        audit.record(session, SYSTEM, "gmail_watch_failed", target_type="email_account", target_id=account.address,
                     details={"error": str(exc)[:300]})
        session.commit()
        raise PermanentError(f"Gmail watch failed (polling still runs): {exc}") from exc
    account.watch_expires_at = datetime.fromtimestamp(int(resp["expiration"]) / 1000, UTC)
    audit.record(session, SYSTEM, "gmail_watch_renewed", target_type="email_account", target_id=account.address,
                 details={"expires": account.watch_expires_at.isoformat()})


@handler("email_fetch_attachment")
def email_fetch_attachment(session, payload, job) -> None:
    att = session.get(EmailAttachment, uuid.UUID(payload["attachment_id"]))
    if att is None or att.file_id or att.skipped_reason:
        return
    msg = session.get(EmailMessage, att.message_id)
    account = msg.thread.account
    if account.status != "active":
        return
    try:
        data = get_client(account).get_attachment(msg.provider_message_id, att.provider_attachment_id)
    except GmailAuthError as exc:
        _auth_failed(session, account.id, exc)
        return
    except GmailRetryable as exc:
        raise RetryableError(str(exc)) from exc
    except GmailError as exc:
        att.skipped_reason = f"download failed: {str(exc)[:100]}"
        return
    try:
        record = files.save_file(session, data, mime_type=att.mime_type or "application/octet-stream",
                                 source="email", actor=Actor.agent("email"), original_name=att.filename,
                                 related_task_id=msg.thread.linked_task_id)
    except files.FileRejected as exc:
        att.skipped_reason = str(exc)[:200]
        return
    att.file_id = record.id
    att.size_bytes = len(data)


@handler("triage_emails")
def triage_emails(session, payload, job) -> None:
    from app.email import triage

    if sync.email_processing_paused(session):
        return  # messages stay pending; polling queues triage again after resume
    n = triage.triage_pending(session)
    log.info("email triage", extra={"processed": n})
    if n:
        sync.enqueue_triage(session)  # more may be waiting


@handler("send_email_alert")
def send_email_alert(session, payload, job) -> None:
    from app.email import alerts

    if sync.email_processing_paused(session):
        alerts.defer(session, job)  # keep it until email is resumed
        return
    log.info("email alert", extra={"result": alerts.process(session, job)})


def _draft_failed(job, error: str) -> None:
    from app.db.session import session_scope
    from app.services import users
    from app.services import outbound

    with session_scope() as s:
        owner = users.get_owner(s)
        if owner:
            outbound.queue_text(s, owner, "⚠️ I couldn't write that email draft this time. Please ask again.",
                                urgent=True)


@handler("draft_email_reply", on_final_failure=_draft_failed)
def draft_email_reply(session, payload, job) -> None:
    from app.email import alerts, drafting
    from app.llm.base import LLMError

    if sync.email_processing_paused(session):
        alerts.defer(session, job, minutes=10)
        return
    from app.services.common import ServiceError

    try:
        draft = drafting.generate(session, payload)
    except (LLMError, ServiceError) as exc:
        raise PermanentError(str(exc)) from exc
    drafting.deliver_draft_message(session, draft)
