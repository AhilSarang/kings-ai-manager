"""In-memory Ads API used by tests and local simulation (ADS_BACKEND=fake).

Responses come from queued fixtures, so tests never touch the live API. Class-level state, so every
instance in a test sees the same queue and the same recorded calls.
"""

from __future__ import annotations

import gzip
import json
import re
from typing import Any

from app.ppc.client import AdsClient
from app.ppc.errors import AdsError


class FakeAdsClient(AdsClient):
    calls: list[dict[str, Any]] = []          # every call made, in order
    queued: list[tuple[str, Any]] = []        # (path_pattern, response or Exception) matched in order
    downloads: dict[str, Any] = {}            # url -> rows (encoded as gzipped JSON on the way out)

    @classmethod
    def reset(cls) -> None:
        cls.calls, cls.queued, cls.downloads = [], [], {}

    # ---- test helpers ----
    @classmethod
    def expect(cls, path_pattern: str, response: Any) -> None:
        """Queue a response (dict, list or Exception) for the next call whose path matches."""
        cls.queued.append((path_pattern, response))

    @classmethod
    def expect_report(cls, rows: list[dict[str, Any]], *, report_id: str = "RPT-1") -> None:
        """The whole asynchronous report dance: request, one poll, and the download."""
        url = f"https://ads-reports.test/{report_id}.json.gz"
        cls.expect(r"/reporting/reports$", {"reportId": report_id, "status": "PENDING"})
        cls.expect(rf"/reporting/reports/{report_id}", {"reportId": report_id, "status": "COMPLETED",
                                                        "url": url})
        cls.downloads[url] = rows

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
              profile_id: str | None, operation: str | None, content_type: str | None = None,
              accept: str | None = None) -> Any:
        # The media type is recorded because Amazon's v3 endpoints reject the wrong one, so tests
        # need to be able to see the exact string that would have gone out.
        type(self).calls.append({"method": method, "path": path, "params": params or {}, "json": body,
                                 "profile_id": profile_id, "operation": operation,
                                 "content_type": content_type, "accept": accept})
        for i, (pattern, response) in enumerate(type(self).queued):
            if re.search(pattern, path):
                type(self).queued.pop(i)
                if isinstance(response, Exception):
                    raise response
                return response
        raise AdsError(f"FakeAdsClient has no queued response for {method} {path}. "
                       "Queue one with FakeAdsClient.expect(pattern, response).")

    def get(self, path, *, params=None, profile_id=None, accept=None, operation=None):
        return self._call("GET", path, params, None, profile_id, operation, accept=accept)

    def post(self, path, *, json=None, params=None, profile_id=None, content_type=None, accept=None,
             operation=None):
        return self._call("POST", path, params, json, profile_id, operation,
                          content_type=content_type, accept=accept)

    def put(self, path, *, json=None, profile_id=None, content_type=None, operation=None):
        return self._call("PUT", path, None, json, profile_id, operation, content_type=content_type)

    def delete(self, path, *, profile_id=None, operation=None):
        return self._call("DELETE", path, None, None, profile_id, operation)

    def download(self, url: str) -> bytes:
        type(self).calls.append({"method": "GET", "path": url, "params": {}, "json": None,
                                 "profile_id": None, "operation": "downloadReport",
                                 "content_type": None, "accept": None})
        if url not in type(self).downloads:
            raise AdsError(f"FakeAdsClient has nothing to download at {url}")
        return gzip.compress(json.dumps(type(self).downloads[url]).encode())
