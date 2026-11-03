"""Helpers to create email rows in tests."""

from __future__ import annotations

import uuid
from datetime import timedelta

from app.db.models import EmailDraft, EmailMessage, EmailThread
from app.email import accounts
from app.services.common import utcnow


def make_thread(db, *, subject="Price update", **kw) -> EmailThread:
    account = accounts.get_primary(db)
    t = EmailThread(account_id=account.id, provider_thread_id=kw.pop("provider_thread_id", uuid.uuid4().hex[:16]),
                    subject=subject, last_message_at=kw.pop("last_message_at", utcnow()), **kw)
    db.add(t)
    db.flush()
    db.refresh(t)
    return t


def make_message(db, thread, *, from_address="supplier@example.com", body="Hello", minutes_ago=0, **kw):
    m = EmailMessage(thread_id=thread.id, provider_message_id=kw.pop("provider_message_id", uuid.uuid4().hex[:16]),
                     from_address=from_address, subject=thread.subject, body_text=body,
                     sent_at=utcnow() - timedelta(minutes=minutes_ago), **kw)
    db.add(m)
    db.flush()
    return m


def make_draft(db, thread, **kw) -> EmailDraft:
    d = EmailDraft(thread_id=thread.id, to=kw.pop("to", ["supplier@example.com"]), subject=f"Re: {thread.subject}",
                   body_text=kw.pop("body_text", "Thanks, confirmed."), **kw)
    db.add(d)
    db.flush()
    db.refresh(d)
    return d
