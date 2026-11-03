"""TACOS: advertising spend as a share of *total* sales, not just advertised sales.

ACOS asks "did this ad pay for itself". TACOS asks "is the business better off", and they disagree
often enough to matter: cutting bids always improves ACOS, and sometimes shrinks the total. So every
recommendation that lowers ACOS while total sales fall has to be flagged (brief 6.7).

    TACOS = ad spend ÷ total sales

Ad spend comes from `ppc_metrics_daily` (our own load) and total sales from `amazon_sales_daily`
(Phase 3's Sales & Traffic report), which is the whole point: one number from advertising, one from
the business, divided.

Two honesty rules are built in. Provisional days never reach a published figure, and a TACOS with no
sales underneath it is `None` rather than infinity — "we spent £40 and sold nothing" is a sentence,
not a ratio.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (AmazonListing, AmazonSalesDaily, Brand, PpcMetricsDaily, PpcProductAd,
                           PpcProfile)
from app.ppc import metrics

log = logging.getLogger(__name__)

WINDOWS = (7, 30)


@dataclass
class Tacos:
    """One scope over one window. `None` where the arithmetic would be a lie."""

    scope: str                      # account | brand | asin
    ref: str | None
    start: date
    end: date
    ad_spend: float = 0.0
    ad_sales: float = 0.0
    total_sales: float = 0.0
    units: int = 0
    label: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def tacos(self) -> float | None:
        return round(self.ad_spend / self.total_sales, 4) if self.total_sales else None

    @property
    def acos(self) -> float | None:
        return round(self.ad_spend / self.ad_sales, 4) if self.ad_sales else None

    @property
    def organic_sales(self) -> float:
        """What sold without an ad being clicked.

        Advertised sales above total sales is not possible in reality, so it means the two sources
        disagree — usually Phase 3's sales report not yet covering a day the Ads report does. It is
        clamped to zero *and* noted, because silently clamping hides the disagreement.
        """
        gap = round(self.total_sales - self.ad_sales, 2)
        if gap < 0:
            note = ("advertised sales are higher than total sales in this window, so the two "
                    "reports disagree — usually Phase 3's sales data lagging behind")
            if note not in self.notes:
                self.notes.append(note)
            return 0.0
        return gap

    @property
    def ad_share(self) -> float | None:
        return round(self.ad_sales / self.total_sales, 4) if self.total_sales else None

    def as_dict(self) -> dict[str, Any]:
        return {"scope": self.scope, "ref": self.ref, "label": self.label,
                "start": self.start.isoformat(), "end": self.end.isoformat(),
                "ad_spend": round(self.ad_spend, 2), "ad_sales": round(self.ad_sales, 2),
                "total_sales": round(self.total_sales, 2), "organic_sales": self.organic_sales,
                "tacos": self.tacos, "acos": self.acos, "ad_share": self.ad_share,
                "units": self.units, "notes": self.notes}


def window(profile: PpcProfile, days: int, *, today: date | None = None) -> tuple[date, date]:
    """The last `days` finalised days: ends where provisional data begins (brief 4.4)."""
    today = today or metrics.today_for(profile)
    end = today - timedelta(days=metrics.PROVISIONAL_DAYS)
    return end - timedelta(days=days - 1), end


def ad_spend(session: Session, profile: PpcProfile, start: date, end: date, *,
             asins: list[str] | None = None) -> tuple[float, float]:
    """Spend and advertised sales in a window, from finalised campaign rows.

    Campaign rows, deliberately: the campaign, ad group, target and advertised-product reports all
    carry the same cost, so adding them together would count the same spend three or four times.
    Per-ASIN figures come from the advertised-product rows, which are the only ones that name an ASIN.
    """
    if asins is not None and not asins:
        return 0.0, 0.0        # "these ASINs" with none named is zero, not "everything"
    entity_type = "asin" if asins else "campaign"
    q = select(func.coalesce(func.sum(PpcMetricsDaily.spend), 0),
               func.coalesce(func.sum(PpcMetricsDaily.sales), 0)).where(
        PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
        PpcMetricsDaily.entity_type == entity_type,
        PpcMetricsDaily.date >= start, PpcMetricsDaily.date <= end,
        PpcMetricsDaily.is_provisional.is_(False))
    if asins:
        q = q.where(PpcMetricsDaily.amazon_entity_id.in_(asins))
    spend, sales = session.execute(q).one()
    return float(spend or 0), float(sales or 0)


def total_sales(session: Session, profile: PpcProfile, start: date, end: date, *,
                asins: list[str] | None = None) -> tuple[float, int]:
    """Total sales in a window, counting each ASIN-day once.

    Phase 3's Sales & Traffic load is per ASIN and stores the SKU only as a label, so one ASIN-day is
    one figure. An older version of that loader could write a second row for the same ASIN and day
    under a different SKU label; summing rows blindly would then double the sales and halve TACOS.
    Taking one value per ASIN-day is correct for ASIN-level data and immune to that.
    """
    if asins is not None and not asins:
        return 0.0, 0
    per_day = select(AmazonSalesDaily.asin, AmazonSalesDaily.date,
                     func.max(AmazonSalesDaily.ordered_product_sales).label("sales"),
                     func.max(AmazonSalesDaily.units).label("units")).where(
        AmazonSalesDaily.marketplace_id == profile.marketplace_id,
        AmazonSalesDaily.date >= start, AmazonSalesDaily.date <= end)
    if asins:
        per_day = per_day.where(AmazonSalesDaily.asin.in_(asins))
    per_day = per_day.group_by(AmazonSalesDaily.asin, AmazonSalesDaily.date).subquery()
    sales, units = session.execute(select(
        func.coalesce(func.sum(per_day.c.sales), 0),
        func.coalesce(func.sum(per_day.c.units), 0))).one()
    return float(sales or 0), int(units or 0)


def for_account(session: Session, profile: PpcProfile, *, days: int = 30,
                today: date | None = None) -> Tacos:
    start, end = window(profile, days, today=today)
    spend, ad_sales = ad_spend(session, profile, start, end)
    sales, units = total_sales(session, profile, start, end)
    got = Tacos(scope="account", ref=None, label="the whole account", start=start, end=end,
                ad_spend=spend, ad_sales=ad_sales, total_sales=sales, units=units)
    if not sales:
        got.notes.append("no sales recorded for this window yet, so there is no TACOS to report")
    return got


def for_asin(session: Session, profile: PpcProfile, asin: str, *, days: int = 30,
             today: date | None = None) -> Tacos:
    start, end = window(profile, days, today=today)
    spend, ad_sales = ad_spend(session, profile, start, end, asins=[asin])
    sales, units = total_sales(session, profile, start, end, asins=[asin])
    got = Tacos(scope="asin", ref=asin, label=asin, start=start, end=end, ad_spend=spend,
                ad_sales=ad_sales, total_sales=sales, units=units)
    if spend and not ad_sales:
        got.notes.append("spend with no advertised sales in this window")
    return got


def _advertised_asins(session: Session, profile: PpcProfile, brand_id) -> list[str]:
    """ASINs with an ad, from the product ads' links back to Phase 3 listings."""
    rows = session.execute(select(AmazonListing.asin).join(
        PpcProductAd, PpcProductAd.listing_id == AmazonListing.id).where(
        PpcProductAd.profile_id == profile.id, AmazonListing.brand_id == brand_id,
        AmazonListing.asin.is_not(None)).distinct()).scalars()
    return [asin for asin in rows if asin]


