"""Small shared helpers for services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


class ServiceError(Exception):
    """A business-rule violation. The message is safe to show to the owner."""


class NotFound(ServiceError):
    pass


@dataclass(frozen=True)
class Actor:
    type: str  # user | agent | system
    id: str | None = None

    @classmethod
    def user(cls, user_id) -> Actor:
        return cls("user", str(user_id))

    @classmethod
    def agent(cls, key: str) -> Actor:
        return cls("agent", key)


SYSTEM = Actor("system", "system")


def utcnow() -> datetime:
    return datetime.now(UTC)
