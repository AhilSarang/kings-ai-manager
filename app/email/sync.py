"""Gmail sync: history-based incremental import, 2-day recovery, 30-day backfill.

Idempotent on Gmail message id. Each message is committed on its own, so an
interrupted sync resumes where it stopped.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import EmailAccount, EmailAttachment, EmailMessage, EmailThread, Job
from app.email import contacts, followup
from app.email.gmail import GmailClient, GmailError, GmailHistoryGone
from app.email.parse import ParsedEmail, parse_message
from app.jobs import queue
from app.services import settings_store
from app.services.common import utcnow
from app.services.notify import notify_owner

log = logging.getLogger(__name__)

BACKFILL_DAYS = 30
BACKFILL_TRIAGE_DAYS = 7
BACKFILL_MAX_MESSAGES = 3000
RECOVERY_QUERY = "newer_than:2d"
SKIP_LABELS = {"SPAM", "TRASH", "DRAFT"}
KEEP_LABELS = {"INBOX", "SENT"}


@dataclass
class SyncStats:
    fetched: int = 0
    stored: int = 0
    skipped: int = 0
    mode: str = "history"


def email_processing_paused(session: Session) -> bool:
    return settings_store.is_paused(session) or bool(settings_store.get(session, "email_paused", False))


def enqueue_sync(session: Session, account_id: uuid.UUID, *, delay_seconds: int = 0) -> Job | None:
    """Queue a sync unless one is already waiting (pushes and polls coalesce)."""
    pending = session.execute(
        select(func.count()).select_from(Job).where(
            Job.type == "gmail_sync", Job.status == "pending",
            Job.payload["account_id"].astext == str(account_id))
    ).scalar_one()
    if pending:
        return None
    return queue.enqueue(session, "gmail_sync", {"account_id": str(account_id)},
                         run_after=utcnow() + timedelta(seconds=delay_seconds), priority=40, max_attempts=6)


def enqueue_triage(session: Session) -> Job | None:
    pending_job = session.execute(
        select(func.count()).select_from(Job).where(Job.type == "triage_emails",
                                                    Job.status.in_(("pending", "waiting_llm")))
    ).scalar_one()
    if pending_job:
        return None
    waiting = session.execute(
        select(func.count()).select_from(EmailMessage).where(EmailMessage.triage_status == "pending")
    ).scalar_one()
    if not waiting:
        return None
    return queue.enqueue(session, "triage_emails", {}, uses_llm=True, priority=35, max_attempts=4)


# ---------------- sync ----------------
def sync_account(session: Session, account: EmailAccount, client: GmailClient) -> SyncStats:
    if account.last_history_id is None:
        return backfill(session, account, client)
    stats = SyncStats()
    ids: list[str] = []
    try:
        page = None
        latest = account.last_history_id
        while True:
            resp = client.list_history(account.last_history_id, page)
            for record in resp.get("history") or []:
                for added in record.get("messagesAdded") or []:
                    m = added.get("message") or {}
                    if _wanted(m.get("labelIds")):
                        ids.append(m["id"])
            latest = max(latest, int(resp.get("historyId") or latest))
            page = resp.get("nextPageToken")
            if not page:
                break
    except GmailHistoryGone:
        stats.mode = "recovery"
        log.warning("gmail history too old; recovering last 2 days", extra={"account": account.address})
        latest = int(client.get_profile().get("historyId"))
        ids = _list_ids(client, RECOVERY_QUERY, limit=1000)
    _ingest_ids(session, account, client, ids, stats, backfill=False)
    account.last_history_id = max(account.last_history_id or 0, latest)
    account.last_synced_at = utcnow()
    session.commit()
    if stats.stored:
        enqueue_triage(session)
        session.commit()
    return stats


def backfill(session: Session, account: EmailAccount, client: GmailClient) -> SyncStats:
    stats = SyncStats(mode="backfill")
    start_history = int(client.get_profile().get("historyId"))  # anything newer is caught by the next sync
    ids = _list_ids(client, f"newer_than:{BACKFILL_DAYS}d", limit=BACKFILL_MAX_MESSAGES)
    _ingest_ids(session, account, client, ids, stats, backfill=True)
    account.last_history_id = start_history
    account.last_synced_at = utcnow()
    account.backfill_completed_at = utcnow()
    session.commit()
    triage_n = session.execute(
        select(func.count()).select_from(EmailMessage).where(EmailMessage.triage_status == "pending")
    ).scalar_one()
    stored_n = session.execute(  # whole mailbox, not just this attempt (backfill resumes after rate limits)
        select(func.count()).select_from(EmailMessage).join(EmailThread)
        .where(EmailThread.account_id == account.id)
    ).scalar_one()
    threads_n = session.execute(
        select(func.count()).select_from(EmailThread).where(EmailThread.account_id == account.id)
    ).scalar_one()
    notify_owner(
        session,
        f"📧 Gmail connected ({account.address}). Imported {stored_n} emails in {threads_n} threads from the "
        f"last {BACKFILL_DAYS} days. Sorting the {triage_n} received in the last {BACKFILL_TRIAGE_DAYS} days now; "
        "no alerts are sent for this older mail. New mail will be checked continuously.",
        key=f"gmail_backfill:{account.id}",
    )
    enqueue_triage(session)
    session.commit()
    log.info("gmail backfill done", extra={"account": account.address, "stored": stats.stored})
    return stats


def _wanted(labels: list[str] | None) -> bool:
    labels = set(labels or [])
    return not (labels & SKIP_LABELS) and bool(labels & KEEP_LABELS)


def _list_ids(client: GmailClient, query: str, limit: int) -> list[str]:
    ids, page = [], None
    while len(ids) < limit:
        resp = client.list_messages(query, page, max_results=min(500, limit - len(ids)))
        ids += [m["id"] for m in resp.get("messages") or []]
        page = resp.get("nextPageToken")
        if not page:
            break
    ids.reverse()  # oldest first, so thread state ends on the newest message
    return ids


def _ingest_ids(session, account, client, ids, stats, *, backfill: bool) -> None:
    seen: set[str] = set()
    for mid in ids:
        if mid in seen:
            continue
        seen.add(mid)
        if session.execute(select(EmailMessage.id).where(EmailMessage.provider_message_id == mid)).first():
            stats.skipped += 1
            continue
        try:
            raw = client.get_message(mid)
        except GmailError as exc:
            if "404" in str(exc):  # deleted before we fetched it
                stats.skipped += 1
                continue
            raise
        stats.fetched += 1
        if not _wanted(raw.get("labelIds")):
            stats.skipped += 1
            continue
        ingest_message(session, account, raw, backfill=backfill)
        session.commit()
        stats.stored += 1


def ingest_message(session: Session, account: EmailAccount, raw: dict, *, backfill: bool = False) -> EmailMessage:
    s = get_settings()
    p = parse_message(raw, body_cap_bytes=s.email_body_cap_bytes)
    is_owner = p.from_address == account.address.lower() or "SENT" in p.labels
    thread = _upsert_thread(session, account, p)

    msg = EmailMessage(
        thread_id=thread.id, provider_message_id=p.provider_message_id, rfc_message_id=p.rfc_message_id,
        from_address=p.from_address, from_name=p.from_name or None, to=p.to, cc=p.cc, reply_to=p.reply_to,
        subject=p.subject, sent_at=p.sent_at, body_text=p.body_text, snippet=p.snippet, gmail_labels=p.labels,
        gmail_category=p.gmail_category, auth_spf=p.auth_spf, auth_dkim=p.auth_dkim, auth_dmarc=p.auth_dmarc,
        dkim_domains=p.dkim_domains, is_from_owner=is_owner, is_self_sent=p.is_self_sent, headers=p.headers,
    )
    if p.hidden_text_detected:
        msg.headers = {**p.headers, "_hidden_text_removed": "true"}
    if is_owner or p.is_self_sent:
        msg.triage_status = "skipped"
    elif backfill and p.sent_at and p.sent_at < utcnow() - timedelta(days=BACKFILL_TRIAGE_DAYS):
        msg.triage_status = "backfill"
    else:
        msg.triage_status = "pending"
        if backfill:
            msg.triage_result = {"backfill": True}  # triage runs, but alerts are suppressed
    session.add(msg)
    session.flush()

    _update_thread_state(session, thread, msg, is_owner)
    if is_owner:
        _link_sent_draft(session, thread, msg)
    if not is_owner:
        contacts.record_seen(session, p.from_address, p.from_name, p.sent_at)
    for att in p.attachments:
        _add_attachment(session, msg, att)
    return msg


def _link_sent_draft(session: Session, thread: EmailThread, msg: EmailMessage) -> None:
    from app.db.models import EmailDraft

    draft = session.execute(select(EmailDraft).where(
        EmailDraft.sent_provider_message_id == msg.provider_message_id, EmailDraft.thread_id.is_(None))
    ).scalar_one_or_none()
    if draft is not None:
        draft.thread_id = thread.id


def _upsert_thread(session: Session, account: EmailAccount, p: ParsedEmail) -> EmailThread:
    thread = session.execute(
        select(EmailThread).where(EmailThread.account_id == account.id,
                                  EmailThread.provider_thread_id == p.provider_thread_id)
    ).scalar_one_or_none()
    if thread is None:
        subject = p.subject
        thread = EmailThread(account_id=account.id, provider_thread_id=p.provider_thread_id,
                             subject=subject[:500], participants=[], first_message_at=p.sent_at)
        session.add(thread)
        session.flush()
        session.refresh(thread)
    people = set(thread.participants or [])
    people.update(a for a in [p.from_address, *p.to, *p.cc] if a and a != account.address.lower())
    thread.participants = sorted(people)
    if p.sent_at and (thread.first_message_at is None or p.sent_at < thread.first_message_at):
        thread.first_message_at = p.sent_at
    return thread


def _update_thread_state(session: Session, thread: EmailThread, msg: EmailMessage, is_owner: bool) -> None:
    is_latest = thread.last_message_at is None or (msg.sent_at and msg.sent_at >= thread.last_message_at)
    if not is_latest:
        return
    thread.last_message_at = msg.sent_at or utcnow()
    thread.last_message_from_owner = is_owner
    if is_owner:
        thread.needs_reply = False
        if thread.status in ("open", "waiting_on_them"):
            thread.status = "waiting_on_them"
            if thread.follow_up_tracking:
                days = int(settings_store.get(session, "email_followup_business_days", 3) or 3)
                thread.follow_up_due_at = followup.add_business_days(thread.last_message_at, days)
    else:
        thread.follow_up_due_at = None
        if thread.status in ("waiting_on_them", "handled"):
            thread.status = "open"


def _add_attachment(session: Session, msg: EmailMessage, att) -> None:
    limit = get_settings().email_max_attachment_mb * 1024 * 1024
    row = EmailAttachment(message_id=msg.id, provider_attachment_id=att.attachment_id, filename=att.filename,
                          mime_type=att.mime_type, size_bytes=att.size)
    if not att.attachment_id:
        row.skipped_reason = "no attachment data"
    elif att.size > limit:
        row.skipped_reason = f"larger than {limit // (1024 * 1024)} MB"
    session.add(row)
    session.flush()
    if row.skipped_reason is None:
        queue.enqueue(session, "email_fetch_attachment", {"attachment_id": str(row.id)}, priority=60,
                      idempotency_key=f"email_att:{row.id}")
