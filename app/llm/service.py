"""The only entry point other code uses to call an LLM.

- picks the backend from LLM_BACKEND
- validates JSON output with Pydantic, retrying once with the error
- logs every call to `llm_calls` (never the full prompt)
- on usage limits/auth failures: pauses the LLM lane, notifies the owner
  once, and raises jobs.errors.LLMUnavailable so the job is parked, not lost
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from datetime import timedelta
from functools import lru_cache
from typing import TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.db.models import LLMCall
from app.db.session import session_scope
from app.jobs.errors import LLMUnavailable
from app.llm import gate
from app.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse, LLMUnavailableError
from app.llm.schema import strict_schema
from app.services import health, settings_store
from app.services.common import SYSTEM, utcnow
from app.services.notify import notify_owner

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

PAUSE_NOTICE_KEY = "llm_pause_notice"
PAUSE_COUNT_KEY = "llm_pause_count"
MIN_PAUSE = timedelta(minutes=5)
MAX_PAUSE = timedelta(minutes=60)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


class LLMInvalidOutput(LLMError):
    pass


@lru_cache(maxsize=4)
def _provider_for(backend: str) -> LLMProvider:
    if backend == "anthropic_api":
        from app.llm.anthropic_api import AnthropicAPIProvider

        return AnthropicAPIProvider()
    if backend == "fake":
        from app.llm.fake import FakeLLMProvider

        return FakeLLMProvider()
    raise LLMError(f"Unknown LLM_BACKEND '{backend}'")


def get_provider() -> LLMProvider:
    return _provider_for(get_settings().llm_backend)


_semaphore_lock = threading.Lock()
_semaphore: threading.BoundedSemaphore | None = None


def _slot() -> threading.BoundedSemaphore:
    global _semaphore
    with _semaphore_lock:
        if _semaphore is None:
            _semaphore = threading.BoundedSemaphore(get_settings().llm_concurrency)
        return _semaphore


def extract_json(text: str):
    cleaned = _FENCE_RE.sub("", (text or "").strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def complete_json(
    model_cls: type[T], *, system: str, prompt: str, purpose: str, max_tokens: int = 16000,
) -> tuple[T, uuid.UUID]:
    """Return a validated `model_cls` instance and the id of the successful llm_calls row."""
    schema = strict_schema(model_cls)
    request = LLMRequest(system=system, prompt=prompt, purpose=purpose, json_schema=schema,
                         max_tokens=max_tokens)
    first_error: str | None = None
    for attempt in (1, 2):
        response, call_id = _call(request)
        try:
            data = response.structured if response.structured is not None else extract_json(response.text)
            return model_cls.model_validate(data), call_id
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            first_error = _short_error(exc)
            _mark_invalid(call_id, first_error)
            log.warning("invalid LLM output", extra={"purpose": purpose, "attempt": attempt, "error": first_error})
            if attempt == 1:
                request = LLMRequest(
                    system=system, purpose=purpose, json_schema=schema, max_tokens=max_tokens,
                    prompt=(f"{prompt}\n\n---\nYour previous reply was not valid for the required JSON "
                            f"format. Error: {first_error}\nReply again with only the corrected JSON object."),
                )
    raise LLMInvalidOutput(f"LLM output invalid after retry: {first_error}")


def _call(request: LLMRequest) -> tuple[LLMResponse, uuid.UUID]:
    provider = get_provider()
    prompt_hash = hashlib.sha256(f"{request.system}\n{request.prompt}".encode()).hexdigest()
    started = time.monotonic()
    status, error, response = "ok", None, None
    try:
        with _slot():
            response = provider.complete(request)
    except LLMUnavailableError as exc:
        status, error = f"unavailable:{exc.kind}", str(exc)
        call_id = _log(provider, request, prompt_hash, started, status, error, None)
        _handle_unavailable(exc)
        raise AssertionError("unreachable")  # _handle_unavailable always raises
    except LLMError as exc:
        status, error = "error", str(exc)
        _log(provider, request, prompt_hash, started, status, error, None)
        raise
    call_id = _log(provider, request, prompt_hash, started, status, error, response)
    _on_success()
    return response, call_id


def _log(provider, request, prompt_hash, started, status, error, response) -> uuid.UUID:
    call_id = uuid.uuid4()
    with session_scope() as s:
        s.add(LLMCall(
            id=call_id, backend=provider.backend, model=(response.model if response else provider.model),
            purpose=request.purpose, status=status, error=(error or "")[:2000] or None,
            input_tokens=response.input_tokens if response else None,
            output_tokens=response.output_tokens if response else None,
            duration_ms=int((time.monotonic() - started) * 1000), prompt_hash=prompt_hash,
        ))
    return call_id


def _mark_invalid(call_id: uuid.UUID, error: str) -> None:
    with session_scope() as s:
        call = s.get(LLMCall, call_id)
        if call:
            call.status, call.error = "invalid_output", error[:2000]


def _handle_unavailable(exc: LLMUnavailableError) -> None:
    with session_scope() as s:
        count = int(settings_store.get(s, PAUSE_COUNT_KEY) or 0)
        if exc.retry_after:
            delay = timedelta(seconds=max(exc.retry_after, 30))
        else:
            delay = min(MIN_PAUSE * (2 ** count), MAX_PAUSE)
        retry_at = utcnow() + delay
        gate.pause(s, retry_at, exc.kind)
        settings_store.set_value(s, PAUSE_COUNT_KEY, count + 1, SYSTEM)
        local = retry_at.astimezone(ZoneInfo(get_settings().timezone)).strftime("%-I:%M %p")
        reason = {
            "auth": "the LLM provider credentials need attention",
            "usage_limit": "the LLM usage limit was reached",
        }.get(exc.kind, "the LLM service is unavailable")
        notify_owner(
            s,
            f"⏸️ AI layer paused: {reason}. Your messages are saved and will be handled "
            f"automatically. Next retry around {local} ET. Commands like STATUS and TASKS still work.",
            key=PAUSE_NOTICE_KEY, urgent=True, respect_quiet_hours=False,
        )
    log.warning("LLM paused", extra={"kind": exc.kind, "retry_at": retry_at.isoformat()})
    raise LLMUnavailable(str(exc), retry_at=retry_at, kind=exc.kind)


def _on_success() -> None:
    with session_scope() as s:
        if gate.paused_until(s) is None and not settings_store.get(s, PAUSE_COUNT_KEY):
            return
        gate.clear(s)
        settings_store.set_value(s, PAUSE_COUNT_KEY, 0, SYSTEM)
        health.release_notification(s, PAUSE_NOTICE_KEY)
        log.info("LLM available again")


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]]
        return "; ".join(parts)
    return f"{type(exc).__name__}: {exc}"[:300]
