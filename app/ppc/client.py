"""Amazon Ads API client (brief 4.3): tokens, profile scoping, retries, async reports.

No code outside this module talks to Amazon Ads over HTTP. Tests use FakeAdsClient; nothing in the
test suite is allowed to reach the live API.

Header names and the reporting flow were confirmed against the live documentation on 2026-09-24:
campaign management wants `Amazon-Advertising-API-ClientId`, reporting wants `Amazon-Ads-ClientId`
and an `Amazon-Ads-AccountId`, and both want the profile in `Amazon-Advertising-API-Scope`.
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time
from datetime import UTC, datetime
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import httpx

from app.config import get_settings
from app.ppc.errors import (AdsAuthError, AdsError, AdsReportFailed, AdsReportInFlight,
                            AdsRetryable, problem_text)
from app.ppc.regions import LWA_TOKEN_URL, endpoint

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
REQUESTS_PER_SECOND = 2.0          # Ads is stricter than SP-API; 429s are still retried
REPORT_POLL_SECONDS = 30.0
REPORT_POLL_MAX_SECONDS = 300.0    # repeated status calls are themselves throttled, so slow down
# Amazon's own FAQ says report generation "can take as long as three hours"; the 15-minute figure in
# the service-limits table is only a P99 target, and a loader that gives up at 30 minutes silently
# loses a day of data on a busy morning.
REPORT_TIMEOUT_SECONDS = 3 * 3600.0
# 425 means one is already generating. Waiting is the documented fix; these are the wait and the tries.
IN_FLIGHT_WAIT_SECONDS = 300.0
IN_FLIGHT_ATTEMPTS = 3
REPORT_PATH = "/reporting/reports"
REPORT_CONTENT_TYPE = "application/vnd.createasyncreportrequest.v3+json"

CallRecord = dict[str, Any]
Recorder = Callable[[CallRecord], None]


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _url_expired(expires_at: Any) -> bool:
    """True when Amazon's presigned link has already run out. An unreadable value counts as fine."""
    if not expires_at:
        return False
    try:
        moment = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment <= datetime.now(UTC)


