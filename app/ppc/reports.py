"""Sponsored Products reporting v3: what to ask Amazon for, and turning its rows into our rows.

Every column name, group-by value, date limit and retention window here was read from the live
documentation on 2026-09-25. The ones worth knowing, because they are all traps:

* Orders are called `purchases7d`. There is no `orders` column in Sponsored Products v3 at all.
* Units are `unitsSoldClicks7d`. There is no plain `unitsSold`.
* Cost is `cost`. `spend` exists on only two report types, so nothing here uses it.
* Same-SKU sales are `attributedSalesSameSku7d`, not `salesSameSku7d`; `salesOtherSku` exists at 7d
  only, and not on the campaign report.
* `spPurchasedProduct` has no impressions, clicks or cost columns — ACOS cannot be computed from it.
* The only impression-share column in v3 is `topOfSearchImpressionShare`, and only the campaign
  report has it. `ppc_search_terms.search_term_impression_share` therefore stays empty until Brand
  Analytics fills it (Phase 3 has that data); v3 has no search-term impression share or rank.
* `spAdvertisedProduct` groups by `advertiser` and `spPurchasedProduct` by `asin`. Neither name is
  guessable from the report's own name.
* 31 days per request, and Amazon keeps 95 days of history — 65 for search terms. A twelve-month
  backfill is therefore impossible through this API; 95 days is the ceiling, and the agent's longer
  history is the one it accumulates here from now on.
* Nothing in the API says which attribution basis produced a number, and re-requesting a date later
  simply returns the restated figure. Provenance is ours to write, which is what `attribution_note`
  and `ppc_metrics_restatements` are for.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import PPC_AD_PRODUCTS, PpcProfile
from app.ppc import api, metrics, profiles
from app.ppc.client import REPORT_PATH
from app.ppc.errors import AdsError, AdsReportFailed

log = logging.getLogger(__name__)

AD_PRODUCT = "SPONSORED_PRODUCTS"
FORMAT = "GZIP_JSON"           # the only format Sponsored Products v3 offers
TIME_UNIT = "DAILY"
MAX_RANGE_DAYS = 31           # Amazon's per-request limit
RETENTION_DAYS = 95           # how far back Amazon keeps most report data
SEARCH_TERM_RETENTION_DAYS = 65   # search terms are kept for less
# Sponsored Products attributes sales over a trailing window; 7 days is the figure Seller Central
# shows, so it is the one stored, and it is recorded on every row so an old number stays readable.
ATTRIBUTION_WINDOW_DAYS = metrics.ATTRIBUTION_WINDOW_DAYS
W = f"{ATTRIBUTION_WINDOW_DAYS}d"

BASE_METRICS = ["impressions", "clicks", "cost", f"purchases{W}", f"sales{W}", f"unitsSoldClicks{W}"]

# Amazon's placement labels, in the words the analysers and the owner use.
PLACEMENTS = {
    "top of search on-amazon": "top_of_search",
    "detail page on-amazon": "product_pages",
    "other on-amazon": "rest_of_search",
    "amazon business": "amazon_business",
}


@dataclass(frozen=True)
class ReportSpec:
    key: str
    report_type: str
    group_by: list[str]
    columns: list[str]
    entity_type: str | None = None        # for ppc_metrics_daily loads
    id_fields: tuple[str, ...] = ()       # first present column wins as the entity id
    retention_days: int = RETENTION_DAYS
    optional_columns: frozenset[str] = field(default_factory=frozenset)

    @property
    def name(self) -> str:
        return f"{self.report_type} by {'+'.join(self.group_by)}"

    @property
    def ad_product(self) -> str:
        """Which ad product this report describes, taken from Amazon's own report type name.

        `spCampaigns` is Sponsored Products, `sbCampaigns` Brands, `sdCampaigns` Display — so the
        prefix already carries it and there is no separate field to set, and therefore none to
        forget. A report type that does not start with a known prefix is a programming error rather
        than something to guess at, because guessing would file another ad product's numbers under
        this one.
        """
        prefix = self.report_type[:2]
        if prefix not in PPC_AD_PRODUCTS:
            raise ValueError(f"{self.report_type} does not name an ad product I know")
        return prefix


SPECS: dict[str, ReportSpec] = {
    "campaigns": ReportSpec(
        key="campaigns", report_type="spCampaigns", group_by=["campaign"], entity_type="campaign",
        id_fields=("campaignId",),
        columns=["date", "campaignId", "campaignName", "campaignStatus", *BASE_METRICS,
                 f"attributedSalesSameSku{W}", f"purchasesSameSku{W}", "topOfSearchImpressionShare"],
        # Only an "additional metric" of the campaign grouping, so drop it rather than lose the report.
        optional_columns=frozenset({"topOfSearchImpressionShare", "campaignStatus",
                                    f"attributedSalesSameSku{W}", f"purchasesSameSku{W}"})),
    "ad_groups": ReportSpec(
        key="ad_groups", report_type="spCampaigns", group_by=["adGroup"], entity_type="ad_group",
        id_fields=("adGroupId",),
        columns=["date", "campaignId", "adGroupId", "adGroupName", *BASE_METRICS],
        optional_columns=frozenset({"adGroupName", "campaignId"})),
    "placements": ReportSpec(
        key="placements", report_type="spCampaigns", group_by=["campaignPlacement"],
        columns=["date", "campaignId", "placementClassification", *BASE_METRICS]),
    "targets": ReportSpec(
        key="targets", report_type="spTargeting", group_by=["targeting"], entity_type="target",
        id_fields=("keywordId", "targetId"),
        columns=["date", "campaignId", "adGroupId", "keywordId", "keyword", "targeting", "matchType",
                 "keywordBid", *BASE_METRICS, f"attributedSalesSameSku{W}", f"salesOtherSku{W}"],
        optional_columns=frozenset({"keywordBid", "matchType", "keyword",
                                    f"attributedSalesSameSku{W}", f"salesOtherSku{W}"})),
    "search_terms": ReportSpec(
        key="search_terms", report_type="spSearchTerm", group_by=["searchTerm"],
        retention_days=SEARCH_TERM_RETENTION_DAYS,
        columns=["date", "campaignId", "adGroupId", "keywordId", "keyword", "searchTerm", "matchType",
                 "targeting", *BASE_METRICS],
        optional_columns=frozenset({"keyword", "matchType", "targeting"})),
    "advertised_products": ReportSpec(
        key="advertised_products", report_type="spAdvertisedProduct", group_by=["advertiser"],
        entity_type="product_ad", id_fields=("adId", "advertisedAsin"),
        columns=["date", "campaignId", "adGroupId", "adId", "advertisedAsin", "advertisedSku",
                 *BASE_METRICS, f"attributedSalesSameSku{W}", f"salesOtherSku{W}"],
        optional_columns=frozenset({"adId", f"attributedSalesSameSku{W}", f"salesOtherSku{W}"})),
    "purchased_products": ReportSpec(
        key="purchased_products", report_type="spPurchasedProduct", group_by=["asin"],
        # No impressions, clicks or cost exist on this report type.
        columns=["date", "campaignId", "adGroupId", "advertisedAsin", "advertisedSku",
                 "purchasedAsin", f"purchases{W}", f"sales{W}", f"unitsSoldClicks{W}"],
        optional_columns=frozenset({"advertisedAsin", "advertisedSku", "adGroupId"})),
}
DAILY_ORDER = ["campaigns", "ad_groups", "placements", "targets", "search_terms",
               "advertised_products", "purchased_products"]


def plan(profile: PpcProfile, *, days: int, today: date, only: list[str] | None = None,
         reason: str = "daily") -> list[dict[str, Any]]:
    """Every (report, date range) a load needs, as job payloads. Oldest first."""
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    out: list[dict[str, Any]] = []
    for key in (only or DAILY_ORDER):
        spec = SPECS[key]
        trimmed = clamp(spec, start, end, today)
        if trimmed is None:
            continue
        for chunk_start, chunk_end in chunks(*trimmed):
            out.append({"profile_id": str(profile.id), "report": key,
                        "start": chunk_start.isoformat(), "end": chunk_end.isoformat(),
                        "reason": reason})
    out.sort(key=lambda c: (c["start"], c["report"]))
    return out


def attribution_note(spec: ReportSpec, *, generated_at: str | None = None) -> dict[str, Any]:
    """Stored with every row so a number from months ago can still be explained (brief 4.4).

    Amazon sends no attribution metadata at all, so this is the only record of which columns a
    stored figure came from — and re-requesting the date later returns the restated number, not this
    one. Hence `generated_at`: when Amazon built the report we read.
    """
    return {"window_days": ATTRIBUTION_WINDOW_DAYS, "report_type": spec.report_type,
            "group_by": spec.group_by, "time_unit": TIME_UNIT, "generated_at": generated_at,
            "columns": {"sales": f"sales{W}", "orders": f"purchases{W}",
                        "units": f"unitsSoldClicks{W}", "spend": "cost"}}


# ---------------- requesting ----------------
def request_body(spec: ReportSpec, start: date, end: date, *, columns: list[str] | None = None) -> dict:
    return {
        "name": f"{spec.name} {start.isoformat()}..{end.isoformat()}",
        "startDate": start.isoformat(), "endDate": end.isoformat(),
        "configuration": {
            "adProduct": AD_PRODUCT, "groupBy": list(spec.group_by),
            "columns": list(columns or spec.columns), "reportTypeId": spec.report_type,
            "timeUnit": TIME_UNIT, "format": FORMAT,
        },
    }


COLUMN_COMPLAINT = re.compile(r"column|not supported|unsupported|invalid metric|metric", re.I)


def _rejected_columns(message: str, columns: list[str]) -> list[str]:
    """Which of our columns Amazon named in a 400. Columns move between groupings over time.

    Only read as a column complaint when Amazon says so: several column names are ordinary words
    ("keyword", "targeting"), and an unrelated 400 mentioning one would otherwise drop it.
    """
    text = message or ""
    if not COLUMN_COMPLAINT.search(text):
        return []
    return [c for c in columns if re.search(rf"\b{re.escape(c)}\b", text)]


def fetch(client, spec: ReportSpec, profile: PpcProfile, start: date,
          end: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One report, one date chunk. A column Amazon has moved is dropped once rather than fatal.

    Columns are not nullable on request: asking for one this grouping doesn't carry fails the whole
    report. So the useful-but-optional ones are listed as optional, and if Amazon names one in a 400
    it is dropped and the report is requested again without it.
    """
    columns = list(spec.columns)
    for attempt in (1, 2):
        body = request_body(spec, start, end, columns=columns)
        try:
            # Not waiting in here: a 425 means Amazon is already building this report, and the job
            # queue is a better place to wait fifteen minutes than a worker thread is.
            report_id = client.request_report(body, profile_id=profile.amazon_profile_id,
                                              account_id=profile.ads_account_id,
                                              wait_for_in_flight=False)
            report = client.wait_for_report(report_id, profile_id=profile.amazon_profile_id)
            rows = client.report_rows(report, profile_id=profile.amazon_profile_id)
            return rows, report if isinstance(report, dict) else {}
        except AdsError as exc:
            droppable = [c for c in _rejected_columns(str(exc), columns) if c in spec.optional_columns]
            if attempt == 1 and droppable:
                log.warning("Amazon refused %s on %s; retrying without it",
                            ", ".join(droppable), spec.name)
                columns = [c for c in columns if c not in droppable]
                continue
            raise
    return [], {}


