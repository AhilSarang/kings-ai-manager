"""Append-only audit log. Application code only ever inserts."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import AuditLog
from app.services.common import Actor


def record(
    session: Session,
    actor: Actor,
    action: str,
    *,
    target_type: str | None = None,
    target_id: Any = None,
    details: dict[str, Any] | None = None,
    trigger_message_id: uuid.UUID | None = None,
    llm_call_id: uuid.UUID | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_type=actor.type,
        actor_id=actor.id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details=details or {},
        trigger_message_id=trigger_message_id,
        llm_call_id=llm_call_id,
    )
    session.add(entry)
    session.flush()
    return entry


def next_error_ref(session: Session) -> str:
    return f"ERR-{session.execute(text('SELECT nextval(\'error_ref_seq\')')).scalar_one()}"
