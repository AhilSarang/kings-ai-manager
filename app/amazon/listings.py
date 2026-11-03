"""Importing and syncing our listings, with a version kept every time Amazon's copy changes."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.amazon import accounts, api
from app.amazon import marketplaces as mkt
from app.amazon.errors import AmazonError
from app.db.models import AmazonAccount, AmazonListing, AmazonListingVersion, Brand
from app.services.common import NotFound, utcnow

log = logging.getLogger(__name__)

INCLUDED_DATA = "summaries,attributes,issues,offers,fulfillmentAvailability,productTypes"
PAGE_SIZE = 20  # searchListingsItems allows up to 20


def normalize_ref(ref: str) -> str:
    ref = (ref or "").strip().upper().replace(" ", "")
    return ref if ref.startswith("L-") else f"L-{ref.lstrip('L')}"


def get_by_ref(session: Session, ref: str) -> AmazonListing:
    row = session.execute(select(AmazonListing).where(AmazonListing.ref == normalize_ref(ref))).scalar_one_or_none()
    if row is None:
        raise NotFound(f"I can't find listing {normalize_ref(ref)}.")
    return row


def find(session: Session, *, sku: str | None = None, asin: str | None = None,
         marketplace_id: str | None = None) -> list[AmazonListing]:
    q = select(AmazonListing)
    if sku:
        q = q.where(AmazonListing.sku.ilike(sku.strip()))
    if asin:
        q = q.where(AmazonListing.asin.ilike(asin.strip()))
    if marketplace_id:
        q = q.where(AmazonListing.marketplace_id == marketplace_id)
    return list(session.execute(q.order_by(AmazonListing.sku)).scalars())


def search(session: Session, text: str, limit: int = 10) -> list[AmazonListing]:
    like = f"%{text.strip()}%"
    q = select(AmazonListing).where(or_(AmazonListing.sku.ilike(like), AmazonListing.asin.ilike(like),
                                        AmazonListing.title_cached.ilike(like)))
    return list(session.execute(q.order_by(AmazonListing.sku).limit(limit)).scalars())


# ---------------- reading from Amazon ----------------
def fetch_page(client, seller_id: str, marketplace_id: str, page_token: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"marketplaceIds": marketplace_id, "includedData": INCLUDED_DATA,
                              "pageSize": PAGE_SIZE}
    if page_token:
        params["pageToken"] = page_token
    return client.get(f"/listings/2021-08-01/items/{seller_id}", params=params, operation="searchListingsItems")


def fetch_one(client, seller_id: str, sku: str, marketplace_id: str) -> dict[str, Any]:
    return client.get(f"/listings/2021-08-01/items/{seller_id}/{sku}",
                      params={"marketplaceIds": marketplace_id, "includedData": INCLUDED_DATA},
                      operation="getListingsItem")


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    summaries = item.get("summaries") or []
    return summaries[0] if summaries else {}


def _price(item: dict[str, Any]) -> tuple[float | None, str | None]:
    for offer in item.get("offers") or []:
        price = offer.get("price") or {}
        if price.get("amount") is not None:
            return float(price["amount"]), price.get("currencyCode")
    return None, None


def _fulfillment(item: dict[str, Any]) -> str:
    for fa in item.get("fulfillmentAvailability") or []:
        if (fa.get("fulfillmentChannelCode") or "").upper().startswith("AMAZON"):
            return "FBA"
    return "FBM"


def _issues(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"code": i.get("code"), "message": i.get("message"), "severity": i.get("severity"),
             "attributes": i.get("attributeNames") or []} for i in item.get("issues") or []]


def _brand_id(session: Session, item: dict[str, Any]) -> Any:
    name = (_summary(item).get("brand") or "").strip()
    if not name:
        attrs = item.get("attributes") or {}
        values = attrs.get("brand") or []
        name = (values[0].get("value") if values and isinstance(values[0], dict) else "") or ""
    if not name:
        return None
    brand = session.execute(select(Brand).where(Brand.name.ilike(name.strip()))).scalar_one_or_none()
    return brand.id if brand else None


def upsert(session: Session, account: AmazonAccount, marketplace_id: str, item: dict[str, Any]) -> AmazonListing:
    """Store one listing and, when Amazon's copy differs from ours, keep a new version."""
    sku = item.get("sku") or item.get("seller-sku")
    if not sku:
        raise AmazonError("Amazon returned a listing with no SKU")
    summary = _summary(item)
    row = session.execute(select(AmazonListing).where(
        AmazonListing.account_id == account.id, AmazonListing.marketplace_id == marketplace_id,
        AmazonListing.sku == sku)).scalar_one_or_none()
    if row is None:
        row = AmazonListing(account_id=account.id, marketplace_id=marketplace_id, sku=sku)
        session.add(row)
    price, currency = _price(item)
    row.asin = summary.get("asin") or row.asin
    row.fnsku = summary.get("fnSku") or row.fnsku
    row.product_type = summary.get("productType") or row.product_type
    row.title_cached = summary.get("itemName") or row.title_cached
    row.status_cached = ", ".join(summary.get("status") or []) or row.status_cached
    row.buyable = "BUYABLE" in (summary.get("status") or [])
    row.issues_cached = _issues(item)
    row.fulfillment = _fulfillment(item)
    row.brand_id = row.brand_id or _brand_id(session, item)
    row.parentage = (summary.get("mainImage") and row.parentage) or row.parentage
    was_price = float(row.price_cached) if row.price_cached is not None else None
    if price is not None:
        row.price_cached, row.currency = price, currency
    elif currency:
        row.currency = currency
    if price is not None and (was_price is None or abs(was_price - float(price)) >= 0.01):
        # Every target ACOS is a fraction of a price, so a price change makes them all wrong at once.
        # The PPC agent is told rather than waiting for tomorrow's schedule (brief 6.5).
        _price_changed(session, row, was_price, float(price))
    row.currency = row.currency or mkt.by_id(marketplace_id).currency
    row.last_synced_at = utcnow()
    session.flush()
    _version_if_changed(session, row, item.get("attributes") or {})
    return row


