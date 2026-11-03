"""Storing metrics: upsert a day, notice when Amazon restates it, and mark what is still moving.

Two rules from the brief (4.4) are enforced here and nowhere else:

* **Restatements are visible.** Sponsored Products sales keep changing for days after the fact. When
  a re-pull overwrites a number we already had, the old and new figures are written to
  `ppc_metrics_restatements` — otherwise the account's history would quietly rewrite itself.
* **Provisional days are marked.** The most recent days are flagged, recomputed on every load, and
  no learning may read them. `final_only()` is how every analyser asks for data.

`is_provisional` is derived from the load date each time, so a day that has settled stops being
provisional the next morning without anything having to go back and clear a flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import (PpcMetricsDaily, PpcMetricsRestatement, PpcPlacement, PpcProfile,
                           PpcPurchasedProduct, PpcSearchTerm, PpcSearchTermDecision)
from app.services.common import utcnow

log = logging.getLogger(__name__)

# Sponsored Products data settles on two clocks, and they are weeks apart. Both are respected here
# because conflating them is how an agent ends up "learning" from numbers Amazon hadn't finished:
#
#   Traffic (impressions, clicks): available in 12 hours, still moving for about 3 days while
#   Amazon validates it. This is the brief's 3-day provisional flag, and it is the gate on
#   recommendations — a bid decision must not rest on this morning's unvalidated clicks.
#
#   Conversions (orders, sales): restated 1, 7 and 28 days after the conversion event. Because a
#   conversion is reported against the date of the ad click, a row dated D can still change until
#   D + attribution window + 28 — 35 days for the 7-day column, by Amazon's own arithmetic. That is
#   the gate on *conclusions*: experiment results and calibration scoring wait for `is_settled`.
#
# Marking everything inside 35 days provisional was the other option and it is the wrong one: it
# would stop the agent acting on the last five weeks, which is all the data that matters day to day.
PROVISIONAL_DAYS = 3
ATTRIBUTION_WINDOW_DAYS = 7
CONVERSION_RESTATEMENT_DAYS = 28
SETTLED_DAYS = ATTRIBUTION_WINDOW_DAYS + CONVERSION_RESTATEMENT_DAYS      # 35
# How far back the daily load re-reads and overwrites (brief 4.4).
RESTATEMENT_DAYS = 14
# The weekly deeper re-pull, wide enough to catch the last conversion restatement. Amazon allows 31
# days per request, so this is two requests — which is why it is weekly rather than daily.
DEEP_RESTATEMENT_DAYS = 42
# Below this, a difference is Amazon's rounding rather than a restatement worth recording.
MATERIAL_MONEY = 0.01
# Postgres binds at most 65,535 parameters in one statement, and a metrics row binds about 25 of
# them, so one INSERT per report died at roughly 2,600 rows — which a fortnight of a couple of
# hundred keywords already exceeds. Every bulk write goes out in batches of this size.
INSERT_BATCH = 500

MONEY_FIELDS = ("spend", "sales")
COUNT_FIELDS = ("clicks", "orders")


# ---------------- dates in the profile's own time zone ----------------
def today_for(profile: PpcProfile) -> date:
    """Amazon reports in the profile's marketplace time zone, so "yesterday" means theirs, not ours.

    Loading UTC-yesterday from Los Angeles would ask for a day that hasn't finished there yet.
    """
    return datetime.now(zone_for(profile)).date()


def zone_for(profile: PpcProfile) -> ZoneInfo:
    """The profile's own time zone, falling back loudly rather than silently to UTC."""
    try:
        return ZoneInfo(profile.timezone or "America/Los_Angeles")
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown profile time zone %r; using America/Los_Angeles", profile.timezone)
        return ZoneInfo("America/Los_Angeles")


def now_for(profile: PpcProfile) -> datetime:
    """Local wall-clock time in the marketplace. Amazon's budget day resets at local midnight, and
    an hour labelled 14 means 2pm there, so every intraday judgement has to be made in this clock."""
    return datetime.now(zone_for(profile))


SYMBOLS = {"USD": "$", "CAD": "C$", "GBP": "£", "EUR": "€", "MXN": "MX$", "JPY": "¥"}


