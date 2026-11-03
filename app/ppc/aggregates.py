"""Summing finalised metrics over a window, in SQL, once per analysis run.

Every analyser needs the same shapes — a target's last 30 days, a campaign's last 7, a search term's
totals — so they are computed here in a handful of grouped queries and handed round as plain objects.
The alternative, each analyser querying per entity, is what turns a 2,000-target account into 20,000
round trips.

Two rules hold everywhere in this module:

* **Finalised data only.** Provisional days are excluded by `metrics.final_only`, so nothing the agent
  proposes rests on numbers Amazon is still validating (brief 4.4).
* **Derived metrics are computed, never read.** ACOS, CVR, CPC and RPC are properties on the rollup,
  from the stored counts, so there is exactly one definition of each in the codebase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (PpcMetricsDaily, PpcPlacement, PpcProfile, PpcSearchTerm,
                           PpcSearchTermDecision, PpcTarget)
from app.ppc import metrics

log = logging.getLogger(__name__)


@dataclass
class Rollup:
    """One entity's totals over a window, with every derived figure computed from them."""

    entity_type: str
    entity_id: str
    days: int
    start: date
    end: date
    impressions: int = 0
    clicks: int = 0
    spend: float = 0.0
    sales: float = 0.0
    orders: int = 0
    units: int = 0
    campaign_id: str | None = None
    ad_group_id: str | None = None
    days_with_data: int = 0

    @property
    def acos(self) -> float | None:
        """Spend over sales. `None` with no sales: an infinite ACOS is not a number to compare."""
        return round(self.spend / self.sales, 4) if self.sales else None

    @property
    def cvr(self) -> float | None:
        return round(self.orders / self.clicks, 4) if self.clicks else None

    @property
    def cpc(self) -> float | None:
        return round(self.spend / self.clicks, 4) if self.clicks else None

    @property
    def rpc(self) -> float | None:
        """Revenue per click — what a click is actually worth, which is what a bid should follow."""
        return round(self.sales / self.clicks, 4) if self.clicks else None

    @property
    def ctr(self) -> float | None:
        return round(self.clicks / self.impressions, 4) if self.impressions else None

    @property
    def aov(self) -> float | None:
        return round(self.sales / self.orders, 2) if self.orders else None

    def as_dict(self) -> dict[str, Any]:
        return {"window_days": self.days, "from": self.start.isoformat(), "to": self.end.isoformat(),
                "impressions": self.impressions, "clicks": self.clicks,
                "spend": round(self.spend, 2), "sales": round(self.sales, 2),
                "orders": self.orders, "units": self.units, "acos": self.acos, "cvr": self.cvr,
                "cpc": self.cpc, "rpc": self.rpc, "days_with_data": self.days_with_data}


def window(profile: PpcProfile, days: int, *, today: date | None = None) -> tuple[date, date]:
    """The last `days` finalised days, ending where provisional data begins."""
    today = today or metrics.today_for(profile)
    end = today - timedelta(days=metrics.PROVISIONAL_DAYS)
    return end - timedelta(days=days - 1), end


def by_entity(session: Session, profile: PpcProfile, entity_type: str, *, ad_product: str,
              days: int = 30,
              today: date | None = None,
              entity_ids: list[str] | None = None) -> dict[str, Rollup]:
    """Totals per entity of one type over the window. One query."""
    start, end = window(profile, days, today=today)
    q = select(PpcMetricsDaily.amazon_entity_id,
               func.sum(PpcMetricsDaily.impressions), func.sum(PpcMetricsDaily.clicks),
               func.sum(PpcMetricsDaily.spend), func.sum(PpcMetricsDaily.sales),
               func.sum(PpcMetricsDaily.orders), func.sum(PpcMetricsDaily.units),
               func.count(PpcMetricsDaily.date),
               func.max(PpcMetricsDaily.amazon_campaign_id),
               func.max(PpcMetricsDaily.amazon_ad_group_id)).where(
        PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
        PpcMetricsDaily.ad_product == ad_product,
        PpcMetricsDaily.entity_type == entity_type,
        PpcMetricsDaily.date >= start, PpcMetricsDaily.date <= end)
    q = metrics.final_only(q)
    if entity_ids:
        q = q.where(PpcMetricsDaily.amazon_entity_id.in_(entity_ids))
    out: dict[str, Rollup] = {}
    for row in session.execute(q.group_by(PpcMetricsDaily.amazon_entity_id)):
        out[row[0]] = Rollup(entity_type=entity_type, entity_id=row[0], days=days, start=start,
                             end=end, impressions=int(row[1] or 0), clicks=int(row[2] or 0),
                             spend=float(row[3] or 0), sales=float(row[4] or 0),
                             orders=int(row[5] or 0), units=int(row[6] or 0),
                             days_with_data=int(row[7] or 0), campaign_id=row[8], ad_group_id=row[9])
    return out


