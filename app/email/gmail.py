"""Gmail API client (REST over httpx) with token refresh, retries and typed errors.

Only the calls the Email Agent needs. Scopes: gmail.readonly + gmail.compose.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from abc import ABC, abstractmethod
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, make_msgid
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose",
          # Phase 3: read listing images the owner shares, and write FBA labels back to a Drive folder
          "https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive.file"]
OUTBOUND_HEADER = "X-Kings-AI"
OUTBOUND_VALUE = "outbound"


class GmailError(Exception):
    """Non-retryable Gmail API problem."""


class GmailAuthError(GmailError):
    """Token revoked/expired or access denied: re-authorisation needed."""


class GmailRetryable(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class GmailHistoryGone(GmailError):
    """startHistoryId is too old (HTTP 404)."""


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def build_raw(*, from_address: str, to: list[str], cc: list[str], subject: str, body: str,
              in_reply_to: str | None = None, references: str | None = None,
              attachments: list[tuple[str, str, bytes]] | None = None) -> str:
    """RFC 2822 message, base64url-encoded, tagged so sync ignores it."""
    msg = MimeMessage()
    msg["From"] = formataddr(("", from_address)) if "<" not in from_address else from_address
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=from_address.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    msg[OUTBOUND_HEADER] = OUTBOUND_VALUE
    msg.set_content(body)
    for filename, mime, data in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
    return b64url_encode(msg.as_bytes())


class GmailClient(ABC):
    @abstractmethod
    def get_profile(self) -> dict[str, Any]: ...

    @abstractmethod
    def list_history(self, start_history_id: int, page_token: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def list_messages(self, query: str, page_token: str | None = None, max_results: int = 100) -> dict[str, Any]: ...

    @abstractmethod
    def get_message(self, message_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def get_attachment(self, message_id: str, attachment_id: str) -> bytes: ...

    @abstractmethod
    def watch(self, topic: str) -> dict[str, Any]: ...

    @abstractmethod
    def create_draft(self, raw: str, thread_id: str) -> str: ...

    @abstractmethod
    def update_draft(self, draft_id: str, raw: str, thread_id: str) -> str: ...

    @abstractmethod
    def delete_draft(self, draft_id: str) -> None: ...

    @abstractmethod
    def send(self, raw: str, thread_id: str) -> dict[str, Any]: ...


class _Throttle:
    """Process-wide spacing between Gmail calls; Google rate-limits bursts (e.g. the 30-day backfill)."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


API_THROTTLE = _Throttle(per_second=5)


class HttpGmailClient(GmailClient):
    def __init__(self, refresh_token: str, *, client_id: str | None = None, client_secret: str | None = None,
                 http: httpx.Client | None = None):
        s = get_settings()
        self.refresh_token = refresh_token
        self.client_id = client_id or s.google_oauth_client_id
        self.client_secret = client_secret or s.google_oauth_client_secret.get_secret_value()
        self.http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    # ---- auth ----
    def _access_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._expires - 60:
                return self._token
            if not (self.client_id and self.client_secret):
                raise GmailAuthError("Google OAuth client id/secret are not configured")
            try:
                r = self.http.post(TOKEN_URL, data={
                    "grant_type": "refresh_token", "refresh_token": self.refresh_token,
                    "client_id": self.client_id, "client_secret": self.client_secret,
                })
            except httpx.HTTPError as exc:
                raise GmailRetryable(f"Google token endpoint unreachable: {exc}") from exc
            if r.status_code >= 500:
                raise GmailRetryable(f"Google token endpoint error {r.status_code}")
            if r.status_code != 200:
                err = _json(r).get("error", "")
                raise GmailAuthError(f"Google refused the refresh token ({err or r.status_code})")
            data = r.json()
            self._token = data["access_token"]
            self._expires = time.time() + float(data.get("expires_in", 3600))
            return self._token

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        url = path if path.startswith("http") else f"{API}{path}"
        for attempt in (1, 2):
            token = self._access_token(force=attempt == 2)
            API_THROTTLE.wait()
            try:
                r = self.http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
            except httpx.HTTPError as exc:
                raise GmailRetryable(f"Gmail API unreachable: {exc}") from exc
            if r.status_code == 401 and attempt == 1:
                continue  # token may have just expired
            return self._check(r, path)
        raise AssertionError("unreachable")

    @staticmethod
    def _check(r: httpx.Response, path: str) -> httpx.Response:
        if r.status_code < 400:
            return r
        body = _json(r).get("error", {}) if r.content else {}
        reason = ""
        if isinstance(body, dict):
            errs = body.get("errors") or [{}]
            reason = errs[0].get("reason", "") or body.get("status", "")
        msg = f"Gmail API {r.status_code} on {path.split('?')[0]}: {reason or r.text[:120]}"
        if r.status_code == 401:
            raise GmailAuthError(msg)
        if r.status_code == 403 and reason not in ("rateLimitExceeded", "userRateLimitExceeded"):
            raise GmailAuthError(msg)
        if r.status_code == 404 and path.startswith("/history"):
            raise GmailHistoryGone(msg)
        if r.status_code in (403, 429) or r.status_code >= 500:
            retry = r.headers.get("retry-after")
            raise GmailRetryable(msg, float(retry) if retry and retry.isdigit() else None)
        raise GmailError(msg)

    # ---- API ----
    def get_profile(self) -> dict[str, Any]:
        return self._request("GET", "/profile").json()

    def list_history(self, start_history_id: int, page_token: str | None = None) -> dict[str, Any]:
        params = {"startHistoryId": str(start_history_id), "historyTypes": "messageAdded", "maxResults": "500"}
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", "/history", params=params).json()

    def list_messages(self, query: str, page_token: str | None = None, max_results: int = 100) -> dict[str, Any]:
        params = {"q": query, "maxResults": str(max_results), "includeSpamTrash": "false"}
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", "/messages", params=params).json()

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self._request("GET", f"/messages/{message_id}", params={"format": "full"}).json()

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        data = self._request("GET", f"/messages/{message_id}/attachments/{attachment_id}").json()
        return b64url_decode(data.get("data", ""))

    def watch(self, topic: str) -> dict[str, Any]:
        return self._request("POST", "/watch", json={
            "topicName": topic, "labelIds": ["INBOX"], "labelFilterBehavior": "include"}).json()

    def create_draft(self, raw: str, thread_id: str) -> str:
        return self._request("POST", "/drafts", json={"message": _msg(raw, thread_id)}).json()["id"]

    def update_draft(self, draft_id: str, raw: str, thread_id: str) -> str:
        return self._request("PUT", f"/drafts/{draft_id}", json={
            "id": draft_id, "message": _msg(raw, thread_id)}).json()["id"]

    def delete_draft(self, draft_id: str) -> None:
        try:
            self._request("DELETE", f"/drafts/{draft_id}")
        except GmailError as exc:
            if "404" not in str(exc):
                raise

    def send(self, raw: str, thread_id: str) -> dict[str, Any]:
        return self._request("POST", "/messages/send", json=_msg(raw, thread_id)).json()


def _msg(raw: str, thread_id: str | None) -> dict[str, str]:
    """A new conversation has no threadId; Gmail starts a new thread."""
    return {"raw": raw, "threadId": thread_id} if thread_id else {"raw": raw}


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
