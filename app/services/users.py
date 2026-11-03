"""User lookup and owner seeding."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import User
from app.services import audit
from app.services.common import SYSTEM


def normalize_number(number: str) -> str:
    """Meta sends numbers without '+'. Store and compare in E.164."""
    digits = "".join(ch for ch in number if ch.isdigit())
    return f"+{digits}"


def get_active_by_number(session: Session, number: str) -> User | None:
    return session.execute(
        select(User).where(User.whatsapp_number == normalize_number(number), User.is_active.is_(True))
    ).scalar_one_or_none()


def owners(session: Session) -> list[User]:
    """Everyone who runs the business. Alerts and approvals go to all of them."""
    return list(session.execute(
        select(User).where(User.role == "owner", User.is_active.is_(True)).order_by(User.created_at)
    ).scalars())


def get_owner(session: Session) -> User | None:
    return session.execute(
        select(User).where(User.role == "owner", User.is_active.is_(True)).order_by(User.created_at)
    ).scalars().first()


def ensure_owner(session: Session) -> User:
    """Idempotently create/update the owner from env settings."""
    s = get_settings()
    number = normalize_number(s.owner_whatsapp_number)
    user = session.execute(select(User).where(User.whatsapp_number == number)).scalar_one_or_none()
    if user is None:
        user = User(name=s.owner_name, whatsapp_number=number, email=s.owner_email, role="owner", is_active=True)
        session.add(user)
        session.flush()
        audit.record(session, SYSTEM, "owner_seeded", target_type="user", target_id=user.id)
    elif user.role != "owner" or not user.is_active:
        user.role, user.is_active = "owner", True
        audit.record(session, SYSTEM, "owner_reactivated", target_type="user", target_id=user.id)
    return user


# ---- approval PIN (stored hashed only) ----
_ITER = 200_000


def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), _ITER).hex()
    return f"pbkdf2_sha256${_ITER}${salt}${digest}"


def verify_pin(pin: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, iters, salt, digest = stored.split("$")
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(iters)).hex()
    return hmac.compare_digest(candidate, digest)