def per_day(session: Session, profile: PpcProfile, entity_type: str, entity_id: str, *,
            ad_product: str,
            days: int = 30, today: date | None = None) -> list[PpcMetricsDaily]:
    """One entity's individual days, for anything that needs the shape rather than the total."""
    start, end = window(profile, days, today=today)
    q = select(PpcMetricsDaily).where(
        PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
        PpcMetricsDaily.ad_product == ad_product,
        PpcMetricsDaily.entity_type == entity_type,
        PpcMetricsDaily.amazon_entity_id == entity_id,
        PpcMetricsDaily.date >= start, PpcMetricsDaily.date <= end)
    return list(session.execute(metrics.final_only(q).order_by(PpcMetricsDaily.date)).scalars())


def days_by_entity(session: Session, profile: PpcProfile, entity_type: str, *, ad_product: str,
                   days: int = 30,
                   today: date | None = None) -> dict[str, list[PpcMetricsDaily]]:
    """Every entity's individual days, grouped, in one query.

    `per_day` answers for one entity, which is right for a single lookup and wrong inside a loop —
    the budget analyser needs the daily shape for every campaign, and asking per campaign was one
    round trip each.
    """
    start, end = window(profile, days, today=today)
    q = select(PpcMetricsDaily).where(
        PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
        PpcMetricsDaily.ad_product == ad_product,
        PpcMetricsDaily.entity_type == entity_type,
        PpcMetricsDaily.date >= start, PpcMetricsDaily.date <= end)
    out: dict[str, list[PpcMetricsDaily]] = {}
    for row in session.execute(metrics.final_only(q).order_by(PpcMetricsDaily.date)).scalars():
        out.setdefault(row.amazon_entity_id, []).append(row)
    return out


@dataclass
class TermRollup:
    """A search term's totals in one ad group, plus the decision already taken about it."""

    search_term: str
    ad_group_id: str
    campaign_id: str | None
    target_id: str | None
    days: int
    start: date
    end: date
    impressions: int = 0
    clicks: int = 0
    spend: float = 0.0
    sales: float = 0.0
    orders: int = 0
    units: int = 0
    match_type: str | None = None
    harvest_status: str = "new"

    acos = Rollup.acos
    cvr = Rollup.cvr
    cpc = Rollup.cpc
    rpc = Rollup.rpc
    aov = Rollup.aov

    def as_dict(self) -> dict[str, Any]:
        return {"search_term": self.search_term, "window_days": self.days,
                "from": self.start.isoformat(), "to": self.end.isoformat(),
                "impressions": self.impressions, "clicks": self.clicks,
                "spend": round(self.spend, 2), "sales": round(self.sales, 2),
                "orders": self.orders, "acos": self.acos, "cvr": self.cvr, "cpc": self.cpc,
                "harvest_status": self.harvest_status}


def search_terms(session: Session, profile: PpcProfile, *, ad_product: str, days: int = 30,
                 today: date | None = None, min_clicks: int = 1) -> list[TermRollup]:
    """Every search term's totals per ad group, with its harvest decision attached.

    Grouped by ad group as well as term because the same term in two ad groups is two decisions:
    harvesting it into one does not answer the other.
    """
    start, end = window(profile, days, today=today)
    q = select(PpcSearchTerm.search_term, PpcSearchTerm.amazon_ad_group_id,
               func.max(PpcSearchTerm.amazon_campaign_id), func.max(PpcSearchTerm.amazon_target_id),
               func.sum(PpcSearchTerm.impressions), func.sum(PpcSearchTerm.clicks),
               func.sum(PpcSearchTerm.spend), func.sum(PpcSearchTerm.sales),
               func.sum(PpcSearchTerm.orders), func.sum(PpcSearchTerm.units),
               func.max(PpcSearchTerm.match_type)).where(
        PpcSearchTerm.amazon_profile_id == profile.amazon_profile_id,
        PpcSearchTerm.ad_product == ad_product,
        PpcSearchTerm.date >= start, PpcSearchTerm.date <= end,
        PpcSearchTerm.is_provisional.is_(False)).group_by(
        PpcSearchTerm.search_term, PpcSearchTerm.amazon_ad_group_id)
    rows = list(session.execute(q))

    # Three columns, for the terms in this window only. `ppc_search_term_decisions` holds one row per
    # (ad group, term) ever decided and only grows, so hydrating the lot as ORM objects to read a
    # status was hundreds of thousands of objects a run on a busy account.
    wanted_groups = {row[1] for row in rows}
    wanted_terms = {row[0] for row in rows}
    decisions: dict[tuple[str, str], str] = {}
    if wanted_terms:
        for group, term, status in session.execute(select(
                PpcSearchTermDecision.amazon_ad_group_id, PpcSearchTermDecision.search_term,
                PpcSearchTermDecision.harvest_status).where(
                PpcSearchTermDecision.amazon_profile_id == profile.amazon_profile_id,
        PpcSearchTermDecision.ad_product == ad_product,
                PpcSearchTermDecision.amazon_ad_group_id.in_(wanted_groups),
                PpcSearchTermDecision.search_term.in_(wanted_terms))):
            decisions[(group, term)] = status
    out = []
    for row in rows:
        if int(row[5] or 0) < min_clicks:
            continue
        out.append(TermRollup(
            search_term=row[0], ad_group_id=row[1], campaign_id=row[2], target_id=row[3],
            days=days, start=start, end=end, impressions=int(row[4] or 0), clicks=int(row[5] or 0),
            spend=float(row[6] or 0), sales=float(row[7] or 0), orders=int(row[8] or 0),
            units=int(row[9] or 0), match_type=row[10],
            harvest_status=decisions.get((row[1], row[0]), "new")))
    return out


