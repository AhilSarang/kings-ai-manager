"""Local channel for development and tests: records sends instead of calling Meta.

Inbound media for simulations is read from <FILE_STORAGE_DIR>/_sim_inbox/<media_id>.
"""

from __future__ import annotations

import hmac
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from app.channels.base import ChannelError, MediaDownload, MessagingChannel, ParsedWebhook
from app.channels.meta_whatsapp import compute_signature, parse_meta_payload
from app.config import get_settings

log = logging.getLogger(__name__)


class ConsoleChannel(MessagingChannel):
    name = "console"
    sent: list[dict[str, Any]] = []
    fail_next: list[ChannelError] = []

    @classmethod
    def reset(cls) -> None:
        cls.sent.clear()
        cls.fail_next.clear()

    def _record(self, **item) -> str:
        if type(self).fail_next:
            raise type(self).fail_next.pop(0)
        item["id"] = f"console-{uuid.uuid4().hex[:12]}"
        type(self).sent.append(item)
        log.info("console send", extra={"kind": item["kind"], "to": item["to"],
                                        "preview": str(item.get("text", ""))[:120]})
        return item["id"]

    def send_text(self, to: str, text: str) -> str:
        return self._record(kind="text", to=to, text=text)

    def send_template(self, to: str, template: str, language: str, params: list[str] | None = None) -> str:
        return self._record(kind="template", to=to, template=template, params=params or [])

    def send_document(self, to: str, data: bytes, filename: str, mime_type: str, caption: str = "") -> str:
        return self._record(kind="document", to=to, filename=filename, size=len(data), text=caption)

    def download_media(self, media_id: str) -> MediaDownload:
        path = Path(get_settings().file_storage_dir) / "_sim_inbox" / media_id
        if not path.is_file():
            raise ChannelError(f"Simulated media {media_id} not found", retryable=False)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return MediaDownload(data=path.read_bytes(), mime_type=mime, filename=path.name)

    def parse_webhook(self, payload: dict[str, Any]) -> ParsedWebhook:
        return parse_meta_payload(payload)

    def verify_signature(self, raw_body: bytes, signature_header: str | None) -> bool:
        secret = get_settings().whatsapp_app_secret.get_secret_value()
        if not secret or not signature_header:
            return False
        return hmac.compare_digest(compute_signature(secret, raw_body), signature_header.strip())
