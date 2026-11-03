"""Base class for all agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db.models import Task


@dataclass
class AgentResult:
    status: str  # "done" | "waiting_owner" | "blocked" | "failed"
    text: str
    summary: str = ""


class BaseAgent(ABC):
    key: str = "base"

    def __init__(self, permissions: dict | None = None):
        self.permissions = permissions or {}

    @abstractmethod
    def can_handle(self, task: Task) -> bool: ...

    @abstractmethod
    def run(self, session: Session, task: Task) -> AgentResult: ...