def ask_for(client, spec: ReportSpec, profile: PpcProfile, start: date, end: date, *,
            columns: list[str] | None = None) -> str:
    """Ask Amazon to build one report and hand back its id. Does not wait."""
    body = request_body(spec, start, end, columns=columns)
    return client.request_report(body, profile_id=profile.amazon_profile_id,
                                 account_id=profile.ads_account_id, wait_for_in_flight=False)


def collect(client, spec: ReportSpec, profile: PpcProfile,
            report_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """One status check. `None` means Amazon is still building it — come back later.

    Polling from the job queue rather than in a loop is what keeps a three-hour report from holding a
    worker thread and a database transaction open for three hours, and from being declared stuck and
    re-queued by the ten-minute reclaim while it was still perfectly healthy.
    """
    report = client.get(f"{REPORT_PATH}/{report_id}", profile_id=profile.amazon_profile_id,
                        operation="getAsyncReport") or {}
    status = (report.get("status") or "").upper()
    if status in ("FAILURE", "FAILED", "CANCELLED"):
        raise AdsReportFailed(f"Amazon couldn't produce the {spec.key} report ({status.lower()}): "
                              f"{report.get('failureReason') or ''}", status=report)
    if status not in ("COMPLETED", "SUCCESS"):
        return None
    return client.report_rows(report, profile_id=profile.amazon_profile_id), report


def store(session: Session, profile: PpcProfile, spec: ReportSpec, rows: list[dict[str, Any]],
          report: dict[str, Any], start: date, end: date, *,
          today: date | None = None) -> dict[str, Any]:
    """Store one collected report. Separate from collecting it so the write is its own transaction."""
    today = today or metrics.today_for(profile)
    stats = _store(session, profile, spec, rows, report_id=report.get("reportId"),
                   generated_at=report.get("generatedAt"), start=start, end=end, today=today)
    result = {"report": spec.key, "start": start.isoformat(), "end": end.isoformat(),
              "rows": len(rows), **stats.as_dict()}
    log.info("ppc report stored", extra=result)
    return result


def chunks(start: date, end: date, size: int = MAX_RANGE_DAYS) -> list[tuple[date, date]]:
    """Amazon allows 31 days per request, so a longer window becomes several reports."""
    out = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=size - 1), end)
        out.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return out


