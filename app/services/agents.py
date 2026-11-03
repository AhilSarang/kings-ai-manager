"""Agent registry lookups (database side)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Agent
from app.services.common import NotFound

AGENT_KEYS = ("supervisor", "email", "amazon", "ppc", "sourcing", "reporting")


def get_by_key(session: Session, key: str) -> Agent:
    agent = session.execute(select(Agent).where(Agent.key == key)).scalar_one_or_none()
    if agent is None:
        raise NotFound(f"Unknown agent '{key}'")
    return agent


def list_all(session: Session) -> list[Agent]:
    return list(session.execute(select(Agent).order_by(Agent.key)).scalars())


def inactive_reason(agent: Agent) -> str:
    phase = (agent.config or {}).get("phase")
    suffix = f" (planned for Phase {phase})" if phase else ""
    return f"Agent not active yet{suffix}"
