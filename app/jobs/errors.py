"""Exceptions a job handler may raise to control retry behaviour."""

from __future__ import annotations

from datetime import datetime


class RetryableError(Exception):
    """Temporary failure: retry with backoff until max_attempts."""


class PermanentError(Exception):
    """Do not retry: mark the job failed immediately."""


class LLMUnavailable(Exception):
    """The LLM backend is rate-limited or logged out. Park the job as waiting_llm."""

    def __init__(self, message: str, retry_at: datetime, kind: str = "usage_limit"):
        super().__init__(message)
        self.retry_at = retry_at
        self.kind = kind
