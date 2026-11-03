"""In-memory SP-API used by tests and local simulation (AMAZON_BACKEND=fake).

Responses come from queued fixtures, so tests never touch the live API. Class-level state, so every
instance in a test sees the same queue and the same recorded calls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.amazon.client import AmazonClient
from app.amazon.errors import AmazonError

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "amazon" / "fixtures"


class FakeAmazonClient(AmazonClient):
    calls: list[dict[str, Any]] = []          # every call made, in order
    queued: list[tuple[str, Any]] = []        # (path_pattern, response or Exception) matched in order
    operations: dict[str, list[dict]] = {}    # operation_id -> statuses returned in turn
    default_operation_status = "SUCCESS"

    @classmethod
    def reset(cls) -> None:
        cls.calls, cls.queued, cls.operations = [], [], {}
        cls.default_operation_status = "SUCCESS"

    # ---- test helpers ----
    @classmethod
    def expect(cls, path_pattern: str, response: Any) -> None:
        """Queue a response (dict or Exception) for the next call whose path matches the pattern."""
        cls.queued.append((path_pattern, response))

    @classmethod
    def expect_many(cls, pairs: list[tuple[str, Any]]) -> None:
        for path, response in pairs:
            cls.expect(path, response)

    @classmethod
    def fixture(cls, name: str) -> dict[str, Any]:
        return json.loads((FIXTURES / f"{name}.json").read_text())

    @classmethod
    def operation(cls, operation_id: str, statuses: list[dict]) -> None:
        """Script what getInboundOperationStatus returns, one entry per poll."""
        cls.operations[operation_id] = list(statuses)

    @classmethod
    def calls_to(cls, pattern: str) -> list[dict[str, Any]]:
        return [c for c in cls.calls if re.search(pattern, c["path"])]

    @classmethod
    def last_body(cls, pattern: str) -> dict[str, Any]:
        matching = cls.calls_to(pattern)
        if not matching:
            raise AssertionError(f"no call matching {pattern!r}; calls were: {[c['path'] for c in cls.calls]}")
        return matching[-1]["json"] or {}

    # ---- client ----
    def _call(self, method: str, path: str, params: dict | None, body: dict | None,
              operation: str | None) -> dict[str, Any]:
        type(self).calls.append({"method": method, "path": path, "params": params or {}, "json": body,
                                 "operation": operation})
        for i, (pattern, response) in enumerate(type(self).queued):
            if re.search(pattern, path):
                type(self).queued.pop(i)
                if isinstance(response, Exception):
                    raise response
                return response
        raise AmazonError(f"FakeAmazonClient has no queued response for {method} {path}. "
                          "Queue one with FakeAmazonClient.expect(pattern, response).")

    def get(self, path, *, params=None, operation=None):
        return self._call("GET", path, params, None, operation)

    def post(self, path, *, json=None, params=None, operation=None):
        return self._call("POST", path, params, json, operation)

    def put(self, path, *, json=None, params=None, operation=None):
        return self._call("PUT", path, params, json, operation)

    def patch(self, path, *, json=None, params=None, operation=None):
        return self._call("PATCH", path, params, json, operation)

    def delete(self, path, *, params=None, operation=None):
        return self._call("DELETE", path, params, None, operation)

    def operation_status(self, operation_id: str) -> dict[str, Any]:
        type(self).calls.append({"method": "GET", "path": f"/operations/{operation_id}", "params": {},
                                 "json": None, "operation": "getInboundOperationStatus"})
        scripted = type(self).operations.get(operation_id)
        if scripted:
            return scripted.pop(0) if len(scripted) > 1 else scripted[0]
        return {"operationId": operation_id, "operationStatus": type(self).default_operation_status}
