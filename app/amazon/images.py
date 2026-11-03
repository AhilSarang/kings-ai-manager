"""Listing images (brief Section 9): Drive → checks → JPEG → public bucket → Amazon attributes."""

from __future__ import annotations

import hashlib
import io
import logging
from typing import Any

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon import drive as drive_svc
from app.db.models import AmazonChangeRequest, AmazonImage, AmazonListing, File
from app.services import files as files_svc
from app.services import media
from app.services.common import Actor, ServiceError

log = logging.getLogger(__name__)
AGENT = Actor.agent("amazon")

ACCEPTED_FORMATS = {"JPEG", "PNG", "TIFF", "GIF", "WEBP"}
MIN_SIDE_BLOCK = 500
MIN_SIDE_WARN = 1000
MAX_BYTES = 10 * 1024 * 1024
SLOTS = ["main_product_image_locator"] + [f"other_product_image_locator_{i}" for i in range(1, 9)]
SWATCH_SLOT = "swatch_product_image_locator"
WHITE_TOLERANCE = 12       # how far from pure white still counts as a white background
WHITE_CORNER_FRACTION = 0.9


class ImageError(ServiceError):
    pass


def _open(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        return image
    except Exception as exc:  # Pillow raises many different things
        raise ImageError("That file isn't an image I can read.") from exc


def looks_white_background(image: Image.Image) -> bool:
    """Rough check of the four corners, to warn rather than to block."""
    rgb = image.convert("RGB")
    w, h = rgb.size
    inset = max(1, min(w, h) // 50)
    corners = [(inset, inset), (w - inset - 1, inset), (inset, h - inset - 1), (w - inset - 1, h - inset - 1)]
    white = sum(1 for xy in corners if all(c >= 255 - WHITE_TOLERANCE for c in rgb.getpixel(xy)))
    return white / len(corners) >= WHITE_CORNER_FRACTION


def to_jpeg(image: Image.Image) -> bytes:
    """Amazon wants JPEG; transparency becomes white, as Amazon's own guidance says."""
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        image = canvas
    else:
        image = image.convert("RGB")
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue()


def check(data: bytes, filename: str = "") -> dict[str, Any]:
    """Every check from brief 9.3, as a record we keep with the image."""
    image = _open(data)
    fmt = (image.format or "").upper()
    width, height = image.size
    result: dict[str, Any] = {"format": fmt, "width": width, "height": height, "bytes": len(data),
                              "errors": [], "warnings": [], "animated": getattr(image, "is_animated", False)}
    if fmt not in ACCEPTED_FORMATS:
        result["errors"].append(f"{filename or 'This image'} is {fmt or 'an unknown format'}; Amazon accepts "
                                "JPEG, PNG, TIFF or GIF.")
    if result["animated"]:
        result["errors"].append(f"{filename or 'This image'} is animated; Amazon doesn't accept animated images.")
    if len(data) > MAX_BYTES:
        result["errors"].append(f"{filename or 'This image'} is {len(data) / 1024 / 1024:.1f} MB; the limit is 10 MB.")
    longest = max(width, height)
    if longest < MIN_SIDE_BLOCK:
        result["errors"].append(f"{filename or 'This image'} is {width}×{height}px. Amazon needs at least "
                                f"{MIN_SIDE_BLOCK}px on the longest side.")
    elif longest < MIN_SIDE_WARN:
        result["warnings"].append(f"{filename or 'This image'} is {width}×{height}px. Under {MIN_SIDE_WARN}px "
                                  "means no zoom on the listing.")
    result["white_background"] = looks_white_background(image)
    return result


def slot_for(index: int, *, swatch: bool = False) -> str:
    if swatch:
        return SWATCH_SLOT
    if index >= len(SLOTS):
        raise ImageError(f"Amazon takes a main image plus 8 others; that's {index + 1} images.")
    return SLOTS[index]


def collect_from_links(session: Session, text: str) -> list[dict[str, Any]]:
    """Files from every Drive link in the owner's message, folders expanded in filename order."""
    links = drive_svc.parse_links(text)
    if not any(links.values()):
        return []
    client = drive_svc.client(session)
    found: list[dict[str, Any]] = []
    for folder_id in links["folders"]:
        found += [f for f in client.list_folder(folder_id) if (f.get("mimeType") or "").startswith("image/")]
    for file_id in links["files"]:
        found.append(client.get_file(file_id))
    seen, unique = set(), []
    for f in found:
        if f["id"] not in seen:
            seen.add(f["id"])
            unique.append(f)
    return unique


def import_images(session: Session, *, text: str, listing: AmazonListing | None = None,
                  change_request: AmazonChangeRequest | None = None,
                  swatch_ids: tuple[str, ...] = ()) -> list[AmazonImage]:
    """Download, check, convert and store. Hosting happens later, on approval."""
    files = collect_from_links(session, text)
    if not files:
        return []
    client = drive_svc.client(session)
    rows: list[AmazonImage] = []
    index = 0
    for meta in files:
        data = client.download(meta["id"])
        checks = check(data, meta.get("name", ""))
        digest = hashlib.sha256(data).hexdigest()
        existing = session.execute(select(AmazonImage).where(AmazonImage.sha256 == digest)).scalars().first()
        if existing is not None and existing.status in ("hosted", "live"):
            rows.append(existing)  # same picture as before: reuse the hosted copy
            index += 1
            continue
        jpeg = data
        if not checks["errors"] and checks["format"] != "JPEG":
            jpeg = to_jpeg(_open(data))
            checks["converted_to_jpeg"] = True
        stored = files_svc.save_file(session, jpeg, mime_type="image/jpeg", source="agent", actor=AGENT,
                                     original_name=meta.get("name")) if not checks["errors"] else None
        row = AmazonImage(
            listing_id=listing.id if listing else None,
            change_request_id=change_request.id if change_request else None,
            drive_file_id=meta["id"], drive_url=f"https://drive.google.com/file/d/{meta['id']}/view",
            file_id=stored.id if stored else None, width_px=checks["width"], height_px=checks["height"],
            format=checks["format"], size_bytes=checks["bytes"], sha256=digest, checks=checks,
            slot=slot_for(index, swatch=meta["id"] in swatch_ids),
            status="rejected" if checks["errors"] else "checked")
        session.add(row)
        rows.append(row)
        if not checks["errors"]:
            index += 1
    session.flush()
    return rows


def host(session: Session, images: list[AmazonImage]) -> list[AmazonImage]:
    """Copy approved images to the public bucket so Amazon can fetch them."""
    store = media.get_store()
    for image in images:
        if image.status not in ("checked", "hosted") or image.public_url:
            continue
        if image.file_id is None:
            raise ImageError("That image was never stored, so there's nothing to publish.")
        stored = session.get(File, image.file_id)
        data = files_svc.get_store().get(stored.storage_key)
        image.public_url = store.put(media.public_key("amazon-listing-images", data), data, "image/jpeg")
        image.status = "hosted"
    session.flush()
    return images


def attributes_for(images: list[AmazonImage], marketplace_id: str) -> dict[str, Any]:
    """Slot → Amazon image attribute, skipping anything that failed its checks."""
    out: dict[str, Any] = {}
    for image in images:
        if image.status not in ("hosted", "live") or not image.public_url:
            continue
        out[image.slot] = [{"media_location": image.public_url, "marketplace_id": marketplace_id}]
    return out


def summary(images: list[AmazonImage]) -> str:
    if not images:
        return "No images found in that message."
    lines = []
    for image in images:
        label = "main image" if image.slot == SLOTS[0] else image.slot.replace("_", " ")
        state = {"checked": "ready", "hosted": "published", "live": "live on Amazon",
                 "rejected": "rejected"}.get(image.status, image.status)
        lines.append(f"• {label}: {image.width_px}×{image.height_px}px, {state}")
        for problem in (image.checks or {}).get("errors", []):
            lines.append(f"  ⛔ {problem}")
        for warning in (image.checks or {}).get("warnings", []):
            lines.append(f"  ⚠️ {warning}")
        if image.slot == SLOTS[0] and (image.checks or {}).get("white_background") is False:
            lines.append("  ⚠️ The main image doesn't look like it's on a pure white background, which Amazon "
                         "requires.")
    lines.append("Images can take a few hours to show on Amazon after a change goes live.")
    return "\n".join(lines)