def _brand_asins(session: Session, profile: PpcProfile, brand_id) -> list[str]:
    """Every ASIN the brand sells in this marketplace, advertised or not."""
    rows = session.execute(select(AmazonListing.asin).where(
        AmazonListing.marketplace_id == profile.marketplace_id,
        AmazonListing.brand_id == brand_id, AmazonListing.asin.is_not(None)).distinct()).scalars()
    return [asin for asin in rows if asin]


def for_brand(session: Session, profile: PpcProfile, brand: Brand, *, days: int = 30,
              today: date | None = None) -> Tacos:
    """A brand's TACOS: what advertising cost, over what the brand sold.

    The denominator is the brand's *whole* sales, not just the advertised products'. That is the
    point of TACOS — dividing by only the advertised products' sales would flatter a brand that
    advertises one ASIN out of ten, and would change the answer whenever an ad was switched on.
    """
    start, end = window(profile, days, today=today)
    advertised = _advertised_asins(session, profile, brand.id)
    all_asins = _brand_asins(session, profile, brand.id)
    got = Tacos(scope="brand", ref=str(brand.id), label=brand.name, start=start, end=end)
    if not all_asins:
        got.notes.append("no listings for this brand in this marketplace")
        return got
    if advertised:
        got.ad_spend, got.ad_sales = ad_spend(session, profile, start, end, asins=advertised)
    else:
        got.notes.append("nothing advertised for this brand yet")
    got.total_sales, got.units = total_sales(session, profile, start, end, asins=all_asins)
    return got


