"""Meta WhatsApp Cloud API implementation."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

import httpx

from app.channels.base import (
    ChannelError,
    InboundMessage,
    MediaDownload,
    MessagingChannel,
    ParsedWebhook,
    StatusUpdate,
)
from app.config import get_settings

GRAPH = "https://graph.facebook.com"
OUTSIDE_WINDOW_CODES = {131047}  # "Re-engagement message": >24h since last user message
RETRYABLE_CODES = {1, 2, 4, 17, 341, 130429, 131016, 131048, 131056, 133004}
MEDIA_TYPES = ("image", "document", "audio", "video", "sticker")


def parse_meta_payload(payload: dict[str, Any]) -> ParsedWebhook:
    """Shared parser (also used by the console channel and the simulator)."""
    out = ParsedWebhook()
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for m in value.get("messages") or []:
                out.messages.append(_parse_message(m))
            for st in value.get("statuses") or []:
                errors = st.get("errors") or []
                err = "; ".join(f"{e.get('code')}: {e.get('title')}" for e in errors) or None
                out.statuses.append(StatusUpdate(provider_message_id=st.get("id", ""),
                                                 status=st.get("status", ""), error=err))
    return out


def _parse_message(m: dict[str, Any]) -> InboundMessage:
    mtype = m.get("type", "other")
    ts = m.get("timestamp")
    msg = InboundMessage(
        provider_message_id=m.get("id", ""),
        from_number=m.get("from", ""),
        timestamp=datetime.fromtimestamp(int(ts), UTC) if ts and str(ts).isdigit() else None,
        type=mtype if mtype in ("text", *MEDIA_TYPES) else "other",
        context_message_id=(m.get("context") or {}).get("id"),
        raw=m,
    )
    if mtype == "text":
        msg.text = (m.get("text") or {}).get("body", "")
    elif mtype in MEDIA_TYPES:
        media = m.get(mtype) or {}
        msg.media_id = media.get("id")
        msg.media_mime = media.get("mime_type")
        msg.filename = media.get("filename")
        msg.text = media.get("caption", "") or ""
    elif mtype == "button":
        msg.type, msg.text = "text", (m.get("button") or {}).get("text", "")
    elif mtype == "interactive":
        inter = m.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        msg.type, msg.text = "text", reply.get("title", "")
    return msg


def compute_signature(secret: str, raw_body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


class MetaWhatsAppChannel(MessagingChannel):
    name = "meta"

    def __init__(self, http: httpx.Client | None = None):
        s = get_settings()
        self.token = s.whatsapp_access_token.get_secret_value()
        self.phone_id = s.whatsapp_phone_number_id
        self.secret = s.whatsapp_app_secret.get_secret_value()
        self.base = f"{GRAPH}/{s.whatsapp_api_version}"
        self.http = http or httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))

    # ---- helpers ----
    def _headers(self) -> dict[str, str]:
        if not self.token or not self.phone_id:
            raise ChannelError("WhatsApp credentials are not configured", retryable=False)
        return {"Authorization": f"Bearer {self.token}"}

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            resp = self.http.request(method, url, headers=self._headers(), **kw)
        except httpx.HTTPError as exc:
            raise ChannelError(f"WhatsApp network error: {exc}", retryable=True) from exc
        if resp.status_code >= 400:
            raise _error_from(resp)
        return resp

    def _send(self, body: dict[str, Any]) -> str:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", **body}
        data = self._request("POST", f"{self.base}/{self.phone_id}/messages", json=body).json()
        return data["messages"][0]["id"]

    @staticmethod
    def _to(number: str) -> str:
        return "".join(ch for ch in number if ch.isdigit())

    # ---- interface ----
    def send_text(self, to: str, text: str) -> str:
        return self._send({"to": self._to(to), "type": "text", "text": {"body": text, "preview_url": False}})

    def send_template(self, to: str, template: str, language: str, params: list[str] | None = None) -> str:
        tpl: dict[str, Any] = {"name": template, "language": {"code": language}}
        if params:
            tpl["components"] = [{"type": "body",
                                  "parameters": [{"type": "text", "text": p} for p in params]}]
        return self._send({"to": self._to(to), "type": "template", "template": tpl})

    def send_document(self, to: str, data: bytes, filename: str, mime_type: str, caption: str = "") -> str:
        up = self._request(
            "POST", f"{self.base}/{self.phone_id}/media",
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (filename, data, mime_type)},
        ).json()
        doc = {"id": up["id"], "filename": filename}
        if caption:
            doc["caption"] = caption
        return self._send({"to": self._to(to), "type": "document", "document": doc})

    def download_media(self, media_id: str) -> MediaDownload:
        meta = self._request("GET", f"{self.base}/{media_id}").json()
        resp = self._request("GET", meta["url"])
        return MediaDownload(data=resp.content, mime_type=meta.get("mime_type", "application/octet-stream"))

    def parse_webhook(self, payload: dict[str, Any]) -> ParsedWebhook:
        return parse_meta_payload(payload)

    def verify_signature(self, raw_body: bytes, signature_header: str | None) -> bool:
        if not self.secret or not signature_header:
            return False
        return hmac.compare_digest(compute_signature(self.secret, raw_body), signature_header.strip())


def _error_from(resp: httpx.Response) -> ChannelError:
    try:
        err = resp.json().get("error", {})
    except ValueError:
        err = {}
    code = err.get("code")
    message = f"WhatsApp API {resp.status_code}: {err.get('message') or resp.text[:200]} (code {code})"
    if code in OUTSIDE_WINDOW_CODES:
        return ChannelError(message, retryable=False, outside_window=True, code=code)
    retryable = resp.status_code >= 500 or resp.status_code == 429 or code in RETRYABLE_CODES
    return ChannelError(message, retryable=retryable, code=code)
