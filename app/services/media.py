"""Public image hosting (Cloudflare R2 or any S3-compatible bucket).

Amazon fetches listing images over the internet, so approved images are copied to a public bucket
on unguessable paths. Nothing else in the app is ever made public.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from abc import ABC, abstractmethod
from functools import lru_cache

from app.config import get_settings
from app.services.common import ServiceError

log = logging.getLogger(__name__)


class MediaError(ServiceError):
    pass


class PublicMediaStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store the bytes and return the public URL."""

    @abstractmethod
    def delete(self, key: str) -> None: ...


class S3MediaStore(PublicMediaStore):
    def __init__(self) -> None:
        import boto3

        s = get_settings()
        if not (s.public_media_bucket and s.public_media_base_url):
            raise MediaError("Public image hosting isn't configured (PUBLIC_MEDIA_BUCKET / _BASE_URL).")
        self.bucket = s.public_media_bucket
        self.base_url = s.public_media_base_url.rstrip("/")
        self.client = boto3.client(
            "s3", endpoint_url=s.public_media_endpoint or None,
            aws_access_key_id=s.public_media_access_key_id or None,
            aws_secret_access_key=s.public_media_secret_access_key.get_secret_value() or None,
            region_name="auto")

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return f"{self.base_url}/{key}"

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


class FakeMediaStore(PublicMediaStore):
    """Used by tests and by local runs: keeps bytes in memory, hands back a plausible URL."""

    objects: dict[str, tuple[bytes, str]] = {}

    @classmethod
    def reset(cls) -> None:
        cls.objects = {}

    def put(self, key: str, data: bytes, content_type: str) -> str:
        type(self).objects[key] = (data, content_type)
        return f"https://img.example.test/{key}"

    def delete(self, key: str) -> None:
        type(self).objects.pop(key, None)


@lru_cache(maxsize=1)
def _store_for(backend: str) -> PublicMediaStore:
    return FakeMediaStore() if backend == "fake" else S3MediaStore()


def get_store() -> PublicMediaStore:
    return _store_for(get_settings().public_media_backend)


def public_key(prefix: str, data: bytes, extension: str = "jpg") -> str:
    """Unguessable path: content hash plus random bytes, so URLs can't be walked."""
    digest = hashlib.sha256(data).hexdigest()[:16]
    return f"{prefix}/{digest}{secrets.token_hex(8)}.{extension.lstrip('.')}"