def daily_series(session: Session, profile: PpcProfile, *, days: int = 30,
                 today: date | None = None, asins: list[str] | None = None) -> list[dict[str, Any]]:
    """TACOS day by day, which is what a trend line and the weekly report are drawn from.

    Two grouped queries rather than one per day: a month of per-day round trips is thirty queries to
    answer one question.
    """
    start, end = window(profile, days, today=today)
    entity_type = "asin" if asins else "campaign"
    spend_q = select(PpcMetricsDaily.date,
                     func.coalesce(func.sum(PpcMetricsDaily.spend), 0),
                     func.coalesce(func.sum(PpcMetricsDaily.sales), 0)).where(
        PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
        PpcMetricsDaily.entity_type == entity_type,
        PpcMetricsDaily.date >= start, PpcMetricsDaily.date <= end,
        PpcMetricsDaily.is_provisional.is_(False)).group_by(PpcMetricsDaily.date)
    # Grouped by ASIN first for the same reason as `total_sales`: one figure per ASIN-day.
    per_asin_day = select(AmazonSalesDaily.asin, AmazonSalesDaily.date,
                          func.max(AmazonSalesDaily.ordered_product_sales).label("sales"),
                          func.max(AmazonSalesDaily.units).label("units")).where(
        AmazonSalesDaily.marketplace_id == profile.marketplace_id,
        AmazonSalesDaily.date >= start, AmazonSalesDaily.date <= end)
    if asins:
        spend_q = spend_q.where(PpcMetricsDaily.amazon_entity_id.in_(asins))
        per_asin_day = per_asin_day.where(AmazonSalesDaily.asin.in_(asins))
    grouped = per_asin_day.group_by(AmazonSalesDaily.asin, AmazonSalesDaily.date).subquery()
    sales_q = select(grouped.c.date, func.coalesce(func.sum(grouped.c.sales), 0),
                     func.coalesce(func.sum(grouped.c.units), 0)).group_by(grouped.c.date)

    spend = {row[0]: (float(row[1]), float(row[2])) for row in session.execute(spend_q)}
    sold = {row[0]: (float(row[1]), int(row[2])) for row in session.execute(sales_q)}
    out = []
    day = start
    while day <= end:
        ad_spend_day, ad_sales_day = spend.get(day, (0.0, 0.0))
        total, units = sold.get(day, (0.0, 0))
        got = Tacos(scope="asin" if asins else "account", ref=asins[0] if asins and len(asins) == 1
                    else None, start=day, end=day, ad_spend=ad_spend_day, ad_sales=ad_sales_day,
                    total_sales=total, units=units)
        out.append(got.as_dict())
        day += timedelta(days=1)
    return out


def overview(session: Session, profile: PpcProfile, *, today: date | None = None,
             windows: tuple[int, ...] = WINDOWS) -> dict[str, Any]:
    """Account and per-brand TACOS over each window — what the weekly report and `PPC` are built on."""
    brands = list(session.execute(select(Brand).order_by(Brand.name)).scalars())
    out: dict[str, Any] = {"profile": profile.amazon_profile_id, "currency": profile.currency,
                           "windows": {}}
    for days in windows:
        account = for_account(session, profile, days=days, today=today)
        out["windows"][days] = {
            "account": account.as_dict(),
            "brands": [for_brand(session, profile, b, days=days, today=today).as_dict()
                       for b in brands],
        }
    out["daily"] = daily_series(session, profile, days=max(windows), today=today)
    return out


def trend(session: Session, profile: PpcProfile, *, days: int = 30,
          today: date | None = None) -> dict[str, Any]:
    """This window against the one before it, so a change has something to be a change from."""
    start, end = window(profile, days, today=today)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    now_spend, now_ad_sales = ad_spend(session, profile, start, end)
    now_total, _units = total_sales(session, profile, start, end)
    was_spend, was_ad_sales = ad_spend(session, profile, previous_start, previous_end)
    was_total, _was_units = total_sales(session, profile, previous_start, previous_end)
    now = Tacos(scope="account", ref=None, start=start, end=end, ad_spend=now_spend,
                ad_sales=now_ad_sales, total_sales=now_total)
    before = Tacos(scope="account", ref=None, start=previous_start, end=previous_end,
                   ad_spend=was_spend, ad_sales=was_ad_sales, total_sales=was_total)
    return {"now": now.as_dict(), "before": before.as_dict(),
            "sales_change": round(now_total - was_total, 2),
            "spend_change": round(now_spend - was_spend, 2),
            "tacos_change": (round(now.tacos - before.tacos, 4)
                             if now.tacos is not None and before.tacos is not None else None)}


def shrinking_the_business(now: Tacos, before: Tacos, *, tolerance: float = 0.02,
                           acos_tolerance: float = 0.005) -> bool:
    """True when ACOS improved but total sales fell — the trade the brief says to flag.

    A better ACOS bought by selling less is not an improvement, and it is exactly what a naive
    optimiser produces. Anything proposing it has to say so out loud.

    The condition is "sales fell materially and ACOS did **not** get materially worse". Not "ACOS
    improved": ACOS holding while the business shrinks is the same bad trade, and requiring a visible
    improvement let a 10% drop in sales through whenever ACOS happened to be flat. ACOS getting
    clearly worse *and* sales falling is a different problem — paying more for less — and other
    analysers own it, so it is deliberately not this flag.

    A collapse to no advertised sales at all still counts, on the sales alone: there is no ACOS to
    compare, and the early `None` check that used to sit here returned False for the worst outcome
    there is.
    """
    if not before.total_sales:
        return False
    lost_sales = (before.total_sales - now.total_sales) / before.total_sales > tolerance
    if not lost_sales:
        return False
    if now.acos is None:
        # Still spending, and nothing attributed to it: the worst version of this trade.
        return now.ad_spend > 0
    if before.acos is None:
        return False
    return now.acos <= before.acos + acos_tolerance
