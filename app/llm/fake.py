"""Deterministic provider for tests and local simulation.

Queue replies with `FakeLLMProvider.script(...)`. Each item may be:
- a dict / list  -> returned as JSON text
- a str          -> returned verbatim
- an Exception   -> raised
- a callable(request) -> its return value is treated as above
With nothing scripted, returns a polite no_op reply.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from app.llm.base import LLMProvider, LLMRequest, LLMResponse

DEFAULT_REPLY = {"actions": [{"type": "no_op", "params": {}}],
                 "reply_to_owner": "Noted. (fake LLM backend)"}


class FakeLLMProvider(LLMProvider):
    backend = "fake"
    model = "fake-model"
    _queue: deque = deque()
    requests: list[LLMRequest] = []

    @classmethod
    def script(cls, *items: Any) -> None:
        cls._queue.extend(items)

    @classmethod
    def reset(cls) -> None:
        cls._queue.clear()
        cls.requests.clear()

    def complete(self, request: LLMRequest) -> LLMResponse:
        type(self).requests.append(request)
        item = type(self)._queue.popleft() if type(self)._queue else DEFAULT_REPLY
        if callable(item) and not isinstance(item, type):
            item = item(request)
        if isinstance(item, BaseException):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return LLMResponse(text=text, model=self.model, input_tokens=len(request.prompt) // 4,
                           output_tokens=len(text) // 4)