@dataclass
class PlacementRollup:
    campaign_id: str
    placement: str
    days: int
    start: date
    end: date
    impressions: int = 0
    clicks: int = 0
    spend: float = 0.0
    sales: float = 0.0
    orders: int = 0

    acos = Rollup.acos
    cvr = Rollup.cvr
    cpc = Rollup.cpc

    def as_dict(self) -> dict[str, Any]:
        return {"placement": self.placement, "window_days": self.days,
                "clicks": self.clicks, "spend": round(self.spend, 2),
                "sales": round(self.sales, 2), "orders": self.orders, "acos": self.acos,
                "cvr": self.cvr}


def placements(session: Session, profile: PpcProfile, *, ad_product: str, days: int = 30,
               today: date | None = None) -> dict[str, list[PlacementRollup]]:
    """Placement totals per campaign, so top-of-search can be compared with the rest."""
    start, end = window(profile, days, today=today)
    rows = session.execute(select(
        PpcPlacement.amazon_campaign_id, PpcPlacement.placement,
        func.sum(PpcPlacement.impressions), func.sum(PpcPlacement.clicks),
        func.sum(PpcPlacement.spend), func.sum(PpcPlacement.sales),
        func.sum(PpcPlacement.orders)).where(
        PpcPlacement.amazon_profile_id == profile.amazon_profile_id,
        PpcPlacement.ad_product == ad_product,
        PpcPlacement.date >= start, PpcPlacement.date <= end,
        PpcPlacement.is_provisional.is_(False)).group_by(
        PpcPlacement.amazon_campaign_id, PpcPlacement.placement)).all()
    out: dict[str, list[PlacementRollup]] = {}
    for row in rows:
        out.setdefault(row[0], []).append(PlacementRollup(
            campaign_id=row[0], placement=row[1], days=days, start=start, end=end,
            impressions=int(row[2] or 0), clicks=int(row[3] or 0), spend=float(row[4] or 0),
            sales=float(row[5] or 0), orders=int(row[6] or 0)))
    return out


def targets(session: Session, profile: PpcProfile, *, ad_product: str,
            states: tuple[str, ...] = ("enabled",)) -> dict[str, PpcTarget]:
    """The live targets, keyed by Amazon's id, so a rollup can be joined to what it is."""
    rows = session.execute(select(PpcTarget).where(
        PpcTarget.profile_id == profile.id, PpcTarget.state.in_(states))).scalars()
    return {row.amazon_target_id: row for row in rows}


def asin_for_ad_group(session: Session, profile: PpcProfile, *,
                      ad_product: str) -> dict[str, list[str]]:
    """Which ASINs each ad group advertises — how a target reaches its product's economics."""
    from app.db.models import PpcProductAd

    rows = session.execute(select(PpcProductAd.amazon_ad_id, PpcProductAd.asin,
                                  PpcProductAd.ad_group_id).where(
        PpcProductAd.profile_id == profile.id, PpcProductAd.state != "archived",
        PpcProductAd.asin.is_not(None))).all()
    from app.db.models import PpcAdGroup

    groups = {g.id: g.amazon_ad_group_id for g in session.execute(select(PpcAdGroup).where(
        PpcAdGroup.profile_id == profile.id)).scalars()}
    out: dict[str, list[str]] = {}
    for _ad_id, asin, ad_group_id in rows:
        key = groups.get(ad_group_id)
        if key and asin not in out.setdefault(key, []):
            out[key].append(asin)
    return out
