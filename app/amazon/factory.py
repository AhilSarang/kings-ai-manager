"""Pick the SP-API backend. No other module decides which client it gets."""

from __future__ import annotations

from app.amazon.client import AmazonClient, HttpAmazonClient, Recorder
from app.amazon.fake import FakeAmazonClient
from app.config import get_settings


def get_client(*, recorder: Recorder | None = None) -> AmazonClient:
    if get_settings().amazon_backend == "fake":
        return FakeAmazonClient()
    return HttpAmazonClient(recorder=recorder)
