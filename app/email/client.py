"""Factory for the Gmail client of an account, and auth-failure handling."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import EmailAccount
from app.email import accounts, crypto
from app.email.gmail import GmailAuthError, GmailClient, HttpGmailClient
from app.services import health
from app.services.common import SYSTEM
from app.services.notify import notify_owner

log = logging.getLogger(__name__)
_clients: dict[str, HttpGmailClient] = {}


def auth_notice_key(account: EmailAccount) -> str:
    return f"gmail_auth_error:{account.id}"


def get_client(account: EmailAccount) -> GmailClient:
    if get_settings().gmail_backend == "fake":
        from app.email.fake_gmail import FakeGmailClient

        return FakeGmailClient()
    if not account.encrypted_refresh_token:
        raise GmailAuthError("Gmail is not authorised yet")
    try:
        token = crypto.decrypt(account.encrypted_refresh_token)
    except crypto.TokenCryptoError as exc:
        raise GmailAuthError(str(exc)) from exc
    cached = _clients.get(account.address)
    if cached is None or cached.refresh_token != token:
        cached = HttpGmailClient(token)
        _clients[account.address] = cached
    return cached


def google_access_token(session: Session) -> str:
    """An access token for the connected Google account, for Drive as well as Gmail.

    Drive scopes are granted at consent time, so this needs a re-authorisation that included them.
    """
    from app.email import accounts as accounts_svc

    account = accounts_svc.get_primary(session)
    if account is None or account.status != "active":
        raise GmailAuthError("Google isn't connected right now.")
    client = get_client(account)
    token = getattr(client, "_access_token", None)
    if token is None:
        raise GmailAuthError("This Google client can't issue access tokens.")
    return token()


def handle_auth_failure(session: Session, account: EmailAccount, error: Exception) -> None:
    """Mark the account, stop Gmail work, tell the owner once."""
    changed = accounts.set_status(session, account, "auth_error", SYSTEM, reason=str(error)[:300])
    _clients.pop(account.address, None)
    if changed:
        log.error("gmail auth failed", extra={"account": account.address, "error": str(error)[:200]})
    notify_owner(
        session,
        f"📧⚠️ Gmail access for {account.address} stopped working ({str(error)[:120]}). Email syncing and alerts "
        "are paused and no email is lost; it will catch up after you re-connect Gmail. "
        "To re-connect, run on the Mac: uv run python scripts/gmail_authorize.py (see docs/RUNBOOK.md).",
        key=auth_notice_key(account), urgent=True, respect_quiet_hours=False,
    )


def store_refresh_token(session: Session, account: EmailAccount, refresh_token: str) -> None:
    account.encrypted_refresh_token = crypto.encrypt(refresh_token)
    accounts.set_status(session, account, "active", SYSTEM, reason=None)
    health.release_notification(session, auth_notice_key(account))
    _clients.pop(account.address, None)