def money(value: float | None, currency: str = "USD", *, places: int = 2,
          absolute: bool = False) -> str:
    """A figure the owner can read, in the marketplace's own currency.

    One formatter, because the account spans currencies and "$40" where the campaign spent 40 Canadian
    dollars is wrong in a way that compounds every time it is quoted.
    """
    if value is None:
        return "—"
    amount = abs(float(value)) if absolute else float(value)
    return f"{SYMBOLS.get(currency, '')}{amount:,.{places}f}"


def clock_of(hour: int) -> str:
    """"2pm" for 14. Spelled out rather than `%-I%p`, which is not portable off glibc and BSD.

    Lives here with `money` and `zone_for` because four modules were formatting an hour by hand and
    one of them got noon wrong.
    """
    hour %= 24
    return f"{hour % 12 or 12}{'am' if hour < 12 else 'pm'}"


def is_provisional(day: date, today: date) -> bool:
    """Still moving: traffic for this day hasn't finished being validated."""
    return (today - day).days < PROVISIONAL_DAYS


def is_settled(day: date, today: date) -> bool:
    """Amazon has finished restating this day's conversions. Required before concluding anything."""
    return (today - day).days >= SETTLED_DAYS


def settled_before(today: date) -> date:
    """The most recent date whose conversions Amazon has finished restating."""
    return today - timedelta(days=SETTLED_DAYS)


def window(profile: PpcProfile, *, days: int = RESTATEMENT_DAYS,
           today: date | None = None) -> tuple[date, date]:
    """The re-pull window: yesterday back `days`, in the profile's time zone."""
    today = today or today_for(profile)
    end = today - timedelta(days=1)
    return end - timedelta(days=days - 1), end


# ---------------- one metric row, as the loaders hand it over ----------------
@dataclass
class MetricRow:
    entity_type: str
    amazon_entity_id: str
    date: date
    impressions: int = 0
    clicks: int = 0
    spend: float = 0.0
    sales: float = 0.0
    orders: int = 0
    units: int = 0
    amazon_campaign_id: str | None = None
    amazon_ad_group_id: str | None = None
    same_sku_sales: float | None = None
    other_sku_sales: float | None = None
    impression_share: float | None = None
    top_of_search_impression_share: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str, date]:
        return (self.entity_type, self.amazon_entity_id, self.date)


@dataclass
class LoadStats:
    stored: int = 0
    restated: int = 0
    provisional: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"stored": self.stored, "restated": self.restated,
                "provisional": self.provisional, "skipped": self.skipped}


ADDITIVE = ("impressions", "clicks", "spend", "sales", "orders", "units",
            "same_sku_sales", "other_sku_sales")
# Percentages cannot be added. They are only ever collapsed in the degenerate case below, where the
# entity is an ASIN rather than an id, and the largest share is the least misleading answer.
PERCENTAGES = ("impression_share", "top_of_search_impression_share")


