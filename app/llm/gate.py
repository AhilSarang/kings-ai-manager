"""Global LLM pause gate, shared by all worker processes via the settings table."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.services import settings_store
from app.services.common import SYSTEM, utcnow

KEY = "llm_paused_until"


def paused_until(session: Session) -> datetime | None:
    raw = settings_store.get(session, KEY)
    return datetime.fromisoformat(raw) if raw else None


def is_llm_paused(session: Session, now: datetime | None = None) -> bool:
    until = paused_until(session)
    return until is not None and until > (now or utcnow())


def pause(session: Session, until: datetime, reason: str) -> None:
    settings_store.set_value(session, KEY, until.isoformat(), SYSTEM)
    settings_store.set_value(session, "llm_pause_reason", reason, SYSTEM)


def clear(session: Session) -> None:
    if paused_until(session) is not None:
        settings_store.set_value(session, KEY, None, SYSTEM)