def _price_changed(session: Session, listing: AmazonListing, was: float | None, now: float) -> None:
    """Ask the PPC agent to redo this ASIN's economics. Quietly, if PPC isn't running yet."""
    if not listing.asin:
        return
    try:
        from app.jobs import queue
        from app.jobs.handlers.ppc import ads_configured

        if not ads_configured():
            return
        queue.enqueue(session, "ppc_economics", {"asins": [listing.asin]},
                      idempotency_key=f"ppc_economics:{listing.asin}:{now:.2f}", coalesce=True)
        log.info("price changed; PPC economics queued",
                 extra={"asin": listing.asin, "was": was, "now": now})
    except Exception:      # a listing sync must never fail because of a downstream agent
        log.warning("could not queue a PPC economics refresh for %s", listing.asin, exc_info=True)


def _version_if_changed(session: Session, listing: AmazonListing, attributes: dict[str, Any]) -> None:
    current = session.get(AmazonListingVersion, listing.current_version_id) if listing.current_version_id else None
    if current is not None and current.attributes == attributes:
        return
    last_number = session.execute(
        select(AmazonListingVersion.version_number).where(AmazonListingVersion.listing_id == listing.id)
        .order_by(AmazonListingVersion.version_number.desc()).limit(1)).scalar()
    version = AmazonListingVersion(listing_id=listing.id, version_number=(last_number or 0) + 1,
                                   source="amazon_sync", attributes=attributes, retrieved_at=utcnow(),
                                   created_by="amazon_sync")
    session.add(version)
    session.flush()
    listing.current_version_id = version.id
    session.flush()


def versions(session: Session, listing: AmazonListing, limit: int = 20) -> list[AmazonListingVersion]:
    return list(session.execute(
        select(AmazonListingVersion).where(AmazonListingVersion.listing_id == listing.id)
        .order_by(AmazonListingVersion.version_number.desc()).limit(limit)).scalars())


def version_by_number(session: Session, listing: AmazonListing, number: int) -> AmazonListingVersion:
    row = session.execute(select(AmazonListingVersion).where(
        AmazonListingVersion.listing_id == listing.id,
        AmazonListingVersion.version_number == number)).scalar_one_or_none()
    if row is None:
        raise NotFound(f"{listing.ref} has no version V{number}.")
    return row


def sync_marketplace(session: Session, account: AmazonAccount, marketplace_id: str, *,
                     max_pages: int = 100) -> dict[str, int]:
    """Import every listing in one marketplace. Safe to re-run: it updates in place."""
    client = api.client()
    stats = {"seen": 0, "stored": 0, "pages": 0}
    token = None
    while stats["pages"] < max_pages:
        page = fetch_page(client, account.seller_id, marketplace_id, token)
        items = page.get("items") or []
        stats["pages"] += 1
        for item in items:
            stats["seen"] += 1
            upsert(session, account, marketplace_id, item)
            stats["stored"] += 1
        session.commit()
        token = ((page.get("pagination") or {}).get("nextToken"))
        if not token or not items:
            break
    log.info("amazon listings synced", extra={"marketplace": marketplace_id, **stats})
    return stats


def sync_all(session: Session) -> dict[str, int]:
    account = accounts.require(session)
    totals = {"seen": 0, "stored": 0, "pages": 0}
    for code in account.enabled_marketplaces or ["US"]:
        stats = sync_marketplace(session, account, mkt.by_code(code).marketplace_id)
        for k in totals:
            totals[k] += stats[k]
    account.last_verified_at = utcnow()
    session.commit()
    return totals


def sync_one(session: Session, listing: AmazonListing) -> AmazonListing:
    """Re-read a single listing from Amazon (used before every write: the freshness rule)."""
    account = accounts.require(session)
    item = fetch_one(api.client(), account.seller_id, listing.sku, listing.marketplace_id)
    return upsert(session, account, listing.marketplace_id, item)
