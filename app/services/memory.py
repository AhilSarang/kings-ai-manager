"""Company memory: SOPs, rules, preferences. Separate from business data."""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import MEMORY_CATEGORIES, MemoryEntry
from app.services import audit
from app.services.common import Actor, NotFound, ServiceError

MAX_ACTIVE_IN_PROMPT = 60


def create(
    session: Session, *, category: str, title: str, content: str, actor: Actor,
    applies_to_agent_key: str | None = None, trigger_message_id: uuid.UUID | None = None,
) -> MemoryEntry:
    if category not in MEMORY_CATEGORIES:
        raise ServiceError(f"Unknown memory category '{category}'")
    by_user = actor.type == "user"
    entry = MemoryEntry(
        category=category, title=title.strip()[:200], content=content.strip(),
        applies_to_agent_key=applies_to_agent_key, created_by="user" if by_user else "agent",
        is_active=by_user,  # agent-proposed memory waits for the owner's confirmation
    )
    session.add(entry)
    session.flush()
    audit.record(session, actor, "memory_created", target_type="memory", target_id=entry.id,
                 details={"category": category, "title": entry.title, "active": entry.is_active},
                 trigger_message_id=trigger_message_id)
    return entry


def confirm(session: Session, entry_id: uuid.UUID, actor: Actor) -> MemoryEntry:
    entry = session.get(MemoryEntry, entry_id)
    if entry is None:
        raise NotFound("Memory entry not found")
    entry.is_active = True
    audit.record(session, actor, "memory_confirmed", target_type="memory", target_id=entry.id)
    return entry


def active_for(session: Session, agent_key: str) -> list[MemoryEntry]:
    return list(session.execute(
        select(MemoryEntry)
        .where(MemoryEntry.is_active.is_(True),
               or_(MemoryEntry.applies_to_agent_key.is_(None), MemoryEntry.applies_to_agent_key == agent_key))
        .order_by(MemoryEntry.category, MemoryEntry.created_at)
        .limit(MAX_ACTIVE_IN_PROMPT)
    ).scalars())
