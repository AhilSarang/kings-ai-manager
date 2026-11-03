"""Runtime switches stored in the `settings` table."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Setting
from app.services import audit
from app.services.common import Actor

_ENV_FALLBACK = {
    "daily_summary_time": lambda: get_settings().daily_summary_time,
    "llm_concurrency": lambda: get_settings().llm_concurrency,
}


def get(session: Session, key: str, default: Any = None) -> Any:
    value = session.execute(select(Setting.value).where(Setting.key == key)).scalar_one_or_none()
    if value is None and key in _ENV_FALLBACK:
        return _ENV_FALLBACK[key]()
    return default if value is None else value


def set_value(session: Session, key: str, value: Any, actor: Actor) -> None:
    old = get(session, key)
    stmt = insert(Setting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": value})
    session.execute(stmt)
    audit.record(session, actor, "setting_changed", target_type="setting", target_id=key,
                 details={"old": old, "new": value})


def is_paused(session: Session) -> bool:
    return bool(get(session, "system_paused", False))
