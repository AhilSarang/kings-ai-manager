"""FBA inventory snapshots and days-of-cover, used for low-stock alerts and shipment sanity checks."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.amazon import accounts, api
from app.amazon import marketplaces as mkt
from app.db.models import AmazonInventorySnapshot, AmazonSalesDaily
from app.services.common import utcnow

log = logging.getLogger(__name__)


def fetch(client, marketplace_id: str, next_token: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"granularityType": "Marketplace", "granularityId": marketplace_id,
                              "marketplaceIds": marketplace_id, "details": "true"}
    if next_token:
        params["nextToken"] = next_token
    return client.get("/fba/inventory/v1/summaries", params=params, operation="getInventorySummaries")


def _qty(value: Any) -> int:
    """Amazon sends some of these as a number and others as a breakdown object with a total."""
    if isinstance(value, dict):
        total = next((v for k, v in value.items() if k.lower().startswith("total")), None)
        if total is None:
            total = sum(v for v in value.values() if isinstance(v, (int, float)))
        return int(total or 0)
    return int(value or 0)


def _detail(summary: dict[str, Any]) -> dict[str, int]:
    d = summary.get("inventoryDetails") or {}
    inbound = sum(_qty(d.get(k)) for k in ("inboundWorkingQuantity", "inboundShippedQuantity",
                                           "inboundReceivingQuantity"))
    return {"fulfillable": _qty(d.get("fulfillableQuantity")), "inbound": inbound,
            "reserved": _qty(d.get("reservedQuantity")),
            "unfulfillable": _qty(d.get("unfulfillableQuantity")) + _qty(d.get("researchingQuantity"))}


def snapshot(session: Session, marketplace_id: str | None = None) -> int:
    account = accounts.require(session)
    client = api.client()
    stored = 0
    for code in ([mkt.by_id(marketplace_id).code] if marketplace_id else (account.enabled_marketplaces or ["US"])):
        mp = mkt.by_code(code)
        token = None
        while True:
            page = fetch(client, mp.marketplace_id, token)
            payload = page.get("payload") or page
            for summary in payload.get("inventorySummaries") or []:
                d = _detail(summary)
                session.add(AmazonInventorySnapshot(
                    marketplace_id=mp.marketplace_id, sku=summary.get("sellerSku") or "",
                    asin=summary.get("asin"), fulfillable_qty=d["fulfillable"], inbound_qty=d["inbound"],
                    reserved_qty=d["reserved"], unfulfillable_qty=d["unfulfillable"]))
                stored += 1
            session.commit()
            token = (page.get("pagination") or {}).get("nextToken")
            if not token:
                break
    log.info("amazon inventory snapshot", extra={"rows": stored})
    return stored


def latest(session: Session, marketplace_id: str | None = None) -> list[AmazonInventorySnapshot]:
    """One row per SKU: the newest snapshot we have."""
    newest = select(AmazonInventorySnapshot.sku, func.max(AmazonInventorySnapshot.snapshot_at).label("at"))
    if marketplace_id:
        newest = newest.where(AmazonInventorySnapshot.marketplace_id == marketplace_id)
    newest = newest.group_by(AmazonInventorySnapshot.sku).subquery()
    q = select(AmazonInventorySnapshot).join(
        newest, (AmazonInventorySnapshot.sku == newest.c.sku) & (AmazonInventorySnapshot.snapshot_at == newest.c.at))
    return list(session.execute(q.order_by(AmazonInventorySnapshot.sku)).scalars())


def for_sku(session: Session, sku: str) -> AmazonInventorySnapshot | None:
    return session.execute(
        select(AmazonInventorySnapshot).where(AmazonInventorySnapshot.sku == sku)
        .order_by(AmazonInventorySnapshot.snapshot_at.desc()).limit(1)).scalar_one_or_none()


def daily_velocity(session: Session, sku: str, days: int = 30) -> float:
    """Units per day over the trailing window; 0 when we have no sales history yet."""
    since = (utcnow() - timedelta(days=days)).date()
    units = session.execute(select(func.coalesce(func.sum(AmazonSalesDaily.units), 0))
                            .where(AmazonSalesDaily.sku == sku, AmazonSalesDaily.date >= since)).scalar_one()
    return round(float(units) / days, 3) if units else 0.0


def days_of_cover(session: Session, sku: str) -> float | None:
    """None when there are no sales to measure against."""
    velocity = daily_velocity(session, sku)
    if velocity <= 0:
        return None
    snap = for_sku(session, sku)
    return round((snap.fulfillable_qty if snap else 0) / velocity, 1)
