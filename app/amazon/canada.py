"""Copying listings to another marketplace, starting with Canada (brief 11.5).

Prices are never converted. The owner gives the CAD price, or the copy doesn't happen.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon import accounts, catalog
from app.amazon import listings as listings_svc
from app.amazon import marketplaces as mkt
from app.amazon import patches as patch_svc
from app.amazon import schemas
from app.amazon import validation as v
from app.db.models import AmazonChangeRequest, AmazonListing
from app.services import settings_store
from app.services.common import Actor, ServiceError

log = logging.getLogger(__name__)

BILINGUAL_WARNING = (
    "Canadian consumer products usually need bilingual English and French packaging and labelling. "
    "I can't check your packaging, so that's on you before this sells in Canada."
)


class CopyError(ServiceError):
    pass


def enabled_codes(session: Session) -> list[str]:
    account = accounts.get_primary(session)
    return list(account.enabled_marketplaces or ["US"]) if account else ["US"]


def set_enabled(session: Session, codes: list[str], actor: Actor) -> str:
    """Turning a marketplace on is a deliberate act; writes are refused until it is."""
    account = accounts.require(session)
    clean = []
    for code in codes:
        mkt.by_code(code)  # raises on an unknown one
        clean.append(code.strip().upper())
    account.enabled_marketplaces = clean
    settings_store.set_value(session, "amazon_enabled_marketplaces", ",".join(clean), actor)
    session.flush()
    return f"🌎 Amazon marketplaces enabled for changes: {', '.join(clean)}."


def require_enabled(session: Session, code: str) -> None:
    if code.upper() not in [c.upper() for c in enabled_codes(session)]:
        raise CopyError(f"{code.upper()} isn't enabled yet. Say \"enable Amazon Canada\" first, and I'll only "
                        "write to it after that.")


def find_target(session: Session, listing: AmazonListing, target_code: str) -> AmazonListing | None:
    mp = mkt.by_code(target_code)
    rows = listings_svc.find(session, sku=listing.sku, marketplace_id=mp.marketplace_id)
    return rows[0] if rows else None


def asin_exists_there(target_code: str, asin: str | None) -> bool:
    if not asin:
        return False
    try:
        return bool(catalog.search_by_identifier(target_code, asin))
    except Exception as exc:  # a catalogue hiccup shouldn't block the draft
        log.warning("catalogue check failed", extra={"error": str(exc)[:120]})
        return False


def copy_listing(session: Session, listing: AmazonListing, *, target_code: str = "CA",
                 price: float | None, instructions: str = "", source_message_id=None) -> AmazonChangeRequest:
    """Draft the same product in another marketplace, with the owner's own price."""
    from app.amazon import changes as changes_svc
    from app.amazon import intake as intake_svc

    require_enabled(session, target_code)
    target = mkt.by_code(target_code)
    source = mkt.by_id(listing.marketplace_id)
    if target.code == source.code:
        raise CopyError(f"{listing.ref} is already the {source.code} listing.")
    existing = find_target(session, listing, target_code)
    if existing is not None:
        raise CopyError(f"{listing.sku} already exists in {target.code} as {existing.ref}. Tell me what to "
                        "change on it instead.")
    if price is None:
        raise CopyError(f"What should {listing.sku} sell for in {target.code}, in {target.currency}? "
                        "I don't convert prices — tell me the number you want.")
    if not listing.product_type:
        raise CopyError(f"I don't know {listing.ref}'s product type yet. Reply SYNC AMAZON and try again.")

    attributes = _translate(changes_svc.current_attributes(session, listing), target)
    attributes[patch_svc.PRICE_ATTRIBUTE] = patch_svc.price_attribute(price, target.currency,
                                                                      target.marketplace_id)
    operation = ("create_offer_existing_asin" if asin_exists_there(target_code, listing.asin)
                 else "create_new_asin")
    request = intake_svc.build_new_listing(
        session, sku=listing.sku, marketplace_code=target.code, product_type=listing.product_type,
        attributes=attributes, instructions=instructions or f"Copy {listing.ref} to {target.code}",
        operation=operation, asin=listing.asin if operation == "create_offer_existing_asin" else None,
        source_message_id=source_message_id,
        # The product already sells in the source marketplace, so its barcode (or its exemption) is settled;
        # if Canada wants one anyway, Amazon's preview says so.
        exemption_confirmed=True)
    request.warnings = list(request.warnings or []) + [BILINGUAL_WARNING]
    session.flush()
    return request


def _translate(attributes: dict[str, Any], target) -> dict[str, Any]:
    """Same content, re-tagged for the target marketplace. Text is not machine-translated."""
    out: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if key == patch_svc.PRICE_ATTRIBUTE:
            continue
        if isinstance(value, list):
            items = []
            for entry in value:
                if isinstance(entry, dict):
                    entry = {**entry}
                    if "marketplace_id" in entry:
                        entry["marketplace_id"] = target.marketplace_id
                    if "language_tag" in entry:
                        entry["language_tag"] = target.locale.replace("_", "-")
                    items.append(entry)
                else:
                    items.append({"value": entry, "marketplace_id": target.marketplace_id})
            out[key] = items
        else:
            out[key] = value
    return out


def copy_summary(session: Session, request: AmazonChangeRequest, source: AmazonListing, target_code: str) -> str:
    target = mkt.by_code(target_code)
    price = patch_svc.price_of(request.payload.get("attributes") or {})
    lines = [f"🇨🇦 Copying {source.ref} {source.sku} to {target.code} as {request.ref}",
             f"Price: {target.currency} {price:,.2f} (your number, not a conversion)"]
    if request.operation == "create_offer_existing_asin":
        lines.append(f"Amazon already has {source.asin} in {target.code}, so this adds our offer rather than "
                     "creating a second ASIN.")
    else:
        lines.append(f"This creates a new {target.code} listing.")
    lines.append(f"⚠️ {BILINGUAL_WARNING}")
    return "\n".join(lines)


def copy_brand(session: Session, brand_name: str, *, target_code: str = "CA",
               prices: dict[str, float]) -> list[AmazonChangeRequest]:
    """Bulk copy: every buyable listing for a brand that the owner has priced."""
    require_enabled(session, target_code)
    source = mkt.by_code("US")
    rows = [r for r in session.execute(select(AmazonListing).where(
        AmazonListing.marketplace_id == source.marketplace_id)).scalars()
        if r.brand and r.brand.name.lower() == brand_name.strip().lower()]
    if not rows:
        raise CopyError(f"I don't have any {brand_name} listings to copy.")
    missing = [r.sku for r in rows if r.sku not in prices]
    if missing:
        raise CopyError(f"I need {target_code} prices for: {', '.join(missing)}. Send them as "
                        "\"SKU price\" lines — I don't convert prices.")
    return [copy_listing(session, row, target_code=target_code, price=prices[row.sku]) for row in rows]


def parse_prices(text: str) -> dict[str, float]:
    """"SKZ-001 32.99" lines from the owner's message."""
    import re

    out = {}
    for line in (text or "").splitlines():
        match = re.match(r"\s*([\w.\-/]+)\s*[|,:]?\s*\$?([\d.]+)\s*$", line.strip())
        if match:
            out[match.group(1)] = float(match.group(2))
    return out