def earliest_available(spec: ReportSpec, today: date) -> date:
    """The oldest day Amazon will still report. Asking for older data fails the whole request."""
    return today - timedelta(days=spec.retention_days - 1)


def clamp(spec: ReportSpec, start: date, end: date, today: date) -> tuple[date, date] | None:
    """Trim a window to what Amazon still holds, or None when all of it has aged out."""
    floor = earliest_available(spec, today)
    ceiling = today - timedelta(days=1)          # today is still accruing
    start, end = max(start, floor), min(end, ceiling)
    return (start, end) if start <= end else None


# ---------------- reading Amazon's rows ----------------
def _num(row: dict[str, Any], *names: str) -> float:
    for name in names:
        value = row.get(name)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _int(row: dict[str, Any], *names: str) -> int:
    return int(round(_num(row, *names)))


def _text(row: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return None


def _date(row: dict[str, Any]) -> date | None:
    raw = _text(row, "date", "startDate")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _entity_id(spec: ReportSpec, row: dict[str, Any]) -> str | None:
    return _text(row, *spec.id_fields) if spec.id_fields else None


def _optional(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def to_asin_rows(rows: list[dict[str, Any]]) -> list[metrics.MetricRow]:
    """The advertised-product report again, keyed by ASIN instead of by ad id.

    Both are stored. The ad id is the finest grain Amazon reports and is what a per-ad analyser needs;
    the ASIN is what TACOS, break-even ACOS and every "should we advertise this product" question are
    asked about, and an account whose ads carry ad ids would otherwise have no per-ASIN spend at all.
    Summing across entity types would double count, which is why `tacos` reads one type at a time.
    """
    out = []
    for row in rows:
        day, asin = _date(row), _text(row, "advertisedAsin")
        if not (day and asin):
            continue
        out.append(metrics.MetricRow(
            entity_type="asin", amazon_entity_id=asin, date=day,
            amazon_campaign_id=_text(row, "campaignId"), amazon_ad_group_id=_text(row, "adGroupId"),
            impressions=_int(row, "impressions"), clicks=_int(row, "clicks"),
            spend=_num(row, "cost"), sales=_num(row, f"sales{W}"),
            orders=_int(row, f"purchases{W}"), units=_int(row, f"unitsSoldClicks{W}"),
            same_sku_sales=_optional(row.get(f"attributedSalesSameSku{W}")),
            other_sku_sales=_optional(row.get(f"salesOtherSku{W}"))))
    return out


def to_metric_rows(spec: ReportSpec, rows: list[dict[str, Any]]) -> list[metrics.MetricRow]:
    out, unusable = [], 0
    for row in rows:
        day, entity_id = _date(row), _entity_id(spec, row)
        if not (day and entity_id):
            unusable += 1
            continue
        entity_type = spec.entity_type
        # Without an ad id there is no per-ad row to store; the ASIN rows are added separately.
        if spec.key == "advertised_products" and not row.get("adId"):
            continue
        same_sku = _optional(row.get(f"attributedSalesSameSku{W}"))
        other_sku = _optional(row.get(f"salesOtherSku{W}"))
        out.append(metrics.MetricRow(
            entity_type=entity_type, amazon_entity_id=entity_id, date=day,
            amazon_campaign_id=_text(row, "campaignId"),
            amazon_ad_group_id=_text(row, "adGroupId"),
            impressions=_int(row, "impressions"), clicks=_int(row, "clicks"),
            spend=_num(row, "cost"), sales=_num(row, f"sales{W}"),
            orders=_int(row, f"purchases{W}"), units=_int(row, f"unitsSoldClicks{W}"),
            same_sku_sales=same_sku, other_sku_sales=other_sku,
            top_of_search_impression_share=_optional(row.get("topOfSearchImpressionShare"))))
    if unusable:
        # Worth saying out loud: a report Amazon changed the shape of would otherwise look like a
        # quiet day rather than a problem.
        log.warning("%s: %d of %d rows had no date or no id and were not stored",
                    spec.key, unusable, len(rows))
    return out


# Amazon's matchType on a search-term row is not always a keyword match type: an auto-targeted
# campaign reports TARGETING_EXPRESSION or TARGETING_EXPRESSION_PREDEFINED there. Those are 20 and 31
# characters and the column is 20, so passing them through failed the INSERT — which aborted the
# whole run's transaction rather than that one row, and did it again on every retry, because the same
# fortnight is re-read each time. An auto campaign would have stopped the daily load indefinitely.
MATCH_TYPES = {"exact": "exact", "phrase": "phrase", "broad": "broad",
               "targeting_expression": "target", "targeting_expression_predefined": "auto"}


def match_type(raw: str | None) -> str | None:
    """Amazon's matchType in our vocabulary, and never wider than the column."""
    value = (raw or "").strip().lower()
    if not value or value == "-":      # the console writes "-" for a targeting expression
        return None
    known = MATCH_TYPES.get(value)
    if known:
        return known
    log.info("unfamiliar search-term matchType %r; storing it truncated", raw)
    return value[:20]


def placement_key(raw: str | None) -> str:
    text = (raw or "").strip()
    known = PLACEMENTS.get(text.lower())
    if known:
        return known
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "unknown"


def to_placement_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        day = _date(row)
        campaign = _text(row, "campaignId")
        if not (day and campaign):
            continue
        out.append({"amazon_campaign_id": campaign, "date": day,
                    "placement": placement_key(_text(row, "placementClassification", "placement")),
                    "impressions": _int(row, "impressions"), "clicks": _int(row, "clicks"),
                    "spend": _num(row, "cost"), "sales": _num(row, f"sales{W}"),
                    "orders": _int(row, f"purchases{W}"), "units": _int(row, f"unitsSoldClicks{W}")})
    return out


def to_search_term_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        day = _date(row)
        term = _text(row, "searchTerm")
        group = _text(row, "adGroupId")
        if not (day and term and group):
            continue
        out.append({"amazon_campaign_id": _text(row, "campaignId"), "amazon_ad_group_id": group,
                    "amazon_target_id": _text(row, "keywordId", "targetId"),
                    "search_term": term, "match_type": match_type(_text(row, "matchType")),
                    "date": day,
                    "impressions": _int(row, "impressions"), "clicks": _int(row, "clicks"),
                    "spend": _num(row, "cost"), "sales": _num(row, f"sales{W}"),
                    "orders": _int(row, f"purchases{W}"), "units": _int(row, f"unitsSoldClicks{W}")})
    return out


def to_purchased_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        day = _date(row)
        purchased = _text(row, "purchasedAsin")
        campaign = _text(row, "campaignId")
        if not (day and purchased and campaign):
            continue
        out.append({"amazon_campaign_id": campaign, "amazon_ad_group_id": _text(row, "adGroupId"),
                    "advertised_asin": _text(row, "advertisedAsin"),
                    "advertised_sku": _text(row, "advertisedSku"), "purchased_asin": purchased,
                    "date": day, "sales": _num(row, f"sales{W}"),
                    "orders": _int(row, f"purchases{W}"), "units": _int(row, f"unitsSoldClicks{W}")})
    return out


# ---------------- loading one report ----------------
def load(session: Session, profile: PpcProfile, spec: ReportSpec, start: date, end: date, *,
         client=None, today: date | None = None, commit_each: bool = False) -> dict[str, Any]:
    """Fetch one report in 31-day chunks and store every chunk. Returns what happened, per chunk."""
    client = client or api.client()
    today = today or metrics.today_for(profile)
    trimmed = clamp(spec, start, end, today)
    if trimmed is None:
        return {"report": spec.key, "skipped": "outside Amazon's retention window",
                "earliest_available": earliest_available(spec, today).isoformat()}
    start, end = trimmed
    totals = metrics.LoadStats()
    chunk_count = 0
    for chunk_start, chunk_end in chunks(start, end):
        rows, report = fetch(client, spec, profile, chunk_start, chunk_end)
        chunk_count += 1
        stats = _store(session, profile, spec, rows, report_id=report.get("reportId"),
                       generated_at=report.get("generatedAt"), start=chunk_start,
                       end=chunk_end, today=today)
        totals.stored += stats.stored
        totals.restated += stats.restated
        totals.provisional += stats.provisional
        totals.skipped += stats.skipped
        if commit_each:
            # Each chunk is kept on its own. Seven reports in one transaction meant a failure in the
            # last one threw away the six that had already loaded, and left the transaction open
            # across however long Amazon took to build them.
            session.commit()
    result = {"report": spec.key, "start": start.isoformat(), "end": end.isoformat(),
              "requests": chunk_count, **totals.as_dict()}
    log.info("ppc report loaded", extra=result)
    return result


def _store(session: Session, profile: PpcProfile, spec: ReportSpec, rows: list[dict[str, Any]], *,
           report_id: str | None, start: date, end: date, today: date,
           generated_at: str | None = None) -> metrics.LoadStats:
    if spec.key == "placements":
        return metrics.load_placements(session, profile, to_placement_rows(rows),
                                       ad_product=spec.ad_product, report_id=report_id,
                                       today=today)
    if spec.key == "search_terms":
        return metrics.load_search_terms(session, profile, to_search_term_rows(rows),
                                        ad_product=spec.ad_product, report_id=report_id,
                                        today=today)
    if spec.key == "purchased_products":
        return metrics.load_purchased_products(session, profile, to_purchased_rows(rows),
                                               ad_product=spec.ad_product, report_id=report_id,
                                               today=today)
    attribution = attribution_note(spec, generated_at=generated_at)
    stats = metrics.load_daily(session, profile, to_metric_rows(spec, rows),
                               ad_product=spec.ad_product, report_id=report_id,
                               report_start=start, report_end=end, attribution=attribution,
                               today=today)
    if spec.key == "advertised_products":
        by_asin = metrics.load_daily(session, profile, to_asin_rows(rows),
                                     ad_product=spec.ad_product, report_id=report_id,
                                     report_start=start, report_end=end, attribution=attribution,
                                     today=today)
        stats.stored += by_asin.stored
        stats.restated += by_asin.restated
        stats.provisional += by_asin.provisional
        stats.skipped += by_asin.skipped
    return stats


def load_recent(session: Session, profile: PpcProfile | None = None, *, days: int | None = None,
                client=None, only: list[str] | None = None, today: date | None = None,
                commit_each: bool = False) -> dict[str, Any]:
    """The daily job: re-read the last fourteen days of every report and overwrite them.

    Re-pulling rather than appending is the whole point — Amazon's numbers for a day keep changing,
    and a row that is never revisited is a row that is quietly wrong (brief 4.4).
    """
    profile = profile or profiles.require(session)
    client = client or api.client()
    today = today or metrics.today_for(profile)
    start, end = metrics.window(profile, days=days or metrics.RESTATEMENT_DAYS, today=today)
    results = []
    for key in (only or DAILY_ORDER):
        results.append(load(session, profile, SPECS[key], start, end, client=client, today=today,
                            commit_each=commit_each))
    stats = {"profile": profile.amazon_profile_id, "start": start.isoformat(), "end": end.isoformat(),
             "reports": results,
             "stored": sum(r.get("stored", 0) for r in results),
             "restated": sum(r.get("restated", 0) for r in results)}
    log.info("ppc daily load", extra={k: v for k, v in stats.items() if k != "reports"})
    return stats
