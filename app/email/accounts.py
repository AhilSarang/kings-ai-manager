"""The monitored mailbox (one in Phase 2; the schema allows more)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import EMAIL_ACCOUNT_STATUSES, EmailAccount
from app.services import audit
from app.services.common import Actor


def get_primary(session: Session, *, create: bool = True) -> EmailAccount | None:
    address = get_settings().gmail_account_address.lower()
    account = session.execute(select(EmailAccount).where(EmailAccount.address == address)).scalar_one_or_none()
    if account is None and create:
        account = EmailAccount(address=address, provider="gmail", status="paused",
                               status_reason="Not authorised yet (run scripts/gmail_authorize.py)")
        session.add(account)
        session.flush()
    return account


def set_status(session: Session, account: EmailAccount, status: str, actor: Actor, reason: str | None = None) -> bool:
    """Returns True if the status changed."""
    assert status in EMAIL_ACCOUNT_STATUSES
    if account.status == status and account.status_reason == reason:
        return False
    old = account.status
    account.status = status
    account.status_reason = reason
    audit.record(session, actor, "email_account_status_changed", target_type="email_account",
                 target_id=account.address, details={"from": old, "to": status, "reason": reason})
    session.flush()
    return old != status
