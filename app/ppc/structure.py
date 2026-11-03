"""Reading the account's shape from Amazon: portfolios, campaigns, ad groups, ads, targets, negatives.

Everything Sponsored Products v3 lists comes back the same way — a POST to `/<entity>/list` with a
vendor media type, a page of rows under a collection key, and a `nextToken`. So there is one
paginator and one table of endpoints; correcting a media type is a one-line change.

Paths, media types, collection keys and field names below were taken from Amazon's own OpenAPI
specs on 2026-09-25 (Sponsored Products v3 and Portfolios v3). Three of them are easy to get wrong:

* The media types are **camelCase** — `application/vnd.spCampaign.v3+json`. Not the lowercase form.
* `maxResults` is not sent at all, because Amazon documents the default as "max page size for given
  API": naming a number can only be smaller than, or larger than, the limit it already applies.
* Creation dates and serving status live in `extendedData`, which Amazon omits unless the request
  asks for it with `includeExtendedDataFields`.

Nothing here writes to Amazon. A sync only ever stores what Amazon says.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (AmazonListing, PpcAdGroup, PpcCampaign, PpcNegative, PpcPortfolio,
                           PpcProductAd, PpcProfile, PpcTarget)
from app.ppc import api, profiles
from app.ppc.errors import AdsError
from app.services.common import utcnow

log = logging.getLogger(__name__)

MAX_PAGES = 500            # a stop, so a broken nextToken can't loop for ever
# The only three states that can be used to filter live entities; the enum also holds PROPOSED,
# ENABLING, OTHER and USER_DELETED, which are draft or read-only states we never ask for.
ALL_STATES = ["ENABLED", "PAUSED", "ARCHIVED"]
# Amazon's states we understand. Anything else is treated as not-running rather than as enabled: a
# USER_DELETED draft read as "enabled" would put dead entities in front of the analysers.
STATES = {"ENABLED": "enabled", "PAUSED": "paused", "ARCHIVED": "archived"}


@dataclass(frozen=True)
class Endpoint:
    """One v3 list endpoint: where it lives, what media type it wants, what key holds the rows."""

    path: str
    media: str
    collection: str
    operation: str


# Everything in this module is Sponsored Products: the ENDPOINTS table below is the Sponsored
# Products v3 surface, and `_media` builds its vendor media types. Stated as a constant rather than
# left implicit because, until milestone 12, "implicit" meant every row written here was unlabelled
# and every query against them meant "all ad products" while reading as though it meant this one.
# Sponsored Display and Brands get their own modules: their endpoints take different shapes
# entirely — GET with startIndex rather than POST /list, plain application/json rather than a vendor
# media type, and a bare array rather than a success/error 207.
AD_PRODUCT = "sp"


def _media(entity: str) -> str:
    return f"application/vnd.{entity}.v3+json"


ENDPOINTS: dict[str, Endpoint] = {
    "portfolios": Endpoint("/portfolios/list", _media("spPortfolio"),
                           "portfolios", "listPortfolios"),
    "campaigns": Endpoint("/sp/campaigns/list", _media("spCampaign"),
                          "campaigns", "listSpCampaigns"),
    "ad_groups": Endpoint("/sp/adGroups/list", _media("spAdGroup"),
                          "adGroups", "listSpAdGroups"),
    "product_ads": Endpoint("/sp/productAds/list", _media("spProductAd"),
                            "productAds", "listSpProductAds"),
    "keywords": Endpoint("/sp/keywords/list", _media("spKeyword"),
                         "keywords", "listSpKeywords"),
    "targets": Endpoint("/sp/targets/list", _media("spTargetingClause"),
                        "targetingClauses", "listSpTargets"),
    "negative_keywords": Endpoint("/sp/negativeKeywords/list", _media("spNegativeKeyword"),
                                  "negativeKeywords", "listSpNegativeKeywords"),
    "campaign_negative_keywords": Endpoint(
        "/sp/campaignNegativeKeywords/list", _media("spCampaignNegativeKeyword"),
        "campaignNegativeKeywords", "listSpCampaignNegativeKeywords"),
    "negative_targets": Endpoint("/sp/negativeTargets/list", _media("spNegativeTargetingClause"),
                                 "negativeTargetingClauses", "listSpNegativeTargets"),
    "campaign_negative_targets": Endpoint(
        "/sp/campaignNegativeTargets/list", _media("spCampaignNegativeTargetingClause"),
        "campaignNegativeTargetingClauses", "listSpCampaignNegativeTargets"),
}

# Amazon's auto-targeting expressions, in the vocabulary a PPC manager uses.
AUTO_MATCH_TYPES = {
    "QUERY_HIGH_REL_MATCHES": "auto_close",
    "QUERY_BROAD_REL_MATCHES": "auto_loose",
    "ASIN_SUBSTITUTE_RELATED": "auto_substitutes",
    "ASIN_ACCESSORY_RELATED": "auto_complements",
}
PRODUCT_EXPRESSIONS = {"ASIN_SAME_AS", "ASIN_EXPANDED_FROM"}
CATEGORY_EXPRESSIONS = {"ASIN_CATEGORY_SAME_AS", "ASIN_BRAND_SAME_AS", "ASIN_PRICE_BETWEEN",
                        "ASIN_PRICE_GREATER_THAN", "ASIN_PRICE_LESS_THAN",
                        "ASIN_REVIEW_RATING_BETWEEN", "ASIN_REVIEW_RATING_GREATER_THAN",
                        "ASIN_REVIEW_RATING_LESS_THAN", "ASIN_IS_PRIME_SHIPPING_ELIGIBLE",
                        "ASIN_AGE_RANGE_SAME_AS", "ASIN_GENRE_SAME_AS"}


# ---------------- the paginator ----------------
def _state(value: Any) -> str:
    raw = str(value or "").strip().upper()
    known = STATES.get(raw)
    if known:
        return known
    if raw:
        log.info("Amazon reported the state %r, which isn't a serving state; treating it as paused", raw)
    return "paused"


def _extended(raw: dict[str, Any]) -> dict[str, Any]:
    """`extendedData` carries the creation date and the serving status, when we asked for them."""
    data = raw.get("extendedData")
    return data if isinstance(data, dict) else {}


def _serving(raw: dict[str, Any]) -> tuple[str | None, list[Any]]:
    extended = _extended(raw)
    status = extended.get("servingStatus")
    details = extended.get("servingStatusDetails")
    return (str(status)[:60] if status else None), (details if isinstance(details, list) else [])


def _created(raw: dict[str, Any]) -> datetime | None:
    return _moment(_extended(raw).get("creationDateTime"))


def _archived_at(row) -> datetime | None:
    """When the entity was archived — set once, so it doesn't drift to the last sync's time."""
    if row.state != "archived":
        return None
    return row.archived_at or utcnow()


def _money(value: Any) -> float | None:
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def _day(value: Any) -> date | None:
    """Amazon sends plain dates, and newer fields sometimes send them with a time. Keep the date."""
    if not value:
        return None
    text = str(value)[:10].replace("/", "-")
    try:
        return date.fromisoformat(text)
    except ValueError:
        # Some older SP fields use YYYYMMDD.
        digits = "".join(c for c in str(value) if c.isdigit())[:8]
        try:
            return datetime.strptime(digits, "%Y%m%d").date()
        except ValueError:
            return None


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        day = _day(value)
        return datetime.combine(day, datetime.min.time()) if day else None


def pages(client, endpoint: Endpoint, profile: PpcProfile, *, body: dict[str, Any] | None = None,
          states: list[str] | None = None) -> Iterator[list[dict[str, Any]]]:
    """Every page of one list endpoint. Archived rows are included so history isn't lost."""
    request = dict(body or {})
    # No maxResults: Amazon's default *is* its maximum page size for each endpoint.
    request.setdefault("stateFilter", {"include": states or ALL_STATES})
    request.setdefault("includeExtendedDataFields", True)
    token = None
    for page in range(MAX_PAGES):
        if token:
            request["nextToken"] = token
        response = client.post(endpoint.path, json=request, profile_id=profile.amazon_profile_id,
                               content_type=endpoint.media, accept=endpoint.media,
                               operation=endpoint.operation) or {}
        rows = response.get(endpoint.collection)
        if rows is None and isinstance(response, list):
            rows = response
        yield list(rows or [])
        token = response.get("nextToken")
        if not token:
            return
    raise AdsError(f"{endpoint.operation} kept returning pages past {MAX_PAGES}; stopping.")