def collapse(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Merge rows that share a conflict key, because Postgres refuses to update one row twice.

    Amazon can legitimately send two rows for one key in a single report: `spAdvertisedProduct`
    groups by advertiser, so one ASIN promoted in two campaigns arrives twice for the same day, and
    when Amazon omits `adId` the ASIN *is* our entity id. Left alone, that raised
    "ON CONFLICT DO UPDATE command cannot affect row a second time" and lost the whole day's load —
    every report in the run, not just the offending one.

    Additive metrics are summed, which is what an ASIN-level row means. Anything identifying a single
    parent is dropped when the merged rows disagree, because the row is no longer about one campaign.
    """
    merged: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = tuple(row.get(k) for k in keys)
        first = merged.get(key)
        if first is None:
            merged[key] = dict(row)
            continue
        for name in ADDITIVE:
            if name in first:
                left, right = first.get(name), row.get(name)
                if left is None and right is None:
                    continue
                first[name] = (left or 0) + (right or 0)
        for name in PERCENTAGES:
            if name in first:
                values = [v for v in (first.get(name), row.get(name)) if v is not None]
                first[name] = max(values) if values else None
        for name in ("amazon_campaign_id", "amazon_ad_group_id", "advertised_sku", "match_type"):
            if name in first and first.get(name) != row.get(name):
                first[name] = None
    return list(merged.values())


def insert_many(session: Session, model, rows: list[dict[str, Any]], *,
                keys: tuple[str, ...] | None = None) -> int:
    """Insert or overwrite rows in batches small enough for Postgres to bind.

    With `keys`, the batch is an upsert on those columns; without, a plain insert. Rows sharing a
    conflict key are merged by `collapse` before they get here — Postgres refuses to update one row
    twice in a single statement.
    """
    if not rows:
        return 0
    for start in range(0, len(rows), INSERT_BATCH):
        batch = rows[start:start + INSERT_BATCH]
        stmt = insert(model).values(batch)
        if keys:
            stmt = stmt.on_conflict_do_update(
                index_elements=list(keys),
                set_={c: stmt.excluded[c] for c in batch[0] if c not in keys})
        session.execute(stmt)
    return len(rows)


def _changed(old: Any, new_row: dict[str, Any]) -> bool:
    for name in MONEY_FIELDS:
        if abs(float(getattr(old, name) or 0) - float(new_row.get(name) or 0)) >= MATERIAL_MONEY:
            return True
    for name in COUNT_FIELDS:
        if int(getattr(old, name) or 0) != int(new_row.get(name) or 0):
            return True
    return False


def _restatement(profile: PpcProfile, old: Any, new_row: dict[str, Any], *, ad_product: str,
                 entity_type: str | None = None, entity_id: str | None = None) -> dict[str, Any]:
    """One row of the audit trail. The side tables name their own entity, since they have no id."""
    return {"amazon_profile_id": profile.amazon_profile_id, "ad_product": ad_product,
            "entity_type": entity_type or new_row["entity_type"],
            "amazon_entity_id": entity_id or new_row["amazon_entity_id"], "date": new_row["date"],
            "old_spend": old.spend, "old_sales": old.sales, "old_orders": old.orders,
            "old_clicks": old.clicks, "new_spend": new_row.get("spend"),
            "new_sales": new_row.get("sales"), "new_orders": new_row.get("orders"),
            "new_clicks": new_row.get("clicks"), "old_report_id": old.report_id,
            "new_report_id": new_row.get("report_id"), "restated_at": utcnow()}


def load_daily(session: Session, profile: PpcProfile, rows: list[MetricRow], *, ad_product: str,
               report_id: str | None = None, report_start: date | None = None,
               report_end: date | None = None, attribution: dict[str, Any] | None = None,
               today: date | None = None) -> LoadStats:
    """Store a report's rows, overwriting whatever was there and recording what changed.

    The rows already in the database are read once for the whole batch, not once per row, so a
    fourteen-day re-pull of a large account is two queries and one upsert per report.
    """
    stats = LoadStats()
    if not rows:
        return stats
    today = today or today_for(profile)
    pulled_at = utcnow()

    existing = _existing_daily(session, profile, rows)
    payload: list[dict[str, Any]] = []
    restatements: list[dict[str, Any]] = []
    for row in rows:
        if not row.amazon_entity_id or row.date is None:
            stats.skipped += 1
            continue
        provisional = is_provisional(row.date, today)
        record = {
            "amazon_profile_id": profile.amazon_profile_id, "ad_product": ad_product,
            "marketplace_id": profile.marketplace_id,
            "entity_type": row.entity_type, "amazon_entity_id": str(row.amazon_entity_id),
            "amazon_campaign_id": row.amazon_campaign_id,
            "amazon_ad_group_id": row.amazon_ad_group_id, "date": row.date,
            "impressions": int(row.impressions or 0), "clicks": int(row.clicks or 0),
            "spend": round(float(row.spend or 0), 2), "sales": round(float(row.sales or 0), 2),
            "orders": int(row.orders or 0), "units": int(row.units or 0),
            "same_sku_sales": row.same_sku_sales, "other_sku_sales": row.other_sku_sales,
            "impression_share": row.impression_share,
            "top_of_search_impression_share": row.top_of_search_impression_share,
            "currency": profile.currency, "is_provisional": provisional,
            "report_id": report_id, "report_start_date": report_start, "report_end_date": report_end,
            "attribution": attribution, "pulled_at": pulled_at,
        }
        payload.append(record)
        stats.provisional += 1 if provisional else 0

    if payload:
        keys = ("amazon_profile_id", "ad_product", "entity_type", "amazon_entity_id", "date")
        payload = collapse(payload, keys)
        # Compared after collapsing, so a restatement is measured against the total we will store.
        for record in payload:
            was = existing.get((record["entity_type"], record["amazon_entity_id"], record["date"]))
            if was is not None and _changed(was, record):
                restatements.append(_restatement(profile, was, record, ad_product=ad_product))
        # Conflicts are named by their columns rather than by a constraint name, so a renamed
        # constraint can't turn "overwrite the day" into "insert it twice".
        stats.stored = insert_many(session, PpcMetricsDaily, payload, keys=keys)
    if restatements:
        stats.restated = insert_many(session, PpcMetricsRestatement, restatements)
    session.flush()
    return stats


def _existing_daily(session: Session, profile: PpcProfile,
                    rows: list[MetricRow]) -> dict[tuple[str, str, date], Any]:
    """Whatever we already hold for the days in this batch, in one query.

    Deliberately not filtered by entity id: a large account's fortnight is tens of thousands of
    targets, and an `IN` list that long is a worse query than reading the same date range by type.
    The rows fetched are bounded by what is about to be written anyway.
    """
    days = [r.date for r in rows if r.date]
    types = {r.entity_type for r in rows}
    if not days:
        return {}
    found = session.execute(select(PpcMetricsDaily).where(
        PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
        PpcMetricsDaily.entity_type.in_(types),
        PpcMetricsDaily.date >= min(days), PpcMetricsDaily.date <= max(days))).scalars()
    return {(r.entity_type, r.amazon_entity_id, r.date): r for r in found}


# ---------------- the tables that hold their own shape ----------------
def _note_restatements(session: Session, model, payload: list[dict[str, Any]], *,
                       profile: PpcProfile, ad_product: str, keys: tuple[str, ...],
                       entity_type: str, identify) -> int:
    """Log what a re-pull changed in one of the tables that holds its own shape.

    Search terms, placements and purchased products are overwritten by the daily re-pull exactly as
    the metrics table is, so they need the same audit trail — acceptance 1 asks for restatements to
    be logged, not for them to be logged in one table.
    """
    if not payload:
        return 0
    days = [r["date"] for r in payload if r.get("date")]
    if not days:
        return 0
    before = session.execute(select(model).where(
        model.amazon_profile_id == profile.amazon_profile_id,
        model.ad_product == ad_product,
        model.date >= min(days), model.date <= max(days))).scalars()
    held = {tuple(getattr(row, k) for k in keys): row for row in before}
    changes = []
    for record in payload:
        was = held.get(tuple(record.get(k) for k in keys))
        if was is not None and _changed(was, record):
            changes.append(_restatement(profile, was, record, ad_product=ad_product,
                                        entity_type=entity_type,
                                        entity_id=identify(record)[:300]))
    return insert_many(session, PpcMetricsRestatement, changes)


def load_search_terms(session: Session, profile: PpcProfile, rows: list[dict[str, Any]], *,
                      ad_product: str,
                      report_id: str | None = None, today: date | None = None) -> LoadStats:
    """Search term rows, plus a decision row per term so a harvest decision survives the daily churn."""
    stats = LoadStats()
    if not rows:
        return stats
    today = today or today_for(profile)
    pulled_at = utcnow()
    payload = []
    for row in rows:
        if not (row.get("search_term") and row.get("date") and row.get("amazon_ad_group_id")):
            stats.skipped += 1
            continue
        provisional = is_provisional(row["date"], today)
        payload.append({
            "amazon_profile_id": profile.amazon_profile_id, "ad_product": ad_product,
            "marketplace_id": profile.marketplace_id,
            "amazon_campaign_id": row.get("amazon_campaign_id") or "",
            "amazon_ad_group_id": row["amazon_ad_group_id"],
            # Empty rather than NULL: Postgres treats NULLs as distinct, so a null target id here
            # would insert the same term twice a day for ever instead of overwriting it.
            "amazon_target_id": row.get("amazon_target_id") or "",
            "search_term": row["search_term"][:1000], "match_type": row.get("match_type"),
            "date": row["date"], "impressions": int(row.get("impressions") or 0),
            "clicks": int(row.get("clicks") or 0), "spend": round(float(row.get("spend") or 0), 2),
            "sales": round(float(row.get("sales") or 0), 2), "orders": int(row.get("orders") or 0),
            "units": int(row.get("units") or 0),
            "search_term_impression_share": row.get("search_term_impression_share"),
            "search_term_impression_rank": row.get("search_term_impression_rank"),
            "currency": profile.currency, "is_provisional": provisional, "report_id": report_id,
            "pulled_at": pulled_at,
        })
        stats.provisional += 1 if provisional else 0
    if payload:
        keys = ("amazon_profile_id", "ad_product", "amazon_ad_group_id", "amazon_target_id",
                "search_term", "date")
        payload = collapse(payload, keys)
        stats.restated = _note_restatements(
            session, PpcSearchTerm, payload, ad_product=ad_product, keys=keys, entity_type="search_term", profile=profile,
            identify=lambda r: f"{r['amazon_ad_group_id']}|{r['search_term']}")
        stats.stored = insert_many(session, PpcSearchTerm, payload, keys=keys)
        _ensure_decisions(session, profile, payload, ad_product=ad_product)
    session.flush()
    return stats


def _ensure_decisions(session: Session, profile: PpcProfile, payload: list[dict[str, Any]], *,
                      ad_product: str) -> None:
    """Every term gets exactly one decision row, starting at `new`, never reset by a later load.

    Read with two `IN` lists rather than one clause per term: a fortnight of search terms for a real
    account is thousands of rows, and an OR chain that long is a query Postgres has to plan clause by
    clause. Over-fetching a few rows and filtering here is cheaper and has a fixed shape.
    """
    pairs = {(r["amazon_ad_group_id"], r["search_term"]): r.get("amazon_campaign_id")
             for r in payload}
    if not pairs:
        return
    groups = {group for group, _term in pairs}
    terms = {term for _group, term in pairs}
    have = set(session.execute(select(
        PpcSearchTermDecision.amazon_ad_group_id, PpcSearchTermDecision.search_term).where(
        PpcSearchTermDecision.amazon_profile_id == profile.amazon_profile_id,
        PpcSearchTermDecision.ad_product == ad_product,
        PpcSearchTermDecision.amazon_ad_group_id.in_(groups),
        PpcSearchTermDecision.search_term.in_(terms))).all())
    fresh = [{"amazon_profile_id": profile.amazon_profile_id, "ad_product": ad_product,
              "amazon_ad_group_id": group, "amazon_campaign_id": campaign, "search_term": term,
              "harvest_status": "new"}
             for (group, term), campaign in pairs.items() if (group, term) not in have]
    for start in range(0, len(fresh), INSERT_BATCH):
        session.execute(insert(PpcSearchTermDecision).values(
            fresh[start:start + INSERT_BATCH]).on_conflict_do_nothing(
            index_elements=["amazon_profile_id", "ad_product", "amazon_ad_group_id",
                            "search_term"]))


def load_placements(session: Session, profile: PpcProfile, rows: list[dict[str, Any]], *,
                    ad_product: str,
                    report_id: str | None = None, today: date | None = None) -> LoadStats:
    stats = LoadStats()
    if not rows:
        return stats
    today = today or today_for(profile)
    pulled_at = utcnow()
    payload = []
    for row in rows:
        if not (row.get("amazon_campaign_id") and row.get("placement") and row.get("date")):
            stats.skipped += 1
            continue
        provisional = is_provisional(row["date"], today)
        payload.append({
            "amazon_profile_id": profile.amazon_profile_id, "ad_product": ad_product,
            "marketplace_id": profile.marketplace_id,
            "amazon_campaign_id": row["amazon_campaign_id"], "placement": row["placement"][:40],
            "date": row["date"], "impressions": int(row.get("impressions") or 0),
            "clicks": int(row.get("clicks") or 0), "spend": round(float(row.get("spend") or 0), 2),
            "sales": round(float(row.get("sales") or 0), 2), "orders": int(row.get("orders") or 0),
            "units": int(row.get("units") or 0), "currency": profile.currency,
            "is_provisional": provisional, "report_id": report_id, "pulled_at": pulled_at,
        })
        stats.provisional += 1 if provisional else 0
    if payload:
        keys = ("amazon_profile_id", "ad_product", "amazon_campaign_id", "placement", "date")
        payload = collapse(payload, keys)
        stats.restated = _note_restatements(
            session, PpcPlacement, payload, ad_product=ad_product, keys=keys, entity_type="placement", profile=profile,
            identify=lambda r: f"{r['amazon_campaign_id']}|{r['placement']}")
        stats.stored = insert_many(session, PpcPlacement, payload, keys=keys)
    session.flush()
    return stats


def load_purchased_products(session: Session, profile: PpcProfile, rows: list[dict[str, Any]], *,
                            ad_product: str,
                            report_id: str | None = None, today: date | None = None) -> LoadStats:
    stats = LoadStats()
    if not rows:
        return stats
    today = today or today_for(profile)
    pulled_at = utcnow()
    payload = []
    for row in rows:
        if not (row.get("purchased_asin") and row.get("date") and row.get("amazon_campaign_id")):
            stats.skipped += 1
            continue
        provisional = is_provisional(row["date"], today)
        payload.append({
            "amazon_profile_id": profile.amazon_profile_id, "ad_product": ad_product,
            "marketplace_id": profile.marketplace_id,
            "amazon_campaign_id": row["amazon_campaign_id"],
            "amazon_ad_group_id": row.get("amazon_ad_group_id"),
            "advertised_asin": row.get("advertised_asin") or "",
            "advertised_sku": row.get("advertised_sku"),
            "purchased_asin": row["purchased_asin"], "date": row["date"],
            "sales": round(float(row.get("sales") or 0), 2), "orders": int(row.get("orders") or 0),
            "units": int(row.get("units") or 0), "currency": profile.currency,
            "is_provisional": provisional, "report_id": report_id, "pulled_at": pulled_at,
        })
        stats.provisional += 1 if provisional else 0
    if payload:
        keys = ("amazon_profile_id", "ad_product", "amazon_campaign_id", "advertised_asin",
                "purchased_asin", "date")
        payload = collapse(payload, keys)
        stats.restated = _note_restatements(
            session, PpcPurchasedProduct, payload, ad_product=ad_product, keys=keys, entity_type="asin", profile=profile,
            identify=lambda r: f"{r['amazon_campaign_id']}|{r['purchased_asin']}")
        stats.stored = insert_many(session, PpcPurchasedProduct, payload, keys=keys)
    session.flush()
    return stats


# ---------------- reading, for everything downstream ----------------
def final_only(query, model=PpcMetricsDaily):
    """Every analyser reads through this. Provisional days never reach a conclusion (brief 4.4)."""
    return query.where(model.is_provisional.is_(False))


def settled_only(query, today: date, model=PpcMetricsDaily):
    """For experiment results and calibration: only days Amazon has stopped restating."""
    return final_only(query, model).where(model.date <= settled_before(today))


def daily(session: Session, profile: PpcProfile, *, entity_type: str,
          entity_ids: list[str] | None = None, start: date | None = None, end: date | None = None,
          include_provisional: bool = False, settled: bool = False,
          today: date | None = None) -> list[PpcMetricsDaily]:
    q = select(PpcMetricsDaily).where(
        PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
        PpcMetricsDaily.entity_type == entity_type)
    if entity_ids:
        q = q.where(PpcMetricsDaily.amazon_entity_id.in_([str(i) for i in entity_ids]))
    if start:
        q = q.where(PpcMetricsDaily.date >= start)
    if end:
        q = q.where(PpcMetricsDaily.date <= end)
    if settled:
        q = settled_only(q, today or today_for(profile))
    elif not include_provisional:
        q = final_only(q)
    return list(session.execute(q.order_by(PpcMetricsDaily.date)).scalars())


def restatements(session: Session, profile: PpcProfile, *, since: date | None = None,
                 limit: int = 200) -> list[PpcMetricsRestatement]:
    q = select(PpcMetricsRestatement).where(
        PpcMetricsRestatement.amazon_profile_id == profile.amazon_profile_id)
    if since:
        q = q.where(PpcMetricsRestatement.date >= since)
    return list(session.execute(q.order_by(
        PpcMetricsRestatement.restated_at.desc()).limit(limit)).scalars())
