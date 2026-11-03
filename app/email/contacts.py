"""Email contacts used as triage context."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EMAIL_CONTACT_TYPES, EmailContact
from app.email.rules import EMAIL_RE
from app.services import audit
from app.services.common import Actor, ServiceError


class InvalidContact(ServiceError):
    pass


def normalize_address(address: str) -> str:
    return (address or "").strip().lower()


def get(session: Session, address: str) -> EmailContact | None:
    return session.execute(
        select(EmailContact).where(EmailContact.address == normalize_address(address))
    ).scalar_one_or_none()


def record_seen(session: Session, address: str, name: str | None, seen_at: datetime | None) -> EmailContact | None:
    address = normalize_address(address)
    if not EMAIL_RE.match(address):
        return None
    contact = get(session, address)
    if contact is None:
        contact = EmailContact(address=address, name=(name or None), domain=address.split("@", 1)[1],
                               type="other", first_seen_at=seen_at, message_count=0)
        session.add(contact)
    if name and not contact.name:
        contact.name = name[:320]
    contact.message_count += 1
    session.flush()
    return contact


def set_type(session: Session, address: str, *, actor: Actor, type: str | None = None,
             is_vip: bool | None = None) -> EmailContact:
    address = normalize_address(address)
    if not EMAIL_RE.match(address):
        raise InvalidContact(f"'{address}' is not an email address")
    if type is not None and type not in EMAIL_CONTACT_TYPES:
        raise InvalidContact(f"Unknown contact type '{type}'")
    contact = get(session, address)
    if contact is None:
        contact = EmailContact(address=address, domain=address.split("@", 1)[1], type="other")
        session.add(contact)
    if type is not None:
        contact.type = type
        contact.type_set_by_owner = True
    if is_vip is not None:
        contact.is_vip = is_vip
    session.flush()
    audit.record(session, actor, "email_contact_updated", target_type="email_contact", target_id=address,
                 details={"type": type, "is_vip": is_vip})
    return contact