def rows(client, name: str, profile: PpcProfile, **kw) -> list[dict[str, Any]]:
    endpoint = ENDPOINTS[name]
    out: list[dict[str, Any]] = []
    for page in pages(client, endpoint, profile, **kw):
        out.extend(page)
    return out


# ---------------- the syncs ----------------
def sync_portfolios(session: Session, profile: PpcProfile, client) -> dict[str, int]:
    seen = set()
    for raw in rows(client, "portfolios", profile):
        amazon_id = str(raw.get("portfolioId") or "").strip()
        if not amazon_id:
            continue
        row = session.execute(select(PpcPortfolio).where(
            PpcPortfolio.profile_id == profile.id,
            PpcPortfolio.amazon_portfolio_id == amazon_id)).scalar_one_or_none()
        if row is None:
            row = PpcPortfolio(profile_id=profile.id, amazon_portfolio_id=amazon_id)
            session.add(row)
        budget = raw.get("budget") or {}
        row.name = (raw.get("name") or row.name or "")[:255]
        row.state = _state(raw.get("state"))
        row.budget_amount = _money(budget.get("amount") if isinstance(budget, dict) else None)
        row.budget_currency = (budget.get("currencyCode") or profile.currency)[:3] if row.budget_amount else None
        row.budget_policy = (budget.get("policy") or None)
        row.budget_start_date = _day(budget.get("startDate"))
        row.budget_end_date = _day(budget.get("endDate"))
        row.in_budget = raw.get("inBudget")
        row.last_synced_at = utcnow()
        seen.add(amazon_id)
    session.flush()
    return {"portfolios": len(seen)}


