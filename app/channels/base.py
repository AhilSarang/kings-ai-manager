"""Messaging channel interface (WhatsApp today; Twilio or others later)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MAX_TEXT_LEN = 4096


@dataclass
class InboundMessage:
    provider_message_id: str
    from_number: str
    timestamp: datetime | None
    type: str  # text | image | document | audio | video | sticker | other
    text: str = ""
    media_id: str | None = None
    media_mime: str | None = None
    filename: str | None = None
    context_message_id: str | None = None  # the message this replies to
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StatusUpdate:
    provider_message_id: str
    status: str  # sent | delivered | read | failed
    error: str | None = None


@dataclass
class ParsedWebhook:
    messages: list[InboundMessage] = field(default_factory=list)
    statuses: list[StatusUpdate] = field(default_factory=list)


@dataclass
class MediaDownload:
    data: bytes
    mime_type: str
    filename: str | None = None


class ChannelError(Exception):
    def __init__(self, message: str, *, retryable: bool, outside_window: bool = False, code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.outside_window = outside_window
        self.code = code


class MessagingChannel(ABC):
    name: str = "base"

    @abstractmethod
    def send_text(self, to: str, text: str) -> str:
        """Send a free-form text. Returns the provider message id."""

    @abstractmethod
    def send_template(self, to: str, template: str, language: str, params: list[str] | None = None) -> str: ...

    @abstractmethod
    def send_document(self, to: str, data: bytes, filename: str, mime_type: str, caption: str = "") -> str: ...

    @abstractmethod
    def download_media(self, media_id: str) -> MediaDownload: ...

    @abstractmethod
    def parse_webhook(self, payload: dict[str, Any]) -> ParsedWebhook: ...

    @abstractmethod
    def verify_signature(self, raw_body: bytes, signature_header: str | None) -> bool: ...
