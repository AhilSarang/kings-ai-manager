"""What Phase 3 knows about a product, and what that forbids.

The brief's rule (7.5) is blunt and worth keeping blunt: **never raise a bid on a SKU that is out of
stock, low on stock, suppressed, or losing the Buy Box. Flag it and hold instead.**

The reason is that advertising spend on an unbuyable product is not merely wasted, it is actively
harmful — it burns budget, it teaches Amazon the keyword converts badly, and the damage outlives the
stockout. Every one of these conditions is knowable from data Phase 3 already collects, so there is no
excuse for the agent to find out afterwards.

This module answers one question per ASIN — *may we push this?* — and says why not in words a person
can read, because that sentence ends up in the digest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (AmazonInventorySnapshot, AmazonListing, AmazonSalesDaily, PpcProfile)
from app.services import settings_store
from app.services.common import utcnow

log = logging.getLogger(__name__)

# Defaults; both are settings so the owner can move them.
LOW_STOCK_DAYS = 14          # less cover than this and a bid increase is not safe
BUY_BOX_FLOOR = 70.0         # below this share of the Buy Box, more traffic goes to someone else
SALES_RATE_DAYS = 14         # how far back to measure the sales rate that turns stock into days
# A snapshot older than this says what was true last week, which is not what we need to know. The
# inventory job runs every two hours, so anything this old means it has been failing.
STOCK_STALE_HOURS = 36


@dataclass
class ProductContext:
    """Everything that decides whether a product can take more advertising."""

    asin: str
    sku: str | None = None
    listing_ref: str | None = None
    buyable: bool = True
    suppressed_reason: str | None = None
    fulfillable_qty: int | None = None
    inbound_qty: int = 0
    daily_sales_rate: float | None = None
    days_of_cover: float | None = None
    stock_as_of: datetime | None = None
    stock_is_stale: bool = False
    buy_box_pct: float | None = None
    price: float | None = None
    known: bool = False           # False when Phase 3 has nothing on this ASIN at all
    blocks: list[str] = field(default_factory=list)

    @property
    def may_increase(self) -> bool:
        """Whether spending *more* on this product is safe. Cutting is always allowed."""
        return not self.blocks

    @property
    def why_not(self) -> str | None:
        if not self.blocks:
            return None
        return "; ".join(self.blocks)

    def as_dict(self) -> dict[str, Any]:
        return {"asin": self.asin, "sku": self.sku, "buyable": self.buyable,
                "fulfillable_qty": self.fulfillable_qty, "days_of_cover": self.days_of_cover,
                "buy_box_pct": self.buy_box_pct, "suppressed_reason": self.suppressed_reason,
                "known": self.known, "blocks": list(self.blocks),
                "stock_as_of": self.stock_as_of.isoformat() if self.stock_as_of else None,
                "stock_is_stale": self.stock_is_stale}


def low_stock_days(session: Session) -> float:
    try:
        return float(settings_store.get(session, "ppc_low_stock_days", LOW_STOCK_DAYS))
    except (TypeError, ValueError):
        return float(LOW_STOCK_DAYS)


def buy_box_floor(session: Session) -> float:
    try:
        return float(settings_store.get(session, "ppc_buy_box_floor_pct", BUY_BOX_FLOOR))
    except (TypeError, ValueError):
        return float(BUY_BOX_FLOOR)


def _latest_snapshots(session: Session, marketplace_id: str) -> dict[str, AmazonInventorySnapshot]:
    """The most recent inventory snapshot per SKU. One query, not one per SKU."""
    newest = select(AmazonInventorySnapshot.sku,
                    func.max(AmazonInventorySnapshot.snapshot_at).label("at")).where(
        AmazonInventorySnapshot.marketplace_id == marketplace_id).group_by(
        AmazonInventorySnapshot.sku).subquery()
    rows = session.execute(select(AmazonInventorySnapshot).join(
        newest, (AmazonInventorySnapshot.sku == newest.c.sku)
        & (AmazonInventorySnapshot.snapshot_at == newest.c.at)).where(
        AmazonInventorySnapshot.marketplace_id == marketplace_id)).scalars()
    return {row.sku: row for row in rows}


def _sales_rates(session: Session, marketplace_id: str, *,
                 today: date, days: int = SALES_RATE_DAYS) -> dict[str, float]:
    """Units a day per ASIN, over the recent past. Used to turn stock into days of cover."""
    start = today - timedelta(days=days)
    rows = session.execute(select(
        AmazonSalesDaily.asin, func.sum(AmazonSalesDaily.units)).where(
        AmazonSalesDaily.marketplace_id == marketplace_id,
        AmazonSalesDaily.date >= start, AmazonSalesDaily.date < today).group_by(
        AmazonSalesDaily.asin)).all()
    return {asin: float(units or 0) / days for asin, units in rows}


def _buy_box(session: Session, marketplace_id: str, *, today: date,
             days: int = 7) -> dict[str, float]:
    """Average Buy Box share per ASIN over the last week, where Amazon reported it."""
    start = today - timedelta(days=days)
    rows = session.execute(select(
        AmazonSalesDaily.asin, func.avg(AmazonSalesDaily.buy_box_pct)).where(
        AmazonSalesDaily.marketplace_id == marketplace_id,
        AmazonSalesDaily.date >= start, AmazonSalesDaily.date < today,
        AmazonSalesDaily.buy_box_pct.is_not(None)).group_by(AmazonSalesDaily.asin)).all()
    return {asin: float(pct) for asin, pct in rows if pct is not None}


def _suppressed(listing: AmazonListing) -> str | None:
    """Why Amazon isn't showing this listing, in its own words where it gave any."""
    if listing.buyable:
        return None
    for issue in listing.issues_cached or []:
        if (issue or {}).get("severity") == "ERROR" and (issue or {}).get("message"):
            return str(issue["message"])[:200]
    status = (listing.status_cached or "").strip()
    return f"Amazon has it as {status}" if status else "Amazon isn't showing it as buyable"


