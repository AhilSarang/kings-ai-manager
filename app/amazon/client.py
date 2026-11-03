"""Amazon SP-API client (brief Section 4.3): tokens, retries, rate limits, async operations.

No code outside this module talks to Amazon over HTTP. Tests use FakeAmazonClient; nothing in the
test suite is allowed to reach the live API.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import httpx

from app.amazon.errors import (
    AmazonAuthError,
    AmazonError,
    AmazonOperationFailed,
    AmazonRetryable,
    problem_text,
)
from app.amazon.marketplaces import LWA_TOKEN_URL, REGION_ENDPOINTS
from app.config import get_settings

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 20.0
OPERATION_POLL_SECONDS = 2.0
REQUESTS_PER_SECOND = 5.0  # below the slowest SP-API limits we use; 429s are still retried
OPERATION_TIMEOUT_SECONDS = 180.0

# What a finished call reports back for the amazon_api_calls table (filled in by M2).
CallRecord = dict[str, Any]
Recorder = Callable[[CallRecord], None]


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class _Throttle:
    """Process-wide spacing between SP-API calls, so a bulk sync doesn't burst into Amazon's limits."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.interval <= 0:  # tests set this to 0; nothing else does
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            _sleep(delay)


API_THROTTLE = _Throttle(per_second=REQUESTS_PER_SECOND)


