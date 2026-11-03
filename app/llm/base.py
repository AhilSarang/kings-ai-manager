"""LLM provider interface. No other code may depend on which backend is active."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMRequest:
    system: str
    prompt: str
    purpose: str
    json_schema: dict[str, Any] | None = None
    max_tokens: int = 16000


@dataclass
class LLMResponse:
    text: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    structured: Any = None  # already-parsed JSON if the backend returned it
    meta: dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    """Non-retryable provider failure (bad request, refusal, unparseable reply)."""


class LLMUnavailableError(Exception):
    """Backend temporarily unusable: usage limit, rate limit, overload, logged out.

    kind: "usage_limit" | "auth" | "overloaded" | "network"
    retry_after: seconds suggested by the backend, if known.
    """

    def __init__(self, message: str, kind: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class LLMProvider(ABC):
    backend: str = "base"
    model: str | None = None

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one completion. Raise LLMUnavailableError or LLMError on failure."""
