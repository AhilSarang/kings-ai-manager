"""Worker heartbeat and staleness detection."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Heartbeat, Notification
from app.services.common import utcnow

WORKER = "worker"
STALE_AFTER = timedelta(minutes=15)


def beat(session: Session, component: str = WORKER, details: dict[str, Any] | None = None) -> None:
    now = utcnow()
    stmt = insert(Heartbeat).values(component=component, last_seen_at=now, details=details or {})
    stmt = stmt.on_conflict_do_update(
        index_elements=[Heartbeat.component],
        set_={"last_seen_at": now, "details": details or {}, "updated_at": now},
    )
    session.execute(stmt)


def last_seen(session: Session, component: str = WORKER) -> datetime | None:
    return session.execute(
        select(Heartbeat.last_seen_at).where(Heartbeat.component == component)
    ).scalar_one_or_none()


def worker_is_stale(session: Session, *, now: datetime | None = None) -> bool:
    seen = last_seen(session)
    return seen is None or (now or utcnow()) - seen > STALE_AFTER


def claim_notification(session: Session, key: str) -> bool:
    """Record that a one-off notice is being sent. False if it was already sent."""
    stmt = (insert(Notification).values(key=key)
            .on_conflict_do_nothing(index_elements=[Notification.key]).returning(Notification.key))
    return session.execute(stmt).scalar_one_or_none() is not None


def release_notification(session: Session, key: str) -> None:
    session.query(Notification).filter(Notification.key == key).delete()