def sync_campaigns(session: Session, profile: PpcProfile, client) -> dict[str, Any]:
    portfolio_ids = {p.amazon_portfolio_id: p.id for p in session.execute(
        select(PpcPortfolio).where(PpcPortfolio.profile_id == profile.id)).scalars()}
    seen: set[str] = set()
    for raw in rows(client, "campaigns", profile):
        amazon_id = str(raw.get("campaignId") or "").strip()
        if not amazon_id:
            continue
        row = session.execute(select(PpcCampaign).where(
            PpcCampaign.profile_id == profile.id, PpcCampaign.ad_product == AD_PRODUCT,
            PpcCampaign.amazon_campaign_id == amazon_id)).scalar_one_or_none()
        if row is None:
            row = PpcCampaign(profile_id=profile.id, ad_product=AD_PRODUCT,
                              amazon_campaign_id=amazon_id)
            session.add(row)
        budget = raw.get("budget") or {}
        dynamic = raw.get("dynamicBidding") or {}
        row.name = (raw.get("name") or row.name or "")[:255]
        row.campaign_type = "sponsoredProducts"
        row.targeting_type = (raw.get("targetingType") or "").strip().lower() or row.targeting_type
        row.state = _state(raw.get("state"))
        row.daily_budget = _money(budget.get("budget") if isinstance(budget, dict) else budget)
        row.budget_type = (budget.get("budgetType") or "").strip().lower() or row.budget_type
        row.bidding_strategy = dynamic.get("strategy") or row.bidding_strategy
        # Kept verbatim so a placement change can be rolled back byte-identical.
        row.placement_adjustments = dynamic.get("placementBidding") or []
        row.portfolio_id = portfolio_ids.get(str(raw.get("portfolioId") or ""))
        row.start_date = _day(raw.get("startDate")) or row.start_date
        row.end_date = _day(raw.get("endDate"))
        row.created_at_amazon = row.created_at_amazon or _created(raw)
        row.serving_status, row.serving_status_details = _serving(raw)
        row.last_synced_at = utcnow()
        row.archived_at = _archived_at(row)
        seen.add(amazon_id)
    session.flush()
    return {"campaigns": len(seen), "seen": seen}


def _campaign_map(session: Session, profile: PpcProfile) -> dict[str, Any]:
    return {c.amazon_campaign_id: c.id for c in session.execute(
        select(PpcCampaign).where(PpcCampaign.profile_id == profile.id,
                                  PpcCampaign.ad_product == AD_PRODUCT)).scalars()}


