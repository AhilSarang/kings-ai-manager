"""Code-side agent registry. Only the Supervisor is implemented in Phase 1."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.agents.base import BaseAgent
from app.services import agents as agents_svc

# Keyword hints used for tests/validation; the LLM does the real routing.
ROUTING_HINTS: list[tuple[str, re.Pattern]] = [
    ("ppc", re.compile(r"\b(ppc|acos|tacos|bids?|keywords?|campaigns?|ads?|advertis\w*)\b", re.I)),
    ("sourcing", re.compile(r"\b(suppliers?|sourcing|source|manufacturers?|factory|alibaba|moq)\b", re.I)),
    ("amazon", re.compile(r"\b(amazon|asin|listings?|fba|reimbursements?|account health|seller central|cases?)\b",
                          re.I)),
    ("email", re.compile(r"\b(inbox|emails?|reply to|follow[- ]?ups?)\b", re.I)),
    ("reporting", re.compile(r"\b(reports?|dashboard)\b", re.I)),
]


def suggest_agent_key(text: str) -> str:
    for key, pattern in ROUTING_HINTS:
        if pattern.search(text or ""):
            return key
    return "supervisor"


def get_agent(session: Session, key: str) -> BaseAgent | None:
    """Return an implemented, active agent, or None."""
    from app.agents.supervisor import SupervisorAgent

    classes = {"supervisor": SupervisorAgent}
    record = agents_svc.get_by_key(session, key)
    if record.status != "active" or key not in classes:
        return None
    return classes[key](permissions=record.permissions)