class AmazonClient(ABC):
    """Every SP-API call the agent can make. Implemented by the HTTP client and the fake."""

    @abstractmethod
    def get(self, path: str, *, params: dict | None = None, operation: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def post(self, path: str, *, json: dict | None = None, params: dict | None = None,
             operation: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def put(self, path: str, *, json: dict | None = None, params: dict | None = None,
            operation: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def patch(self, path: str, *, json: dict | None = None, params: dict | None = None,
              operation: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def delete(self, path: str, *, params: dict | None = None,
               operation: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def operation_status(self, operation_id: str) -> dict[str, Any]: ...

    def await_operation(self, operation_id: str, *, timeout: float = OPERATION_TIMEOUT_SECONDS,
                        poll: float = OPERATION_POLL_SECONDS) -> dict[str, Any]:
        """Fulfillment Inbound writes are async: poll until SUCCESS, or raise with the problems."""
        deadline = time.monotonic() + timeout
        while True:
            status = self.operation_status(operation_id)
            state = (status.get("operationStatus") or "").upper()
            if state == "SUCCESS":
                return status
            if state == "FAILED":
                raise AmazonOperationFailed(problem_text(status.get("operationProblems")), status=status)
            if time.monotonic() >= deadline:
                raise AmazonOperationFailed(
                    f"Amazon is still working on this after {int(timeout)}s (operation {operation_id}); "
                    "nothing was confirmed. Check the shipment in Seller Central before retrying.",
                    status=status, timed_out=True)
            _sleep(poll)


class HttpAmazonClient(AmazonClient):
    def __init__(self, *, client_id: str | None = None, client_secret: str | None = None,
                 refresh_token: str | None = None, region: str | None = None,
                 http: httpx.Client | None = None, recorder: Recorder | None = None):
        s = get_settings()
        self.client_id = client_id if client_id is not None else s.amazon_lwa_client_id
        self.client_secret = (client_secret if client_secret is not None
                              else s.amazon_lwa_client_secret.get_secret_value())
        self.refresh_token = (refresh_token if refresh_token is not None
                              else s.amazon_lwa_refresh_token.get_secret_value())
        self.base = REGION_ENDPOINTS[region or s.amazon_region]
        self.http = http or httpx.Client(timeout=httpx.Timeout(45.0, connect=10.0))
        self.recorder = recorder
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    # ---- auth ----
    def _access_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._expires - 60:
                return self._token
            if not (self.client_id and self.client_secret and self.refresh_token):
                raise AmazonAuthError("Amazon SP-API credentials are not configured "
                                      "(AMAZON_LWA_CLIENT_ID / _SECRET / _REFRESH_TOKEN)")
            try:
                r = self.http.post(LWA_TOKEN_URL, data={
                    "grant_type": "refresh_token", "refresh_token": self.refresh_token,
                    "client_id": self.client_id, "client_secret": self.client_secret,
                })
            except httpx.HTTPError as exc:
                raise AmazonRetryable(f"Login with Amazon unreachable: {exc}") from exc
            if r.status_code >= 500:
                raise AmazonRetryable(f"Login with Amazon error {r.status_code}")
            if r.status_code != 200:
                err = (r.json().get("error") if r.content else "") or r.status_code
                raise AmazonAuthError(f"Login with Amazon refused the refresh token ({err}). "
                                      "Re-authorise the app in the Solution Provider Portal.")
            data = r.json()
            self._token = data["access_token"]
            self._expires = time.time() + float(data.get("expires_in", 3600))
            return self._token

    # ---- requests ----
    def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None,
                operation: str | None = None) -> dict[str, Any]:
        operation = operation or f"{method} {path.split('?')[0]}"
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            token = self._access_token(force=attempt > 1 and isinstance(last, AmazonAuthError))
            API_THROTTLE.wait()
            started = time.monotonic()
            try:
                r = self.http.request(method, self.base + path, params=params, json=json,
                                      headers={"x-amz-access-token": token, "accept": "application/json"})
            except httpx.HTTPError as exc:
                last = AmazonRetryable(f"Amazon SP-API unreachable: {exc}")
                self._record(operation, params, None, None, started, str(exc))
                if attempt == MAX_ATTEMPTS:
                    raise last from exc
                _sleep(self._backoff(attempt, None))
                continue

            body = self._body(r)
            request_id = r.headers.get("x-amzn-RequestId") or r.headers.get("x-amzn-requestid")
            self._record(operation, params, r.status_code, request_id, started,
                         None if r.status_code < 300 else problem_text(body.get("errors")))
            if r.status_code < 300:
                return body
            err = self._error(r, body, operation)
            if isinstance(err, AmazonRetryable) and attempt < MAX_ATTEMPTS:
                last = err
                _sleep(self._backoff(attempt, r))
                continue
            raise err
        raise last or AmazonError(f"{operation} failed")

    @staticmethod
    def _body(r: httpx.Response) -> dict[str, Any]:
        if not r.content:
            return {}
        try:
            data = r.json()
        except ValueError:
            return {"raw": r.text[:500]}
        return data if isinstance(data, dict) else {"payload": data}

    @staticmethod
    def _backoff(attempt: int, r: httpx.Response | None) -> float:
        if r is not None:
            retry_after = r.headers.get("retry-after")
            if retry_after and retry_after.strip().isdigit():
                return min(float(retry_after), BACKOFF_CAP_SECONDS)
            limit = r.headers.get("x-amzn-RateLimit-Limit")  # requests per second Amazon allows
            try:
                if limit and float(limit) > 0:
                    return min(max(1.0 / float(limit), BACKOFF_BASE_SECONDS), BACKOFF_CAP_SECONDS)
            except ValueError:
                pass
        return min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_CAP_SECONDS)

    @staticmethod
    def _error(r: httpx.Response, body: dict, operation: str) -> AmazonError:
        detail = problem_text(body.get("errors")) or r.text[:200]
        msg = f"{operation}: {detail}" if detail else f"{operation}: HTTP {r.status_code}"
        if r.status_code in (401, 403):
            # SP-API uses 403 for both an expired token and a missing role; the message says which.
            if "unauthorized" in detail.lower() or "access to requested resource is denied" in detail.lower():
                return AmazonAuthError(f"{msg} — the app may be missing a role, or the authorisation "
                                       "was created before that role was added.", missing_role=True)
            return AmazonAuthError(msg)
        if r.status_code == 429:
            return AmazonRetryable(f"{msg} (rate limited)")
        if r.status_code >= 500:
            return AmazonRetryable(msg)
        return AmazonError(msg)

    def _record(self, operation: str, params: dict | None, status: int | None, request_id: str | None,
                started: float, error: str | None) -> None:
        record: CallRecord = {
            "operation": operation,
            "marketplace_id": (params or {}).get("marketplaceIds") or (params or {}).get("MarketplaceId"),
            "sku": (params or {}).get("sku"),
            "http_status": status,
            "request_id": request_id,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error_code": error[:200] if error else None,
        }
        log.info("amazon api call", extra={k: v for k, v in record.items() if v is not None})
        if self.recorder:
            try:
                self.recorder(record)
            except Exception:  # recording must never break a call
                log.warning("could not record amazon api call", exc_info=True)

    # ---- verbs ----
    def get(self, path, *, params=None, operation=None):
        return self.request("GET", path, params=params, operation=operation)

    def post(self, path, *, json=None, params=None, operation=None):
        return self.request("POST", path, params=params, json=json, operation=operation)

    def put(self, path, *, json=None, params=None, operation=None):
        return self.request("PUT", path, params=params, json=json, operation=operation)

    def patch(self, path, *, json=None, params=None, operation=None):
        return self.request("PATCH", path, params=params, json=json, operation=operation)

    def delete(self, path, *, params=None, operation=None):
        return self.request("DELETE", path, params=params, operation=operation)

    def operation_status(self, operation_id: str) -> dict[str, Any]:
        return self.get(f"/inbound/fba/2024-03-20/operations/{operation_id}",
                        operation="getInboundOperationStatus")
