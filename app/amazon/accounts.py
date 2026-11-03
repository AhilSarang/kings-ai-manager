"""The Kings Distro Amazon account row, created from settings on first use."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon import marketplaces as mkt
from app.config import get_settings
from app.db.models import AmazonAccount
from app.services import settings_store
from app.services.common import ServiceError


class AmazonNotConnected(ServiceError):
    pass


def get_primary(session: Session) -> AmazonAccount | None:
    return session.execute(select(AmazonAccount).order_by(AmazonAccount.created_at)).scalars().first()


def ensure(session: Session) -> AmazonAccount:
    """Create (or refresh) the account row from .env. Credentials stay in .env, never in the table."""
    s = get_settings()
    if not s.amazon_seller_id:
        raise AmazonNotConnected("Amazon isn't connected: AMAZON_SELLER_ID is not set.")
    account = get_primary(session)
    if account is None:
        account = AmazonAccount(seller_id=s.amazon_seller_id, region=s.amazon_region)
        session.add(account)
    account.seller_id = s.amazon_seller_id
    account.region = s.amazon_region
    # Anything the owner turned on from WhatsApp is stored in settings and beats .env, so a restart
    # (or a stale .env) can never quietly re-enable writes or drop a marketplace he enabled.
    codes = settings_store.get(session, "amazon_enabled_marketplaces", None)
    account.enabled_marketplaces = [m.code for m in mkt.enabled(codes or s.amazon_enabled_marketplaces)]
    stored = settings_store.get(session, "amazon_write_mode", None)
    account.write_mode = stored if stored in ("validation_only", "live") else s.amazon_write_mode
    if account.status != "auth_error":
        account.status = "active"
    session.flush()
    return account


def require(session: Session) -> AmazonAccount:
    account = get_primary(session)
    if account is None and get_settings().amazon_seller_id:
        account = ensure(session)      # first use after deployment: build the row from .env
    if account is None or account.status != "active":
        raise AmazonNotConnected(
            "Amazon isn't connected right now." if account is None else
            f"Amazon access is {account.status.replace('_', ' ')}: {account.status_reason or 'needs re-authorising'}")
    return account


def set_status(session: Session, account: AmazonAccount, status: str, reason: str | None = None) -> None:
    account.status = status
    account.status_reason = reason
    session.flush()


def writes_allowed(session: Session) -> bool:
    account = get_primary(session)
    return bool(account and account.status == "active" and account.write_mode == "live")
