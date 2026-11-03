"""File intake: validation, random-key storage, and DB records.

Uploaded content is never executed or interpreted; only stored and linked.
"""

from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import File
from app.services import audit
from app.services.common import Actor, ServiceError

# Allowed MIME types -> extension. Anything else is rejected.
ALLOWED = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
}


class FileRejected(ServiceError):
    pass


def _sniff(data: bytes) -> str | None:
    """Detect the real type from magic bytes. None = unknown/plain."""
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image/heic"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "audio/mpeg"
    if head.startswith((b"MZ", b"\x7fELF", b"#!")):
        return "executable"
    return None


def verify_mime(data: bytes, claimed: str) -> str:
    claimed = (claimed or "").split(";")[0].strip().lower()
    if claimed not in ALLOWED:
        raise FileRejected(f"File type {claimed or 'unknown'} is not accepted")
    sniffed = _sniff(data)
    if sniffed == "executable":
        raise FileRejected("Executable files are not accepted")
    compatible = {
        "zip": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        "ole": {"application/vnd.ms-excel"},
        "video/mp4": {"video/mp4", "audio/mp4"},
    }
    if sniffed is None:
        if claimed not in ("text/plain", "text/csv"):
            raise FileRejected("File content does not match its type")
        try:
            data[:4096].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileRejected("Text file is not valid UTF-8") from exc
        return claimed
    if sniffed == claimed or claimed in compatible.get(sniffed, set()):
        return claimed
    raise FileRejected("File content does not match its type")


class FileStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...


class LocalFileStore(FileStore):
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise FileRejected("Invalid storage key")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.chmod(0o640)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


@lru_cache(maxsize=4)
def _store_for(root: str) -> FileStore:
    return LocalFileStore(root)


def get_store() -> FileStore:
    return _store_for(get_settings().file_storage_dir)


def save_file(
    session: Session, data: bytes, *, mime_type: str, source: str, actor: Actor,
    original_name: str | None = None, uploaded_by_user_id: uuid.UUID | None = None,
    related_task_id: uuid.UUID | None = None,
) -> File:
    limit = get_settings().max_upload_bytes
    if len(data) == 0:
        raise FileRejected("File is empty")
    if len(data) > limit:
        raise FileRejected(f"File is larger than {limit // (1024 * 1024)} MB")
    mime = verify_mime(data, mime_type)
    now = datetime.now(UTC)
    key = f"{now:%Y/%m}/{uuid.uuid4().hex}{ALLOWED[mime]}"
    get_store().put(key, data)
    record = File(
        storage_key=key, original_name=(original_name or "")[:255] or None, mime_type=mime,
        size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), source=source,
        uploaded_by_user_id=uploaded_by_user_id, related_task_id=related_task_id,
    )
    session.add(record)
    session.flush()
    audit.record(session, actor, "file_saved", target_type="file", target_id=record.id,
                 details={"mime": mime, "size": len(data)})
    return record


def attach_to_task(session: Session, file: File, task, actor: Actor) -> None:
    from app.services import tasks

    file.related_task_id = task.id
    tasks.record_event(session, task, "file_attached", actor,
                       {"file_id": str(file.id), "name": file.original_name, "mime": file.mime_type})
    audit.record(session, actor, "file_attached", target_type="task", target_id=task.ref,
                 details={"file_id": str(file.id)})
