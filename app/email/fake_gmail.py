"""In-memory Gmail used by tests and local simulation (GMAIL_BACKEND=fake)."""

from __future__ import annotations

import base64
import itertools
import time
import uuid
from email import message_from_bytes, policy
from email.message import EmailMessage as MimeMessage
from typing import Any

from app.email.gmail import GmailAuthError, GmailClient, GmailHistoryGone, b64url_decode


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class FakeGmailClient(GmailClient):
    """Class-level state so every client instance sees the same mailbox."""

    messages: dict[str, dict[str, Any]] = {}
    attachments: dict[tuple[str, str], bytes] = {}
    history: list[tuple[int, str]] = []  # (history_id, message_id)
    drafts: dict[str, dict[str, Any]] = {}
    sent: list[dict[str, Any]] = []
    watch_calls: list[str] = []
    history_id = 1000
    min_history_id = 0
    auth_broken = False
    address = "you@example.com"
    _seq = itertools.count(1)

    @classmethod
    def reset(cls) -> None:
        cls.messages, cls.attachments, cls.history = {}, {}, []
        cls.drafts, cls.sent, cls.watch_calls = {}, [], []
        cls.history_id, cls.min_history_id, cls.auth_broken = 1000, 0, False

    # ---- test helpers ----
    @classmethod
    def add_message(
        cls, *, from_: str = "Sam Supplier <sam@supplier.com>", to: str | None = None, subject: str = "Hello",
        body: str = "Hi there", html: str | None = None, thread_id: str | None = None,
        labels: list[str] | None = None, headers: dict[str, str] | None = None,
        attachments: list[tuple[str, str, bytes]] | None = None, age_seconds: int = 0,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        mid = message_id or f"m{next(cls._seq):05d}{uuid.uuid4().hex[:6]}"
        tid = thread_id or f"t{mid}"
        part_headers = {"From": from_, "To": to or cls.address, "Subject": subject,
                        "Date": time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(time.time() - age_seconds)),
                        "Message-ID": f"<{mid}@mail.example>", **(headers or {})}
        parts: list[dict[str, Any]] = [
            {"partId": "0.0", "mimeType": "text/plain", "headers": [],
             "body": {"size": len(body.encode()), "data": _b64(body.encode())}},
        ]
        if html is not None:
            parts.append({"partId": "0.1", "mimeType": "text/html", "headers": [],
                          "body": {"size": len(html.encode()), "data": _b64(html.encode())}})
        payload_parts: list[dict[str, Any]] = [{"partId": "0", "mimeType": "multipart/alternative",
                                                "headers": [], "body": {"size": 0}, "parts": parts}]
        for i, (fname, mime, data) in enumerate(attachments or [], start=1):
            att_id = f"att{i}{mid}"
            cls.attachments[(mid, att_id)] = data
            payload_parts.append({"partId": str(i), "mimeType": mime, "filename": fname,
                                  "headers": [{"name": "Content-Disposition", "value": f'attachment; filename="{fname}"'}],
                                  "body": {"attachmentId": att_id, "size": len(data)}})
        msg = {
            "id": mid, "threadId": tid, "labelIds": labels if labels is not None else ["INBOX", "CATEGORY_PERSONAL"],
            "snippet": body[:100], "internalDate": str(int((time.time() - age_seconds) * 1000)),
            "payload": {"mimeType": "multipart/mixed", "headers": [{"name": k, "value": v} for k, v in part_headers.items()],
                        "body": {"size": 0}, "parts": payload_parts},
        }
        cls.messages[mid] = msg
        cls.history_id += 1
        cls.history.append((cls.history_id, mid))
        return msg

    # ---- client API ----
    def _check_auth(self) -> None:
        if type(self).auth_broken:
            raise GmailAuthError("Google refused the refresh token (invalid_grant)")

    def get_profile(self) -> dict[str, Any]:
        self._check_auth()
        return {"emailAddress": type(self).address, "historyId": str(type(self).history_id)}

    def list_history(self, start_history_id: int, page_token: str | None = None) -> dict[str, Any]:
        self._check_auth()
        cls = type(self)
        if start_history_id < cls.min_history_id:
            raise GmailHistoryGone("Gmail API 404 on /history: notFound")
        added = [{"messagesAdded": [{"message": {"id": mid, "threadId": cls.messages[mid]["threadId"],
                                                  "labelIds": cls.messages[mid]["labelIds"]}}]}
                 for hid, mid in cls.history if hid > start_history_id]
        return {"history": added, "historyId": str(cls.history_id)}

    def list_messages(self, query: str, page_token: str | None = None, max_results: int = 100) -> dict[str, Any]:
        self._check_auth()
        cls = type(self)
        days = 30
        if "newer_than:" in query:
            days = int(query.split("newer_than:")[1].split("d")[0])
        cutoff = (time.time() - days * 86400) * 1000
        msgs = [{"id": m["id"], "threadId": m["threadId"]} for m in cls.messages.values()
                if int(m["internalDate"]) >= cutoff]
        return {"messages": msgs[:max_results], "resultSizeEstimate": len(msgs)}

    def get_message(self, message_id: str) -> dict[str, Any]:
        self._check_auth()
        return type(self).messages[message_id]

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        self._check_auth()
        return type(self).attachments[(message_id, attachment_id)]

    def watch(self, topic: str) -> dict[str, Any]:
        self._check_auth()
        type(self).watch_calls.append(topic)
        return {"historyId": str(type(self).history_id), "expiration": str(int((time.time() + 7 * 86400) * 1000))}

    def create_draft(self, raw: str, thread_id: str) -> str:
        self._check_auth()
        did = f"r{uuid.uuid4().hex[:10]}"
        type(self).drafts[did] = {"raw": raw, "threadId": thread_id, "parsed": parse_raw(raw)}
        return did

    def update_draft(self, draft_id: str, raw: str, thread_id: str) -> str:
        self._check_auth()
        type(self).drafts[draft_id] = {"raw": raw, "threadId": thread_id, "parsed": parse_raw(raw)}
        return draft_id

    def delete_draft(self, draft_id: str) -> None:
        self._check_auth()
        type(self).drafts.pop(draft_id, None)

    def send(self, raw: str, thread_id: str) -> dict[str, Any]:
        self._check_auth()
        mid = f"s{uuid.uuid4().hex[:12]}"
        type(self).sent.append({"id": mid, "raw": raw, "threadId": thread_id, "parsed": parse_raw(raw)})
        return {"id": mid, "threadId": thread_id or f"t{mid}", "labelIds": ["SENT"]}


def parse_raw(raw: str) -> MimeMessage:
    return message_from_bytes(b64url_decode(raw), policy=policy.default)
