"""Channel factory."""

from __future__ import annotations

from functools import lru_cache

from app.channels.base import MessagingChannel
from app.config import get_settings


@lru_cache(maxsize=2)
def _channel_for(backend: str) -> MessagingChannel:
    if backend == "console":
        from app.channels.console import ConsoleChannel

        return ConsoleChannel()
    from app.channels.meta_whatsapp import MetaWhatsAppChannel

    return MetaWhatsAppChannel()


def get_channel() -> MessagingChannel:
    return _channel_for(get_settings().messaging_backend)
