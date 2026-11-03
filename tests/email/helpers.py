"""Shared helpers for email tests: inbox with a connected account and quick message creation."""

from __future__ import annotations

from app.email import accounts
from app.email.client import store_refresh_token
from app.email.fake_gmail import FakeGmailClient as G
from app.email.sync import ingest_message

GENUINE_AMAZON_AUTH = ("mx.google.com; dkim=pass header.i=@amazon.com header.s=a; spf=pass; "
                       "dmarc=pass header.from=amazon.com")


def connect(db):
    a = accounts.get_primary(db)
    store_refresh_token(db, a, "1//" + "t" * 40)
    a.last_history_id = 1
    db.commit()
    return a


def receive(db, account, *, backfill=False, **kw):
    """Put a message in the fake mailbox and ingest it directly (as sync would)."""
    raw = G.add_message(**kw)
    msg = ingest_message(db, account, raw, backfill=backfill)
    db.commit()
    return msg


def triage_reply(*results):
    return {"results": [
        {"message_id": r.get("id", f"m{i}"), "category": r.get("category", "other"),
         "importance": r.get("importance", "normal"), "needs_reply": r.get("needs_reply", False),
         "alert_rule_matches": r.get("matches", []), "summary": r.get("summary", "Summary."),
         "suggested_action": r.get("action", ""), "confidence": r.get("confidence", 0.9)}
        for i, r in enumerate(results, start=1)
    ]}