def _ad_group_map(session: Session, profile: PpcProfile) -> dict[str, Any]:
    return {g.amazon_ad_group_id: g for g in session.execute(
        select(PpcAdGroup).where(PpcAdGroup.profile_id == profile.id,
                                 PpcAdGroup.ad_product == AD_PRODUCT)).scalars()}


def sync_ad_groups(session: Session, profile: PpcProfile, client) -> dict[str, Any]:
    campaigns = _campaign_map(session, profile)
    seen: set[str] = set()
    orphans = 0
    for raw in rows(client, "ad_groups", profile):
        amazon_id = str(raw.get("adGroupId") or "").strip()
        amazon_campaign_id = str(raw.get("campaignId") or "").strip()
        if not amazon_id:
            continue
        campaign_id = campaigns.get(amazon_campaign_id)
        if campaign_id is None:
            # A campaign created between the two calls. The next sync picks it up.
            orphans += 1
            continue
        row = session.execute(select(PpcAdGroup).where(
            PpcAdGroup.profile_id == profile.id, PpcAdGroup.ad_product == AD_PRODUCT,
            PpcAdGroup.amazon_ad_group_id == amazon_id)).scalar_one_or_none()
        if row is None:
            row = PpcAdGroup(profile_id=profile.id, ad_product=AD_PRODUCT,
                             amazon_ad_group_id=amazon_id,
                             campaign_id=campaign_id, amazon_campaign_id=amazon_campaign_id)
            session.add(row)
        row.campaign_id = campaign_id
        row.amazon_campaign_id = amazon_campaign_id
        row.name = (raw.get("name") or row.name or "")[:255]
        row.state = _state(raw.get("state"))
        row.default_bid = _money(raw.get("defaultBid"))
        row.serving_status, row.serving_status_details = _serving(raw)
        row.last_synced_at = utcnow()
        row.archived_at = _archived_at(row)
        seen.add(amazon_id)
    session.flush()
    return {"ad_groups": len(seen), "ad_groups_without_a_campaign": orphans, "seen": seen}


def sync_product_ads(session: Session, profile: PpcProfile, client) -> dict[str, Any]:
    groups = _ad_group_map(session, profile)
    listings = {(row.asin, row.sku): row.id for row in session.execute(
        select(AmazonListing).where(AmazonListing.marketplace_id == profile.marketplace_id)).scalars()}
    by_sku = {sku: rid for (_asin, sku), rid in listings.items() if sku}
    by_asin = {asin: rid for (asin, _sku), rid in listings.items() if asin}
    seen: set[str] = set()
    orphans = 0
    for raw in rows(client, "product_ads", profile):
        amazon_id = str(raw.get("adId") or "").strip()
        group = groups.get(str(raw.get("adGroupId") or "").strip())
        if not amazon_id or group is None:
            orphans += 0 if not amazon_id else 1
            continue
        row = session.execute(select(PpcProductAd).where(
            PpcProductAd.profile_id == profile.id, PpcProductAd.ad_product == AD_PRODUCT,
            PpcProductAd.amazon_ad_id == amazon_id)).scalar_one_or_none()
        if row is None:
            row = PpcProductAd(profile_id=profile.id, ad_product=AD_PRODUCT,
                               amazon_ad_id=amazon_id,
                               campaign_id=group.campaign_id, ad_group_id=group.id)
            session.add(row)
        row.campaign_id = group.campaign_id
        row.ad_group_id = group.id
        row.asin = (raw.get("asin") or row.asin or None)
        row.sku = (raw.get("sku") or row.sku or None)
        # The link to Phase 3: stock, suppression and Buy Box reach the analysers through it.
        row.listing_id = by_sku.get(row.sku) or by_asin.get(row.asin) or row.listing_id
        row.state = _state(raw.get("state"))
        row.serving_status, row.serving_status_details = _serving(raw)
        row.last_synced_at = utcnow()
        row.archived_at = _archived_at(row)
        seen.add(amazon_id)
    session.flush()
    return {"product_ads": len(seen), "product_ads_without_an_ad_group": orphans, "seen": seen}