def build(session: Session, profile: PpcProfile, *, today: date,
          asins: list[str] | None = None) -> dict[str, ProductContext]:
    """One context per ASIN for the whole marketplace, in four queries rather than four per ASIN."""
    # Filtered in SQL when the caller knows which ASINs it wants — which the analysis always does.
    # Fetching the whole marketplace and discarding most of it read the entire catalogue every run.
    q = select(AmazonListing).where(AmazonListing.marketplace_id == profile.marketplace_id,
                                    AmazonListing.asin.is_not(None))
    wanted = {a.strip().upper() for a in asins} if asins else None
    if wanted:
        q = q.where(AmazonListing.asin.in_(wanted))
    listings = list(session.execute(q.order_by(AmazonListing.created_at)).scalars())

    snapshots = _latest_snapshots(session, profile.marketplace_id)
    rates = _sales_rates(session, profile.marketplace_id, today=today)
    boxes = _buy_box(session, profile.marketplace_id, today=today)
    floor_days = low_stock_days(session)
    floor_box = buy_box_floor(session)

    out: dict[str, ProductContext] = {}
    for listing in listings:
        asin = (listing.asin or "").upper()
        snapshot = snapshots.get(listing.sku)
        rate = rates.get(asin)
        got = ProductContext(
            asin=asin, sku=listing.sku, listing_ref=listing.ref, buyable=bool(listing.buyable),
            suppressed_reason=_suppressed(listing),
            fulfillable_qty=snapshot.fulfillable_qty if snapshot else None,
            inbound_qty=snapshot.inbound_qty if snapshot else 0,
            stock_as_of=snapshot.snapshot_at if snapshot else None,
            daily_sales_rate=rate, buy_box_pct=boxes.get(asin),
            price=float(listing.price_cached) if listing.price_cached is not None else None,
            known=True)
        if snapshot is not None and rate:
            got.days_of_cover = round(snapshot.fulfillable_qty / rate, 1) if rate > 0 else None
        if got.stock_as_of is not None:
            age = utcnow() - got.stock_as_of
            got.stock_is_stale = age > timedelta(hours=STOCK_STALE_HOURS)

        # A listing that already has a context keeps the more restrictive one: two SKUs on one ASIN
        # where either is out of stock is not an ASIN we should push harder.
        existing = out.get(asin)
        _apply_blocks(got, floor_days=floor_days, floor_box=floor_box)
        if existing is None or len(got.blocks) > len(existing.blocks):
            out[asin] = got

    for asin in (a.strip().upper() for a in (asins or [])):
        if asin not in out:
            # Advertised, but Phase 3 has never seen it. Not a block, but it is said out loud.
            out[asin] = ProductContext(asin=asin, known=False)
    return out


def _apply_blocks(got: ProductContext, *, floor_days: float, floor_box: float) -> None:
    if got.stock_is_stale:
        # Not "it is fine": we do not know. Spending more on a guess is the thing to avoid.
        hours = int((utcnow() - got.stock_as_of).total_seconds() // 3600) if got.stock_as_of else 0
        got.blocks.append(f"the last stock figure I have is {hours} hours old, so I can't tell "
                          "whether there is any")
    if not got.buyable:
        got.blocks.append(f"it isn't buyable right now ({got.suppressed_reason})")
    if got.fulfillable_qty is not None and got.fulfillable_qty <= 0:
        inbound = f", {got.inbound_qty} inbound" if got.inbound_qty else ""
        got.blocks.append(f"there's no sellable stock{inbound}")
    elif got.days_of_cover is not None and got.days_of_cover < floor_days:
        got.blocks.append(f"only {got.days_of_cover:g} days of stock left "
                          f"(under the {floor_days:g}-day floor)")
    if got.buy_box_pct is not None and got.buy_box_pct < floor_box:
        got.blocks.append(f"it only holds the Buy Box {got.buy_box_pct:.0f}% of the time, so the "
                          "extra traffic would partly go to another seller")


def for_asin(session: Session, profile: PpcProfile, asin: str, *,
             today: date) -> ProductContext:
    return build(session, profile, today=today, asins=[asin])[asin.strip().upper()]