class _Throttle:
    """Process-wide spacing between Ads calls, so a backfill doesn't burst into Amazon's limits."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.interval <= 0:          # tests set this to 0; nothing else does
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            _sleep(delay)


API_THROTTLE = _Throttle(per_second=REQUESTS_PER_SECOND)


class AdsClient(ABC):
    """Every Ads API call the agent can make. Implemented by the HTTP client and the fake."""

    @abstractmethod
    def get(self, path: str, *, params: dict | None = None, profile_id: str | None = None,
            accept: str | None = None, operation: str | None = None) -> Any: ...

    @abstractmethod
    def post(self, path: str, *, json: dict | None = None, params: dict | None = None,
             profile_id: str | None = None, content_type: str | None = None,
             accept: str | None = None, operation: str | None = None) -> Any: ...

    @abstractmethod
    def put(self, path: str, *, json: dict | None = None, profile_id: str | None = None,
            content_type: str | None = None, operation: str | None = None) -> Any: ...

    @abstractmethod
    def delete(self, path: str, *, profile_id: str | None = None,
               operation: str | None = None) -> Any: ...

    @abstractmethod
    def download(self, url: str) -> bytes: ...

    def request(self, method: str, path: str, *, params: dict | None = None,
                json: dict | None = None, profile_id: str | None = None,
                content_type: str | None = None, accept: str | None = None,
                operation: str | None = None) -> Any:
        """One verb-agnostic entry point, so a caller can be told which verb to use.

        The write layer builds `(verb, path, media)` from a table rather than branching, and the fake
        has to answer the same calls as the real client, so this lives on the interface.
        """
        verb = method.upper()
        if verb == "GET":
            return self.get(path, params=params, profile_id=profile_id, accept=accept,
                            operation=operation)
        if verb == "POST":
            return self.post(path, json=json, params=params, profile_id=profile_id,
                             content_type=content_type, accept=accept, operation=operation)
        if verb == "PUT":
            return self.put(path, json=json, profile_id=profile_id, content_type=content_type,
                            operation=operation)
        if verb == "DELETE":
            return self.delete(path, profile_id=profile_id, operation=operation)
        raise AdsError(f"{method} isn't a verb this client makes")

    # ---- reports: request, wait, download, parse ----
    def request_report(self, body: dict[str, Any], *, profile_id: str, account_id: str | None = None,
                       wait_for_in_flight: bool = True) -> str:
        """Ask for a report. A 425 means an identical one is already running, so wait for it."""
        for attempt in range(1, IN_FLIGHT_ATTEMPTS + 1):
            try:
                response = self.post(REPORT_PATH, json=body, profile_id=profile_id,
                                     content_type=REPORT_CONTENT_TYPE, operation="createAsyncReport")
            except AdsReportInFlight:
                if not wait_for_in_flight or attempt == IN_FLIGHT_ATTEMPTS:
                    raise
                log.info("an identical report is already generating; waiting %ss",
                         int(IN_FLIGHT_WAIT_SECONDS))
                _sleep(IN_FLIGHT_WAIT_SECONDS)
                continue
            report_id = (response or {}).get("reportId")
            if not report_id:
                raise AdsError(f"Amazon didn't accept that report request: {problem_text(response)}")
            return report_id
        raise AdsReportInFlight("An identical report has been generating for a while; try later.")

    def wait_for_report(self, report_id: str, *, profile_id: str,
                        timeout: float = REPORT_TIMEOUT_SECONDS,
                        poll: float = REPORT_POLL_SECONDS) -> dict[str, Any]:
        """Poll until the report is COMPLETED and hand back its record, including the download url."""
        deadline = time.monotonic() + timeout
        interval = poll
        while True:
            report = self.get(f"{REPORT_PATH}/{report_id}", profile_id=profile_id, operation="getAsyncReport")
            status = (report.get("status") or "").upper()
            if status in ("COMPLETED", "SUCCESS"):
                return report
            if status in ("FAILURE", "FAILED", "CANCELLED"):
                raise AdsReportFailed(
                    f"Amazon couldn't produce that report ({status.lower()}): "
                    f"{report.get('failureReason') or problem_text(report)}", status=report)
            if time.monotonic() >= deadline:
                raise AdsReportFailed(f"Report {report_id} is still generating after {int(timeout)}s; "
                                      "nothing was loaded.", status=report, timed_out=True)
            _sleep(interval)
            # Polling has its own rate limit, and a three-hour report doesn't need asking every 30s.
            interval = min(interval * 1.5, REPORT_POLL_MAX_SECONDS)

    def report_rows(self, report: dict[str, Any], *, profile_id: str | None = None) -> list[dict[str, Any]]:
        """A finished report's rows. Amazon serves GZIP_JSON from a presigned url that expires.

        The link is good for about an hour, which Amazon documents as a default that "may vary", so
        the expiry it sends is read rather than assumed and an expired link is asked for again.
        """
        if profile_id and report.get("reportId") and _url_expired(report.get("urlExpiresAt")):
            log.info("the report's download link had expired; asking Amazon for a fresh one")
            report = self.get(f"{REPORT_PATH}/{report['reportId']}", profile_id=profile_id,
                              operation="getAsyncReport")
        url = report.get("url") or report.get("location")
        if not url:
            raise AdsError("Amazon marked the report complete but gave no download link.")
        data = self.download(url)
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        try:
            rows = json.loads(data.decode("utf-8", errors="replace") or "[]")
        except ValueError as exc:
            raise AdsError("That report wasn't readable JSON.") from exc
        return rows if isinstance(rows, list) else (rows.get("rows") or [])

    def fetch_report(self, body: dict[str, Any], *, profile_id: str,
                     account_id: str | None = None) -> list[dict[str, Any]]:
        """Request, wait, download, parse — the whole asynchronous dance in one call."""
        report_id = self.request_report(body, profile_id=profile_id, account_id=account_id)
        return self.report_rows(self.wait_for_report(report_id, profile_id=profile_id),
                                profile_id=profile_id)


class HttpAdsClient(AdsClient):
    def __init__(self, *, client_id: str | None = None, client_secret: str | None = None,
                 refresh_token: str | None = None, region: str | None = None,
                 account_id: str | None = None, http: httpx.Client | None = None,
                 recorder: Recorder | None = None):
        s = get_settings()
        self.client_id = client_id if client_id is not None else s.ads_lwa_client_id
        self.client_secret = (client_secret if client_secret is not None
                              else s.ads_lwa_client_secret.get_secret_value())
        self.refresh_token = (refresh_token if refresh_token is not None
                              else s.ads_lwa_refresh_token.get_secret_value())
        self.account_id = account_id if account_id is not None else s.ads_account_id
        self.base = endpoint(region or s.ads_region)
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
        self.recorder = recorder
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    # ---- auth ----
    def _access_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._expires - 120:
                return self._token
            if not (self.client_id and self.client_secret and self.refresh_token):
                raise AdsAuthError("Amazon Ads credentials are not configured "
                                   "(ADS_LWA_CLIENT_ID / _SECRET / _REFRESH_TOKEN)")
            try:
                r = self.http.post(LWA_TOKEN_URL, data={
                    "grant_type": "refresh_token", "refresh_token": self.refresh_token,
                    "client_id": self.client_id, "client_secret": self.client_secret,
                })
            except httpx.HTTPError as exc:
                raise AdsRetryable(f"Login with Amazon unreachable: {exc}") from exc
            if r.status_code >= 500:
                raise AdsRetryable(f"Login with Amazon error {r.status_code}")
            if r.status_code != 200:
                err = (r.json().get("error") if r.content else "") or r.status_code
                raise AdsAuthError(f"Login with Amazon refused the Ads refresh token ({err}). "
                                   "Re-authorise the Ads application — note that refresh tokens "
                                   "issued after 30 July 2026 expire 365 days after consent.")
            data = r.json()
            self._token = data["access_token"]
            self._expires = time.time() + float(data.get("expires_in", 3600))
            return self._token

    def _headers(self, token: str, *, profile_id: str | None, content_type: str | None,
                 accept: str | None, reporting: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Amazon-Advertising-API-ClientId": self.client_id,
            "Amazon-Ads-ClientId": self.client_id,      # reporting v3 uses this spelling
        }
        if profile_id:
            headers["Amazon-Advertising-API-Scope"] = str(profile_id)
        if reporting and self.account_id:
            headers["Amazon-Ads-AccountId"] = self.account_id
        headers["Accept"] = accept or content_type or "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    # ---- requests ----
    def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None,
                profile_id: str | None = None, content_type: str | None = None,
                accept: str | None = None, operation: str | None = None) -> Any:
        operation = operation or f"{method} {path.split('?')[0]}"
        reporting = path.startswith(REPORT_PATH)
        last: Exception | None = None
        stale_token = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            token = self._access_token(force=stale_token)
            API_THROTTLE.wait()
            started = time.monotonic()
            try:
                r = self.http.request(method, self.base + path, params=params, json=json,
                                      headers=self._headers(token, profile_id=profile_id,
                                                            content_type=content_type, accept=accept,
                                                            reporting=reporting))
            except httpx.HTTPError as exc:
                last = AdsRetryable(f"Amazon Ads unreachable: {exc}")
                self._record(operation, profile_id, None, None, started, str(exc))
                if attempt == MAX_ATTEMPTS:
                    raise last from exc
                _sleep(self._backoff(attempt, None))
                continue

            body = self._body(r)
            request_id = (r.headers.get("x-amzn-RequestId") or r.headers.get("x-amz-rid")
                          or r.headers.get("x-amzn-requestid"))
            self._record(operation, profile_id, r.status_code, request_id, started,
                         None if r.status_code < 300 else problem_text(body))
            if r.status_code < 300:
                return body
            err = self._error(r, body, operation)
            # A 401 can simply be a token that expired mid-flight. Fetch a fresh one and try once
            # more; anything else about credentials is a real problem and is raised.
            if r.status_code == 401 and not stale_token and attempt < MAX_ATTEMPTS:
                log.info("Amazon Ads refused the access token; fetching a new one and retrying")
                stale_token, last = True, err
                continue
            if isinstance(err, AdsRetryable) and attempt < MAX_ATTEMPTS:
                last = err
                _sleep(self._backoff(attempt, r))
                continue
            raise err
        raise last or AdsError(f"{operation} failed")

    @staticmethod
    def _body(r: httpx.Response) -> Any:
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text[:500]}

    @staticmethod
    def _backoff(attempt: int, r: httpx.Response | None) -> float:
        if r is not None:
            retry_after = (r.headers.get("retry-after") or "").strip()
            if retry_after.isdigit():
                return min(float(retry_after), BACKOFF_CAP_SECONDS)
        return min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_CAP_SECONDS)

    @staticmethod
    def _error(r: httpx.Response, body: Any, operation: str) -> AdsError:
        detail = problem_text(body) or r.text[:200]
        msg = f"{operation}: {detail}" if detail else f"{operation}: HTTP {r.status_code}"
        if r.status_code == 401:
            return AdsAuthError(f"{msg} — the access token or client id was rejected.")
        if r.status_code == 403:
            # Ads uses 403 for a profile the application may not touch, and for a missing scope.
            return AdsAuthError(f"{msg} — this application isn't authorised for that profile or "
                                "scope. Re-authorise the Ads application for this account.",
                                missing_scope=True)
        if r.status_code == 425:
            # Not a throttle: Amazon is saying an identical report is already being generated.
            return AdsReportInFlight(msg)
        if r.status_code == 429:
            return AdsRetryable(f"{msg} (rate limited)")
        if r.status_code >= 500:
            return AdsRetryable(msg)
        return AdsError(msg)

    def _record(self, operation: str, profile_id: str | None, status: int | None,
                request_id: str | None, started: float, error: str | None) -> None:
        record: CallRecord = {
            "operation": operation,
            "profile_id": profile_id,
            "http_status": status,
            "request_id": request_id,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error_code": error[:200] if error else None,
        }
        log.info("ads api call", extra={k: v for k, v in record.items() if v is not None})
        if self.recorder:
            try:
                self.recorder(record)
            except Exception:  # recording must never break a call
                log.warning("could not record ads api call", exc_info=True)

    # ---- verbs ----
    def get(self, path, *, params=None, profile_id=None, accept=None, operation=None):
        return self.request("GET", path, params=params, profile_id=profile_id, accept=accept,
                            operation=operation)

    def post(self, path, *, json=None, params=None, profile_id=None, content_type=None, accept=None,
             operation=None):
        return self.request("POST", path, params=params, json=json, profile_id=profile_id,
                            content_type=content_type, accept=accept, operation=operation)

    def put(self, path, *, json=None, profile_id=None, content_type=None, operation=None):
        return self.request("PUT", path, json=json, profile_id=profile_id, content_type=content_type,
                            operation=operation)

    def delete(self, path, *, profile_id=None, operation=None):
        return self.request("DELETE", path, profile_id=profile_id, operation=operation)

    def download(self, url: str) -> bytes:
        """Report downloads go to a presigned url, so they carry no Ads headers."""
        try:
            r = self.http.get(url, timeout=300, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise AdsRetryable(f"Could not download the report: {exc}") from exc
        if r.status_code != 200:
            raise AdsRetryable(f"Could not download the report ({r.status_code}).")
        return r.content
