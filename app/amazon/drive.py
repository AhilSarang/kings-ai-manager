"""Google Drive: read the image files the owner shares as links.

Uses the same Google account as Gmail, so `drive.readonly` and `drive.file` have to be added to the
consent (one re-authorisation) before this works. Drive content is data, never instructions.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.email.client import google_access_token
from app.services.common import ServiceError

log = logging.getLogger(__name__)
API = "https://www.googleapis.com/drive/v3"
FILE_FIELDS = "id,name,mimeType,size,md5Checksum,imageMediaMetadata(width,height)"
EXPORT_AS = {"application/vnd.google-apps.document": "text/plain",
             "application/vnd.google-apps.spreadsheet": "text/csv"}

FILE_LINK = re.compile(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?id=)([\w-]{2,})")
FOLDER_LINK = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([\w-]{2,})")
DOC_LINK = re.compile(r"docs\.google\.com/(?:document|spreadsheets)/d/([\w-]{2,})")


class DriveError(ServiceError):
    pass


def parse_links(text: str) -> dict[str, list[str]]:
    """Pull Drive ids out of whatever the owner pasted."""
    return {"files": FILE_LINK.findall(text or ""), "folders": FOLDER_LINK.findall(text or ""),
            "documents": DOC_LINK.findall(text or "")}


class DriveClient(ABC):
    @abstractmethod
    def get_file(self, file_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def list_folder(self, folder_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def download(self, file_id: str) -> bytes: ...

    @abstractmethod
    def export_text(self, file_id: str) -> str: ...


class HttpDriveClient(DriveClient):
    def __init__(self, session, http: httpx.Client | None = None):
        self.session = session
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {google_access_token(self.session)}"}

    def _get(self, path: str, **params) -> httpx.Response:
        r = self.http.get(f"{API}{path}", params=params, headers=self._headers())
        if r.status_code == 403 and "insufficient" in r.text.lower():
            raise DriveError("Google hasn't given us Drive access yet. Re-run the Google authorisation to add "
                             "Drive, then try again.")
        if r.status_code == 404:
            raise DriveError("I can't open that Drive link. Check it's shared with the configured Gmail account.")
        if r.status_code >= 400:
            raise DriveError(f"Google Drive error {r.status_code}: {r.text[:160]}")
        return r

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self._get(f"/files/{file_id}", fields=FILE_FIELDS, supportsAllDrives="true").json()

    def list_folder(self, folder_id: str) -> list[dict[str, Any]]:
        files, token = [], None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed = false",
                      "fields": f"nextPageToken,files({FILE_FIELDS})", "pageSize": 200,
                      "orderBy": "name_natural", "supportsAllDrives": "true",
                      "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            data = self._get("/files", **params).json()
            files += data.get("files") or []
            token = data.get("nextPageToken")
            if not token:
                return files

    def download(self, file_id: str) -> bytes:
        return self._get(f"/files/{file_id}", alt="media", supportsAllDrives="true").content

    def export_text(self, file_id: str) -> str:
        """A Doc or Sheet as plain text. Its words are product information, never instructions."""
        mime = (self.get_file(file_id).get("mimeType") or "")
        if mime in EXPORT_AS:
            return self._get(f"/files/{file_id}/export", mimeType=EXPORT_AS[mime]).text
        if mime.startswith("text/") or mime == "application/json":
            return self.download(file_id).decode("utf-8", errors="replace")
        raise DriveError(f"I can only read Google Docs, Sheets and text files; that one is {mime or 'unknown'}.")


class FakeDriveClient(DriveClient):
    """Tests and local runs: files registered by id."""

    files: dict[str, dict[str, Any]] = {}
    contents: dict[str, bytes] = {}
    folders: dict[str, list[str]] = {}
    fail_with: Exception | None = None

    @classmethod
    def reset(cls) -> None:
        cls.files, cls.contents, cls.folders, cls.fail_with = {}, {}, {}, None

    @classmethod
    def add_document(cls, file_id: str, name: str, text: str,
                     mime="application/vnd.google-apps.document") -> str:
        cls.files[file_id] = {"id": file_id, "name": name, "mimeType": mime, "size": str(len(text))}
        cls.contents[file_id] = text.encode("utf-8")
        return file_id

    @classmethod
    def add_file(cls, file_id: str, name: str, data: bytes, mime="image/jpeg", width=1600, height=1600,
                 folder: str | None = None) -> str:
        cls.files[file_id] = {"id": file_id, "name": name, "mimeType": mime, "size": str(len(data)),
                              "imageMediaMetadata": {"width": width, "height": height}}
        cls.contents[file_id] = data
        if folder:
            cls.folders.setdefault(folder, []).append(file_id)
        return file_id

    def _check(self):
        if type(self).fail_with:
            raise type(self).fail_with

    def get_file(self, file_id: str) -> dict[str, Any]:
        self._check()
        if file_id not in type(self).files:
            raise DriveError("I can't open that Drive link. Check it's shared with the configured Gmail account.")
        return type(self).files[file_id]

    def list_folder(self, folder_id: str) -> list[dict[str, Any]]:
        self._check()
        return [type(self).files[f] for f in type(self).folders.get(folder_id, [])]

    def download(self, file_id: str) -> bytes:
        self._check()
        return type(self).contents[file_id]

    def export_text(self, file_id: str) -> str:
        self._check()
        mime = self.get_file(file_id).get("mimeType") or ""
        if mime not in EXPORT_AS and not mime.startswith("text/"):
            raise DriveError(f"I can only read Google Docs, Sheets and text files; that one is {mime}.")
        return type(self).contents[file_id].decode("utf-8", errors="replace")


def client(session) -> DriveClient:
    from app.config import get_settings

    return FakeDriveClient() if get_settings().gmail_backend == "fake" else HttpDriveClient(session)