# ---------------- targets ----------------
def describe_expression(expression: Any, resolved: Any = None) -> tuple[str, str, str]:
    """Amazon's expression list → (target_type, match_type, readable text).

    The readable text is what the LLM and the owner see in a recommendation, so it says
    "asin=B01ABC" rather than repeating Amazon's nested JSON back at them. Amazon's own
    `resolvedExpression` is used for the classification when it is richer — for a category target it
    resolves the category id to its name, which is the difference between a recommendation that
    reads "category=1234567" and one that reads "category=Vape Accessories".
    """
    parts = expression if isinstance(expression, list) else [expression] if expression else []
    resolved_parts = resolved if isinstance(resolved, list) else []
    kinds = [str((p or {}).get("type") or "").strip().upper() for p in parts if isinstance(p, dict)]
    readable = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "").strip().upper()
        value = part.get("value")
        if isinstance(value, list):     # nested refinements (category + brand + price)
            nested = [f"{str((v or {}).get('type') or '').lower()}={(v or {}).get('value')}"
                      for v in value if isinstance(v, dict)]
            readable.append(f"{kind.lower()}({', '.join(nested)})" if nested else kind.lower())
        else:
            readable.append(f"{kind.lower()}={value}" if value is not None else kind.lower())
    text = " ".join(readable)
    if resolved_parts:
        resolved_text = " ".join(
            f"{str((p or {}).get('type') or '').lower()}={(p or {}).get('value')}"
            if isinstance(p, dict) and p.get("value") is not None
            else str((p or {}).get("type") or "").lower()
            for p in resolved_parts if isinstance(p, dict))
        # Amazon's resolved form names categories and brands; prefer it when it says more.
        if len(resolved_text) > len(text):
            text = resolved_text
    for kind in kinds:
        if kind in AUTO_MATCH_TYPES:
            return "auto", AUTO_MATCH_TYPES[kind], text or kind.lower()
    if any(k in PRODUCT_EXPRESSIONS for k in kinds):
        return "product", "asin", text
    if any(k in CATEGORY_EXPRESSIONS for k in kinds):
        return "category", "category", text
    if any("AUDIENCE" in k for k in kinds):
        return "audience", "audience", text
    return "product", "asin", text


def sync_targets(session: Session, profile: PpcProfile, client) -> dict[str, Any]:
    """Keywords and product/category/auto targets, into one table with two id namespaces."""
    groups = _ad_group_map(session, profile)
    counts = {"keywords": 0, "targets": 0, "targets_without_an_ad_group": 0}
    seen: dict[str, set[str]] = {"keyword": set(), "target": set()}
    for kind, name, id_field in (("keyword", "keywords", "keywordId"),
                                 ("target", "targets", "targetId")):
        for raw in rows(client, name, profile):
            amazon_id = str(raw.get(id_field) or "").strip()
            group = groups.get(str(raw.get("adGroupId") or "").strip())
            if not amazon_id:
                continue
            if group is None:
                counts["targets_without_an_ad_group"] += 1
                continue
            row = session.execute(select(PpcTarget).where(
                PpcTarget.profile_id == profile.id, PpcTarget.ad_product == AD_PRODUCT,
                PpcTarget.target_kind == kind,
                PpcTarget.amazon_target_id == amazon_id)).scalar_one_or_none()
            if kind == "keyword":
                target_type, match_type = "keyword", (raw.get("matchType") or "exact").strip().lower()
                if match_type not in ("exact", "phrase", "broad"):
                    match_type = "exact"
                expression = (raw.get("keywordText") or "").strip()
            else:
                target_type, match_type, expression = describe_expression(
                    raw.get("expression"), resolved=raw.get("resolvedExpression"))
            if row is None:
                row = PpcTarget(profile_id=profile.id, ad_product=AD_PRODUCT, target_kind=kind,
                                amazon_target_id=amazon_id,
                                campaign_id=group.campaign_id, ad_group_id=group.id,
                                amazon_campaign_id=group.amazon_campaign_id,
                                amazon_ad_group_id=group.amazon_ad_group_id,
                                first_seen_at=utcnow(), target_type=target_type, match_type=match_type)
                session.add(row)
            row.campaign_id = group.campaign_id
            row.ad_group_id = group.id
            row.amazon_campaign_id = group.amazon_campaign_id
            row.amazon_ad_group_id = group.amazon_ad_group_id
            row.target_type = target_type
            row.match_type = match_type
            row.expression = expression
            row.state = _state(raw.get("state"))
            bid = _money(raw.get("bid"))
            row.bid = bid if bid is not None else row.bid
            # An empty bid means the ad group default is in force, which matters to every bid analyser.
            row.bid_inherited = raw.get("bid") is None
            row.created_at_amazon = row.created_at_amazon or _created(raw)
            row.serving_status, row.serving_status_details = _serving(raw)
            row.last_synced_at = utcnow()
            row.archived_at = _archived_at(row)
            counts["keywords" if kind == "keyword" else "targets"] += 1
            seen[kind].add(amazon_id)
    both = seen["keyword"] & seen["target"]
    if both:
        # ppc_metrics_daily keys a target row by Amazon's id alone, so the same id in both namespaces
        # would merge two unrelated entities' spend. Amazon has never been seen to do this; if it
        # ever does, this is the line that explains the strange numbers.
        log.error("the same id is both a keyword and a product target (%s); their daily metrics "
                  "cannot be told apart", ", ".join(sorted(both)[:5]))
        counts["ids_in_both_namespaces"] = len(both)
    session.flush()
    counts["seen"] = seen
    return counts


