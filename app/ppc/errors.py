"""What the Amazon Ads API can go wrong with, in words the owner can read."""

from __future__ import annotations

from typing import Any


class AdsError(Exception):
    """Something Amazon refused, and retrying it unchanged won't help."""


class AdsAuthError(AdsError):
    """Credentials, consent or profile scope. `missing_scope` means this one call isn't permitted."""

    def __init__(self, message: str, *, missing_scope: bool = False):
        super().__init__(message)
        self.missing_scope = missing_scope


class AdsRetryable(AdsError):
    """Rate limit, 5xx or a network problem: the same call may work in a moment."""


class AdsReportInFlight(AdsError):
    """425: an identical report is already generating.

    Amazon de-duplicates report requests by their parameters, so retrying the create immediately
    just earns another 425. The documented remedy is to wait for the one already running.
    """


class AdsReportFailed(AdsError):
    """An asynchronous report finished as FAILURE, or never finished."""

    def __init__(self, message: str, *, status: dict[str, Any] | None = None, timed_out: bool = False):
        super().__init__(message)
        self.status = status or {}
        self.timed_out = timed_out


def problem_text(problems: Any) -> str:
    """Amazon returns errors in several shapes; turn any of them into one readable line."""
    if not problems:
        return ""
    if isinstance(problems, str):
        return problems[:500]
    if isinstance(problems, dict):
        for key in ("details", "message", "detail", "description"):
            if problems.get(key):
                return str(problems[key])[:500]
        problems = problems.get("errors") or problems.get("validationErrors") or []
    parts = []
    for problem in problems if isinstance(problems, list) else [problems]:
        if isinstance(problem, dict):
            message = problem.get("message") or problem.get("details") or problem.get("errorType") or ""
            field = problem.get("field") or problem.get("path") or ""
            parts.append(f"{message} ({field})" if field else str(message))
        else:
            parts.append(str(problem))
    return "; ".join(p for p in parts if p)[:500]
