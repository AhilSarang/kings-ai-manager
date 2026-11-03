import json
import os
import stat
import types
from datetime import timedelta

import anthropic
import httpx2
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.actions.schemas import SupervisorOutput
from app.config import get_settings
from app.db.models import Job, LLMCall
from app.jobs import queue
from app.jobs.errors import LLMUnavailable
from app.jobs.handlers import handler
from app.jobs.worker import Worker
from app.llm import gate, service
from app.llm.anthropic_api import AnthropicAPIProvider
from app.llm.base import LLMError, LLMRequest, LLMUnavailableError
from app.llm.fake import FakeLLMProvider
from app.llm.schema import strict_schema
from app.services.common import utcnow


class Reply(BaseModel):
    answer: str
    score: int | None = None


class Nested(BaseModel):
    items: list[Reply]


def _complete(**kw):
    return service.complete_json(Reply, system="sys", prompt="hi", purpose="test", **kw)


# ---- schema / parsing ----
def test_strict_schema_closes_all_objects():
    s = strict_schema(Nested)
    assert s["additionalProperties"] is False and s["required"] == ["items"]
    reply = s["$defs"]["Reply"]
    assert reply["additionalProperties"] is False
    assert reply["required"] == ["answer", "score"]
    assert "default" not in json.dumps(s)


def test_strict_schema_keeps_fields_named_like_dropped_keywords():
    class Keywordy(BaseModel):
        title: str
        default: int | None = None
        pattern: str | None = None

    s = strict_schema(Keywordy)
    assert s["required"] == ["title", "default", "pattern"]
    assert "title" not in s  # the model's own title annotation is still dropped
    assert "default" not in s["properties"]["default"]


@pytest.mark.parametrize("name", ["CreateTaskParams", "SubtaskSpec", "RememberParams"])
def test_supervisor_schema_allows_title_fields(name):
    params = strict_schema(SupervisorOutput)["$defs"][name]
    assert "title" in params["properties"] and "title" in params["required"]


@pytest.mark.parametrize("text", [
    '{"answer": "x"}',
    '```json\n{"answer": "x"}\n```',
    'Sure! Here you go: {"answer": "x"} hope that helps',
])
def test_extract_json(text):
    assert service.extract_json(text) == {"answer": "x"}


# ---- service ----
def test_complete_json_valid_and_logged(db):
    FakeLLMProvider.script({"answer": "yes", "score": 3})
    result, call_id = _complete()
    assert result == Reply(answer="yes", score=3)
    call = db.get(LLMCall, call_id)
    assert call.status == "ok" and call.backend == "fake" and call.purpose == "test"
    assert call.prompt_hash and len(call.prompt_hash) == 64


def test_invalid_output_retried_once_with_error(db):
    FakeLLMProvider.script("not json at all", {"answer": "fixed"})
    result, _ = _complete()
    assert result.answer == "fixed"
    retry_prompt = FakeLLMProvider.requests[1].prompt
    assert "not valid" in retry_prompt and "Error:" in retry_prompt
    statuses = [c.status for c in db.execute(select(LLMCall).order_by(LLMCall.created_at)).scalars()]
    assert statuses == ["invalid_output", "ok"]


def test_invalid_output_twice_raises(db):
    FakeLLMProvider.script({"wrong": 1}, {"still": "wrong"})
    with pytest.raises(service.LLMInvalidOutput):
        _complete()
    assert len(FakeLLMProvider.requests) == 2


def test_provider_error_is_logged_and_raised(db):
    FakeLLMProvider.script(LLMError("refused"))
    with pytest.raises(LLMError):
        _complete()
    assert db.execute(select(LLMCall.status)).scalar_one() == "error"


def _notice_jobs(db):
    return db.execute(select(Job).where(Job.type == "send_owner_message")).scalars().all()


def test_usage_limit_pauses_and_notifies_once(db):
    FakeLLMProvider.script(LLMUnavailableError("limit", "usage_limit"),
                           LLMUnavailableError("limit", "usage_limit"))
    with pytest.raises(LLMUnavailable) as first:
        _complete()
    db.expire_all()
    assert gate.is_llm_paused(db)
    assert first.value.retry_at > utcnow() + timedelta(minutes=4)
    with pytest.raises(LLMUnavailable) as second:
        _complete()
    assert second.value.retry_at - utcnow() > timedelta(minutes=9)  # backoff grew
    notices = _notice_jobs(db)
    assert len(notices) == 1
    assert "paused" in notices[0].payload["text"] and notices[0].payload["urgent"] is True


def test_retry_after_from_backend_is_used(db):
    FakeLLMProvider.script(LLMUnavailableError("limit", "usage_limit", retry_after=600))
    with pytest.raises(LLMUnavailable) as exc:
        _complete()
    delta = exc.value.retry_at - utcnow()
    assert timedelta(minutes=9) < delta <= timedelta(minutes=10)


def test_success_clears_pause_and_allows_new_notice(db):
    FakeLLMProvider.script(LLMUnavailableError("x", "auth"), {"answer": "ok"},
                           LLMUnavailableError("x", "auth"))
    with pytest.raises(LLMUnavailable):
        _complete()
    assert "credentials" in _notice_jobs(db)[0].payload["text"]
    _complete()
    db.expire_all()
    assert gate.paused_until(db) is None
    with pytest.raises(LLMUnavailable):
        _complete()
    assert len(_notice_jobs(db)) == 2


