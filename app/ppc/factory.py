"""Pick the Ads backend. No other module decides which client it gets."""

from __future__ import annotations

from app.config import get_settings
from app.ppc.client import AdsClient, HttpAdsClient, Recorder
from app.ppc.fake import FakeAdsClient


def get_client(*, recorder: Recorder | None = None) -> AdsClient:
    if get_settings().ads_backend == "fake":
        return FakeAdsClient()
    return HttpAdsClient(recorder=recorder)