def sync_negatives(session: Session, profile: PpcProfile, client) -> dict[str, Any]:
    """Negative keywords and negative targets, at both ad group and campaign level."""
    campaigns = _campaign_map(session, profile)
    groups = _ad_group_map(session, profile)
    seen: dict[str, set[str]] = {"keyword": set(), "target": set()}
    counted = skipped = 0
    specs = (
        ("negative_keywords", "keyword", "keywordId", "ad_group"),
        ("campaign_negative_keywords", "keyword", "keywordId", "campaign"),
        ("negative_targets", "target", "targetId", "ad_group"),
        ("campaign_negative_targets", "target", "targetId", "campaign"),
    )
    for name, kind, id_field, level in specs:
        for raw in rows(client, name, profile):
            amazon_id = str(raw.get(id_field) or "").strip()
            if not amazon_id:
                continue
            group = groups.get(str(raw.get("adGroupId") or "").strip()) if level == "ad_group" else None
            if level == "ad_group" and group is None:
                # Falling back to the campaign would turn one ad group's negative into an
                # account-wide block, and the agent would report a keyword as excluded everywhere.
                skipped += 1
                continue
            campaign_id = (group.campaign_id if group is not None
                           else campaigns.get(str(raw.get("campaignId") or "").strip()))
            if campaign_id is None and group is None:
                skipped += 1
                continue        # neither level resolved; the next sync will have the parent
            row = session.execute(select(PpcNegative).where(
                PpcNegative.profile_id == profile.id, PpcNegative.ad_product == AD_PRODUCT,
                PpcNegative.target_kind == kind,
                PpcNegative.amazon_negative_id == amazon_id)).scalar_one_or_none()
            if kind == "keyword":
                match_type = (raw.get("matchType") or "").strip().lower()
                match_type = match_type.replace("negative_", "")
                if match_type not in ("exact", "phrase", "broad"):
                    match_type = "exact"
                expression = (raw.get("keywordText") or "").strip()
            else:
                _type, match_type, expression = describe_expression(
                    raw.get("expression"), resolved=raw.get("resolvedExpression"))
            if row is None:
                row = PpcNegative(profile_id=profile.id, ad_product=AD_PRODUCT, target_kind=kind,
                                  amazon_negative_id=amazon_id, match_type=match_type)
                session.add(row)
            row.campaign_id = campaign_id
            row.ad_group_id = group.id if group is not None else None
            row.match_type = match_type
            row.expression = expression
            row.state = _state(raw.get("state"))
            row.last_synced_at = utcnow()
            row.archived_at = _archived_at(row)
            seen[kind].add(amazon_id)
            counted += 1
    session.flush()
    return {"negatives": counted, "negatives_without_a_parent": skipped, "seen": seen}