@handler("test_llm_job")
def _llm_job(session, payload, job):
    result, _ = service.complete_json(Reply, system="s", prompt=payload["q"], purpose="test")
    payload_out = dict(job.payload, answer=result.answer)
    job.payload = payload_out


def test_parked_job_resumes_after_limit_clears(db):
    """Acceptance 12: one notice, queued work runs after the limit clears."""
    FakeLLMProvider.script(LLMUnavailableError("limit", "usage_limit"), {"answer": "done"})
    job = queue.enqueue(db, "test_llm_job", {"q": "hello"}, uses_llm=True)
    queue.enqueue(db, "test_llm_job", {"q": "second"}, uses_llm=True)
    db.commit()
    w = Worker()
    assert w.run_one(uses_llm=True) is True
    db.expire_all()
    assert db.get(Job, job.id).status == "waiting_llm"
    assert w.run_one(uses_llm=True) is False  # gate closed: second message waits too
    # time passes
    db.expire_all()
    gate.pause(db, utcnow() - timedelta(seconds=1), "usage_limit")
    assert queue.wake_waiting_llm(db, now=utcnow() + timedelta(hours=1)) == 1
    db.commit()
    FakeLLMProvider.script({"answer": "done2"})
    assert w.run_one(uses_llm=True) and w.run_one(uses_llm=True)
    db.expire_all()
    assert queue.counts(db).get("done") == 2
    assert db.get(Job, job.id).payload["answer"] in {"done", "done2"}
    assert len(_notice_jobs(db)) == 1


# ---- backend selection ----
@pytest.mark.parametrize("backend,cls", [("fake", FakeLLMProvider)])
def test_backend_selected_by_env(monkeypatch, backend, cls):
    monkeypatch.setenv("LLM_BACKEND", backend)
    get_settings.cache_clear()
    try:
        assert isinstance(service.get_provider(), cls)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_anthropic_backend_selected_by_env(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "anthropic_api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    get_settings.cache_clear()
    service._provider_for.cache_clear()
    try:
        p = service.get_provider()
        assert isinstance(p, AnthropicAPIProvider) and p.model == "claude-opus-5"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        service._provider_for.cache_clear()


def test_anthropic_backend_requires_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
            AnthropicAPIProvider()
    finally:
        get_settings.cache_clear()


# ---- provider request fixture ----
REQ = LLMRequest(system="You are X", prompt="hello", purpose="t", json_schema={"type": "object"})


# ---- Anthropic API backend ----
class _FakeMessages:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def create(self, **params):
        self.calls.append(params)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _client(result):
    msgs = _FakeMessages(result)
    return types.SimpleNamespace(beta=types.SimpleNamespace(messages=msgs)), msgs


def _message(text, stop_reason="end_turn"):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="thinking", thinking=""), types.SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason, model="claude-opus-5",
        usage=types.SimpleNamespace(input_tokens=100, output_tokens=20),
    )


def _status_error(cls, code, headers=None):
    resp = httpx2.Response(code, headers=headers or {}, request=httpx2.Request("POST", "https://api"))
    return cls("err", response=resp, body=None)


def test_api_request_shape_and_parse():
    client, msgs = _client(_message('{"answer": "api"}'))
    r = AnthropicAPIProvider(client=client).complete(REQ)
    assert r.structured == {"answer": "api"} and r.input_tokens == 100
    p = msgs.calls[0]
    assert p["model"] == "claude-opus-5" and p["system"] == "You are X"
    assert p["messages"] == [{"role": "user", "content": "hello"}]
    assert p["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert p["fallbacks"] == "default" and p["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in p and "temperature" not in p


@pytest.mark.parametrize("exc,kind,retry", [
    (_status_error(anthropic.RateLimitError, 429, {"retry-after": "42"}), "usage_limit", 42.0),
    (_status_error(anthropic.AuthenticationError, 401), "auth", None),
    (_status_error(anthropic.InternalServerError, 529), "overloaded", None),
    (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api")), "network", None),
])
def test_api_unavailable_errors(exc, kind, retry):
    client, _ = _client(exc)
    with pytest.raises(LLMUnavailableError) as err:
        AnthropicAPIProvider(client=client).complete(REQ)
    assert err.value.kind == kind and err.value.retry_after == retry


def test_api_bad_request_and_refusal_are_permanent():
    client, _ = _client(_status_error(anthropic.BadRequestError, 400))
    with pytest.raises(LLMError):
        AnthropicAPIProvider(client=client).complete(REQ)
    client, _ = _client(_message("", stop_reason="refusal"))
    with pytest.raises(LLMError, match="declined"):
        AnthropicAPIProvider(client=client).complete(REQ)


def test_no_module_depends_on_active_backend():
    """Only the LLM package may import concrete backends."""
    import pathlib

    root = pathlib.Path("app")
    offenders = [
        str(p) for p in root.rglob("*.py")
        if "llm" not in p.parts
        and any(m in p.read_text() for m in ("anthropic_api", "llm.fake", "FakeLLMProvider"))
        and p.name != "config.py"
    ]
    assert offenders == []
    assert os.environ["LLM_BACKEND"] == "fake"