# ---------------- archiving what Amazon stopped listing ----------------
def _archive_missing(session: Session, profile: PpcProfile, stats: dict[str, Any],
                     previous_sync: datetime | None) -> int:
    """Mark rows Amazon no longer lists as archived, once they have been missing twice.

    Only ever called after a *complete* sync: a half-finished read would otherwise archive the
    account. Rows are never deleted — the metrics history and the audit trail refer to them.

    Two misses, not one, because Amazon's state filter can only name ENABLED, PAUSED and ARCHIVED,
    while the enum also holds PROPOSED, ENABLING, OTHER and USER_DELETED. An entity that passes
    briefly through one of those is absent from the list without being gone, and archiving it on the
    strength of a single sync would hide a live campaign from every analyser until the next one.
    `previous_sync` is when the last complete sync began: a row stamped at or after it was seen then,
    so this is only its first miss.

    Every query here is scoped to `AD_PRODUCT`, and that is not tidiness. `seen` is built from a
    Sponsored Products sync, so without the scope the first sync after Sponsored Brands and Display
    arrived would find every SB and SD row missing from it and archive the lot — hiding the owner's
    other two ad products from every analyser and every view, in one pass, silently.
    """
    gone = 0

    def missed_before(row) -> bool:
        if previous_sync is None:
            return False          # nothing to compare against yet; wait for the next sync
        return row.last_synced_at is None or row.last_synced_at < previous_sync
    plans = [
        (PpcCampaign, PpcCampaign.amazon_campaign_id, stats.get("campaigns_seen") or set()),
        (PpcAdGroup, PpcAdGroup.amazon_ad_group_id, stats.get("ad_groups_seen") or set()),
        (PpcProductAd, PpcProductAd.amazon_ad_id, stats.get("product_ads_seen") or set()),
    ]
    for model, id_column, seen in plans:
        for row in session.execute(select(model).where(
                model.profile_id == profile.id, model.ad_product == AD_PRODUCT,
                model.archived_at.is_(None))).scalars():
            if getattr(row, id_column.key) not in seen and missed_before(row):
                row.state, row.archived_at = "archived", utcnow()
                gone += 1
    for kind in ("keyword", "target"):
        for model, id_column, key in ((PpcTarget, PpcTarget.amazon_target_id, "targets_seen"),
                                      (PpcNegative, PpcNegative.amazon_negative_id, "negatives_seen")):
            seen = (stats.get(key) or {}).get(kind) or set()
            for row in session.execute(select(model).where(
                    model.profile_id == profile.id, model.ad_product == AD_PRODUCT,
                    model.target_kind == kind,
                    model.archived_at.is_(None))).scalars():
                if getattr(row, id_column.key) not in seen and missed_before(row):
                    row.state, row.archived_at = "archived", utcnow()
                    gone += 1
    session.flush()
    return gone


def sync_all(session: Session, profile: PpcProfile | None = None, client=None) -> dict[str, Any]:
    """The whole account shape, parents before children so every child finds its parent."""
    profile = profile or profiles.require(session)
    client = client or api.client()
    # When this sync began, and when the last complete one did. Rows are stamped as they are seen,
    # so "last_synced_at older than the previous sync started" is exactly "missing last time too".
    started, previous_sync = utcnow(), profile.last_synced_at
    stats: dict[str, Any] = {"profile": profile.amazon_profile_id}
    stats.update(sync_portfolios(session, profile, client))

    campaigns = sync_campaigns(session, profile, client)
    stats["campaigns_seen"] = campaigns.pop("seen", set())
    stats.update(campaigns)

    groups = sync_ad_groups(session, profile, client)
    stats["ad_groups_seen"] = groups.pop("seen", set())
    stats.update(groups)

    ads = sync_product_ads(session, profile, client)
    stats["product_ads_seen"] = ads.pop("seen", set())
    stats.update(ads)

    targets = sync_targets(session, profile, client)
    stats["targets_seen"] = targets.pop("seen", {})
    stats.update(targets)

    negatives = sync_negatives(session, profile, client)
    stats["negatives_seen"] = negatives.pop("seen", {})
    stats.update(negatives)

    stats["archived"] = _archive_missing(session, profile, stats, previous_sync)
    # Stamped with the start, not the end: it means "complete as of here", and it is what the next
    # sync compares its rows against.
    profile.last_synced_at = started
    session.flush()
    for key in ("campaigns_seen", "ad_groups_seen", "product_ads_seen", "targets_seen", "negatives_seen"):
        stats.pop(key, None)
    log.info("ppc structure synced", extra=stats)
    return stats
