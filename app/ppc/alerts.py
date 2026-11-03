"""Alerts: the small number of things worth interrupting the owner's day for.

An alert is not an observation. The analysers (M5) already produce observations by the dozen, and
they arrive in the Monday report where they belong. An alert is the opposite: it says *now*, because
by tomorrow the money is gone or the week is lost.

Six of them, and the list is deliberately short:

* `spend_spike` — spend well above the trailing average for this weekday;
* `cpc_jump` — the auction got more expensive, which is the usual cause of the above;
* `zero_sales_with_spend` — the clearest waste signal there is;
* `budget_exhausted_early` — a campaign went dark while the day still had selling hours in it;
* `account_spend_cap` — the account, not a campaign, has stopped spending: out of budget, payment
  failed, suspended. Nothing below it matters until this is fixed;
* `ad_serving_problem` — ads that cannot serve, from Amazon's own verdict rather than our guess.

Four rules hold the whole thing together.

**Comparisons are same-weekday.** A Tuesday measured against last week's Saturday is a weekday
effect dressed up as an anomaly. Every baseline here is the trailing average of the same weekday.

**An alert is a state, not an event.** Raising the same alert twice because the condition persisted
is how an alerting system teaches people to ignore it. An open alert is updated in place; it closes
itself when the condition goes, and the close is recorded. Muting one stops the messages without
closing it — a muted alert is still an open alert, and `PPC ALERTS` still lists it.

**A check that could not look has no opinion about whether the thing is fixed.** `resolve_missing` is
scoped to the alert types this run actually examined, and a detector that threw, or that declined to
answer, takes its types out of that scope. Silence is not evidence: reading it as an all-clear once
closed a campaign's budget alert at 8pm while the campaign was still dark.

**Nothing fires on provisional data alone where it can be avoided.** Spend is reliable on the day it
lands; sales are not, which is why `zero_sales_with_spend` waits for the day to stop being
provisional before it will call a zero a zero. Yesterday's zero sales is usually just yesterday's
conversions not having arrived yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (PpcAdGroup, PpcAlert, PpcCampaign, PpcHourlyMetric, PpcMetricsDaily,
                           PpcProductAd, PpcProfile, PpcTarget)
from app.ppc import budget, metrics

log = logging.getLogger(__name__)

# How many same-weekday observations to average over. Four weeks is enough to have a baseline and
# short enough that a seasonal shift stops being treated as an anomaly after a month of it.
BASELINE_WEEKS = 4
MIN_BASELINE_DAYS = 2

# Floors, so the alerts stay about money. A campaign that spent £0.40 against a £0.10 average is a
# 300% spike and worth nobody's morning.
MIN_SPEND = 5.00
MIN_CLICKS = 10

# Every detector here reads Sponsored Products tables. Sponsored Brands and Display get their own
# thresholds and in Display's case their own metrics entirely, so this is stated rather than assumed.
AD_PRODUCT = "sp"

# Losing the second half of the day costs less than losing the first.
HALF_THE_DAY = 0.5

# Amazon's own serving verdicts, grouped by what the owner would have to do about them. Taken from the
# Sponsored Products spec's ServingStatus enums rather than invented.
ACCOUNT_BLOCKED = {
    "ACCOUNT_OUT_OF_BUDGET": "the account is out of budget",
    "ADVERTISER_ACCOUNT_OUT_OF_BUDGET": "the account is out of budget",
    "ADVERTISER_OUT_OF_BUDGET": "the advertiser is out of budget",
    "ADVERTISER_EXCEED_SPENDS_LIMIT": "the account has hit its spend limit",
    "ADVERTISER_PAYMENT_FAILURE": "a payment failed",
    "ADVERTISER_POLICING_SUSPENDED": "the account is suspended",
    "PORTFOLIO_OUT_OF_BUDGET": "a portfolio is out of budget",
}

# Things that stop one entity serving. Severity is ours: "nobody can buy it" outranks "needs a bid".
SERVING_PROBLEMS = {
    "REJECTED": ("high", "it was rejected"),
    "AD_GROUP_POLICING_CREATIVE_REJECTED": ("high", "the creative was rejected"),
    "SECURITY_SCAN_REJECTED": ("high", "it failed a security scan"),
    "CAMPAIGN_INCOMPLETE": ("high", "the campaign is incomplete"),
    "AD_GROUP_INCOMPLETE": ("high", "the ad group is incomplete"),
    "CAMPAIGN_ADS_NOT_DELIVERING": ("high", "its ads aren't delivering"),
    "AD_NOT_BUYABLE": ("high", "the product isn't buyable"),
    "NOT_BUYABLE": ("high", "the product isn't buyable"),
    "AD_NO_PURCHASABLE_OFFER": ("high", "there's no purchasable offer"),
    "NO_PURCHASABLE_OFFER": ("high", "there's no purchasable offer"),
    "AD_OUT_OF_STOCK": ("high", "the product is out of stock"),
    "OUT_OF_STOCK": ("high", "the product is out of stock"),
    "NO_INVENTORY": ("high", "there's no inventory"),
    "AD_NOT_IN_BUYBOX": ("medium", "the product has lost the Buy Box"),
    "NOT_IN_BUYBOX": ("medium", "the product has lost the Buy Box"),
    "AD_LANDING_PAGE_NOT_AVAILABLE": ("high", "the product page isn't available"),
    "LANDING_PAGE_NOT_AVAILABLE": ("high", "the product page isn't available"),
    "AD_MISSING_IMAGE": ("medium", "the listing has no image"),
    "MISSING_IMAGE": ("medium", "the listing has no image"),
    "AD_GROUP_LOW_BID": ("medium", "the bid is below Amazon's minimum"),
    "AD_INELIGIBLE": ("medium", "Amazon says it's ineligible"),
    "INELIGIBLE": ("medium", "Amazon says it's ineligible"),
    "TARGETING_CLAUSE_BLOCKED": ("medium", "the target is blocked"),
    "TARGETING_CLAUSE_POLICING_SUSPENDED": ("high", "the target is suspended"),
    # Pending review is normal on day one and a problem on day five. Low, so it is reported without
    # pretending to be urgent, and the age is in the details.
    "PENDING_REVIEW": ("low", "it's waiting on Amazon's review"),
    "ADVERTISER_POLICING_PENDING_REVIEW": ("low", "the account is waiting on Amazon's review"),
    "AD_GROUP_POLICING_PENDING_REVIEW": ("low", "the ad group is waiting on Amazon's review"),
    "AD_POLICING_PENDING_REVIEW": ("low", "the ad is waiting on Amazon's review"),
}

# Statuses that mean "working", "you paused it", or "we can't tell" — none of them are news.
# CAMPAIGN_OUT_OF_BUDGET is deliberately absent: that is the budget alert's job, with a number
# attached, not a vague "something is wrong".
BENIGN = {"ELIGIBLE", "AD_ELIGIBLE", "AD_STATUS_LIVE", "ADVERTISER_STATUS_ENABLED",
          "CAMPAIGN_STATUS_ENABLED", "AD_GROUP_STATUS_ENABLED", "PORTFOLIO_STATUS_ENABLED",
          "TARGETING_CLAUSE_STATUS_LIVE", "CAMPAIGN_OUT_OF_BUDGET", "PENDING_START_DATE",
          "CAMPAIGN_PENDING_START_DATE", "PORTFOLIO_PENDING_START_DATE", "ENDED", "CAMPAIGN_ENDED",
          "PORTFOLIO_ENDED", "STATUS_UNAVAILABLE", "OTHER"}


@dataclass
class Finding:
    """One thing worth saying, before it becomes a row."""

    type: str
    severity: str
    message: str
    ad_product: str = AD_PRODUCT
    entity_type: str | None = None
    amazon_entity_id: str | None = None
    entity_label: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str | None, str | None]:
        return (self.type, self.entity_type, self.amazon_entity_id)


# ---------------- the ledger ----------------
def open_alerts(session: Session, profile: PpcProfile, *, types: Iterable[str] | None = None,
                include_muted: bool = True) -> list[PpcAlert]:
    """Everything still wrong, muted included.

    Muting means "stop messaging me about this", not "pretend it is fixed" — which is exactly what
    `PPC ALERTS` told the owner when this filtered on `status == "open"`: muting one alert made it vanish
    from the list while the reply promised it would stay there. A muted alert is still an open alert;
    the only thing muting changes is whether it generates a message.
    """
    statuses = ("open", "muted") if include_muted else ("open",)
    query: Select = select(PpcAlert).where(PpcAlert.profile_id == profile.id,
                                           PpcAlert.ad_product == AD_PRODUCT,
                                           PpcAlert.status.in_(statuses))
    if types is not None:
        query = query.where(PpcAlert.type.in_(list(types)))
    return list(session.scalars(query.order_by(PpcAlert.created_at)).all())


def raise_finding(session: Session, profile: PpcProfile, finding: Finding) -> PpcAlert:
    """Open an alert, or refresh the one that is already open for this thing.

    Refreshing rather than re-raising is the whole point: a campaign that has been out of budget for
    three days is one alert with a current number in it, not three alerts.
    """
    existing = session.scalars(
        select(PpcAlert).where(PpcAlert.profile_id == profile.id,
                               PpcAlert.ad_product == finding.ad_product,
                               PpcAlert.type == finding.type,
                               PpcAlert.entity_type.is_(None) if finding.entity_type is None
                               else PpcAlert.entity_type == finding.entity_type,
                               PpcAlert.amazon_entity_id.is_(None) if finding.amazon_entity_id is None
                               else PpcAlert.amazon_entity_id == finding.amazon_entity_id,
                               PpcAlert.status.in_(("open", "muted")))).first()
    details = dict(finding.details)
    details["message"] = finding.message
    if existing is not None:
        # A muted alert stays muted. The owner said he didn't want to hear about this one, and a worsening
        # number is not consent to start again.
        existing.details = {**(existing.details or {}), **details,
                            "seen_count": (existing.details or {}).get("seen_count", 1) + 1,
                            "last_seen_at": datetime.now(timezone.utc).isoformat()}
        existing.severity = finding.severity
        existing.entity_label = finding.entity_label or existing.entity_label
        return existing
    row = PpcAlert(profile_id=profile.id, ad_product=finding.ad_product, type=finding.type,
                   severity=finding.severity,
                   entity_type=finding.entity_type, amazon_entity_id=finding.amazon_entity_id,
                   entity_label=finding.entity_label, status="open",
                   details={**details, "seen_count": 1,
                            "first_seen_at": datetime.now(timezone.utc).isoformat()})
    session.add(row)
    session.flush()
    return row


def resolve_missing(session: Session, profile: PpcProfile, findings: list[Finding], *,
                    types: Iterable[str]) -> list[PpcAlert]:
    """Close the alerts whose condition has gone, within the types this run actually looked at.

    Scoping by type matters. A run that only checked budgets must not close a serving problem it
    never examined; silence about something is not evidence it is fixed.
    """
    live = {f.key for f in findings}
    closed = []
    for row in open_alerts(session, profile, types=types):
        if (row.type, row.entity_type, row.amazon_entity_id) in live:
            continue
        row.status = "resolved"
        row.resolved_at = datetime.now(timezone.utc)
        closed.append(row)
    if closed:
        session.flush()
    return closed


# ---------------- baselines ----------------
def _same_weekday(session: Session, profile: PpcProfile, day: date, *,
                  weeks: int = BASELINE_WEEKS) -> Select:
    days = [day - timedelta(days=7 * n) for n in range(1, weeks + 1)]
    return (select(PpcMetricsDaily.amazon_entity_id,
                   func.avg(PpcMetricsDaily.spend).label("spend"),
                   func.avg(PpcMetricsDaily.clicks).label("clicks"),
                   func.avg(PpcMetricsDaily.sales).label("sales"),
                   func.count().label("days"))
            .where(PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
               PpcMetricsDaily.ad_product == AD_PRODUCT,
                   PpcMetricsDaily.entity_type == "campaign",
                   PpcMetricsDaily.date.in_(days))
            .group_by(PpcMetricsDaily.amazon_entity_id))


def _day_rows(session: Session, profile: PpcProfile, day: date) -> dict[str, Any]:
    rows = session.execute(
        select(PpcMetricsDaily.amazon_entity_id, PpcMetricsDaily.spend, PpcMetricsDaily.clicks,
               PpcMetricsDaily.sales, PpcMetricsDaily.orders)
        .where(PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
               PpcMetricsDaily.ad_product == AD_PRODUCT,
               PpcMetricsDaily.entity_type == "campaign",
               PpcMetricsDaily.date == day)).all()
    return {r.amazon_entity_id: r for r in rows}


def latest_day(session: Session, profile: PpcProfile, *, settled_only: bool = False,
               today: date | None = None) -> date | None:
    """The most recent day we hold campaign metrics for, optionally the most recent reliable one.

    Both are needed, and conflating them made one alert impossible. Spend and clicks are right on
    the day they arrive, so spikes are judged on the newest day. Conversions are not — Amazon keeps
    attributing them for days — so a zero-sales claim has to wait for a day that has stopped moving.
    Checking for zero sales on the newest day meant checking a day that is always provisional, and
    the alert never fired at all.
    """
    query = (select(func.max(PpcMetricsDaily.date))
             .where(PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
               PpcMetricsDaily.ad_product == AD_PRODUCT,
                    PpcMetricsDaily.entity_type == "campaign"))
    if settled_only:
        today = today or metrics.today_for(profile)
        query = query.where(PpcMetricsDaily.date <= today - timedelta(days=metrics.PROVISIONAL_DAYS))
    return session.scalar(query)


def _labels(session: Session, profile: PpcProfile) -> dict[str, str]:
    rows = session.execute(
        select(PpcCampaign.amazon_campaign_id, PpcCampaign.name)
        .where(PpcCampaign.profile_id == profile.id,
               PpcCampaign.ad_product == AD_PRODUCT)).all()
    return {r.amazon_campaign_id: r.name or r.amazon_campaign_id for r in rows}


# ---------------- detectors on daily data ----------------
def daily_anomalies(session: Session, profile: PpcProfile, day: date | None = None) -> list[Finding]:
    """Spend spikes, CPC jumps and waste, judged against the same weekday in recent weeks.

    Two days are examined, not one. Spikes and CPC jumps go on the newest day we hold, because spend
    and clicks are reliable as soon as they land. Zero-sales goes on the newest day that has stopped
    being provisional, because conversions arrive late and yesterday's zero is usually just
    yesterday's orders not having been attributed yet.

    Naming a `day` explicitly overrides both — that is the on-demand path, where the caller has said
    which day they mean.
    """
    settings = get_settings()
    today = metrics.today_for(profile)
    asked_for = day is not None
    day = day or latest_day(session, profile)
    if day is None:
        return []
    waste_day = day if asked_for else latest_day(session, profile, settled_only=True, today=today)
    rows = _day_rows(session, profile, day)
    if not rows:
        return []
    baseline = {r.amazon_entity_id: r for r in session.execute(_same_weekday(session, profile, day)).all()}
    labels = _labels(session, profile)
    findings: list[Finding] = []

    for campaign_id, row in rows.items():
        base = baseline.get(campaign_id)
        label = labels.get(campaign_id, campaign_id)
        spend = float(row.spend or 0)
        clicks = int(row.clicks or 0)
        sales = float(row.sales or 0)
        common = {"day": day.isoformat(), "spend": round(spend, 2), "clicks": clicks,
                  "sales": round(sales, 2), "weekday": day.strftime("%A")}

        if base is not None and base.days >= MIN_BASELINE_DAYS and spend >= MIN_SPEND:
            base_spend = float(base.spend or 0)
            if base_spend > 0:
                change = (spend - base_spend) / base_spend * 100
                if change >= settings.ppc_anomaly_spend_spike_pct:
                    findings.append(Finding(
                        type="spend_spike",
                        severity="high" if change >= 2 * settings.ppc_anomaly_spend_spike_pct
                        else "medium",
                        message=(f"{label} spent {metrics.money(spend, profile.currency)} on "
                                 f"{day.strftime('%A')}, {change:.0f}% above its usual "
                                 f"{metrics.money(base_spend, profile.currency)} for that day."),
                        entity_type="campaign", amazon_entity_id=campaign_id, entity_label=label,
                        details={**common, "baseline_spend": round(base_spend, 2),
                                 "change_pct": round(change, 1),
                                 "baseline_days": int(base.days)}))

            base_clicks = float(base.clicks or 0)
            cpc = spend / clicks if clicks else None
            base_cpc = base_spend / base_clicks if base_clicks else None
            if cpc and base_cpc and clicks >= MIN_CLICKS and base_clicks >= MIN_CLICKS:
                change = (cpc - base_cpc) / base_cpc * 100
                if change >= settings.ppc_anomaly_cpc_jump_pct:
                    findings.append(Finding(
                        type="cpc_jump", severity="medium",
                        message=(f"{label} is paying {metrics.money(cpc, profile.currency)} a click, "
                                 f"up {change:.0f}% from its usual "
                                 f"{metrics.money(base_cpc, profile.currency)}."),
                        entity_type="campaign", amazon_entity_id=campaign_id, entity_label=label,
                        details={**common, "cpc": round(cpc, 2), "baseline_cpc": round(base_cpc, 2),
                                 "change_pct": round(change, 1)}))

    findings.extend(_waste(session, profile, waste_day, today=today, labels=labels))
    return findings


def _waste(session: Session, profile: PpcProfile, day: date | None, *, today: date,
           labels: dict[str, str]) -> list[Finding]:
    """Campaigns that spent money on a finished day and sold nothing.

    Only a day Amazon has stopped restating can support this. On a provisional day the sales column
    is still filling in, so "spent £45 and sold nothing" would be a statement about our own data
    arriving rather than about the account.
    """
    if day is None or metrics.is_provisional(day, today):
        return []
    findings: list[Finding] = []
    for campaign_id, row in _day_rows(session, profile, day).items():
        spend = float(row.spend or 0)
        if spend < MIN_SPEND or float(row.sales or 0) > 0:
            continue
        label = labels.get(campaign_id, campaign_id)
        findings.append(Finding(
            type="zero_sales_with_spend",
            severity="high" if spend >= 4 * MIN_SPEND else "medium",
            message=(f"{label} spent {metrics.money(spend, profile.currency)} on "
                     f"{day.strftime('%A')} and sold nothing."),
            entity_type="campaign", amazon_entity_id=campaign_id, entity_label=label,
            details={"day": day.isoformat(), "spend": round(spend, 2),
                     "clicks": int(row.clicks or 0), "sales": 0.0,
                     "weekday": day.strftime("%A")}))
    return findings


# ---------------- detectors on intraday data ----------------
def budget_findings(session: Session, profile: PpcProfile, pace: budget.Pace) -> list[Finding]:
    """Campaigns that have gone dark with selling hours left, and the account-level version."""
    findings: list[Finding] = []
    clock = metrics.clock_of(pace.at.hour)
    for reading in budget.exhausted_early(pace):
        hours_left = round((1 - pace.elapsed) * 24)
        findings.append(Finding(
            type="budget_exhausted_early",
            # Half the day is the line. Going dark with most of the selling day still ahead is a
            # different size of problem from going dark after tea, and both are different from the
            # 11pm case, which `budget.exhausted_early` has already declined to report at all.
            severity="high" if pace.elapsed < HALF_THE_DAY else "medium",
            message=(f"{reading.label} has used {reading.used_pct:.0f}% of its "
                     f"{metrics.money(reading.budget or 0, reading.currency)} budget by {clock}, "
                     f"with about {hours_left} hours of the day left."),
            entity_type="campaign", amazon_entity_id=reading.amazon_campaign_id,
            entity_label=reading.label,
            details={"used_pct": round(reading.used_pct or 0, 1),
                     "budget": reading.budget, "spent": reading.spent,
                     "day": pace.day.isoformat(), "hour": pace.at.hour,
                     "day_elapsed_pct": round(pace.elapsed * 100, 1),
                     "as_of": reading.as_of.isoformat() if reading.as_of else None}))
    return findings


def serving_findings(session: Session, profile: PpcProfile) -> list[Finding]:
    """Amazon's own verdict on what can't serve — the account first, then the entities.

    When the account is blocked, every campaign under it reports the same status. Raising sixty
    identical alerts would bury the one sentence that matters, so the account case is collapsed into
    a single alert and the entity scan skips those statuses.
    """
    findings: list[Finding] = []
    blocked: dict[str, int] = {}

    scans = (("campaign", PpcCampaign, PpcCampaign.amazon_campaign_id, PpcCampaign.name),
             ("ad_group", PpcAdGroup, PpcAdGroup.amazon_ad_group_id, PpcAdGroup.name),
             ("product_ad", PpcProductAd, PpcProductAd.amazon_ad_id, PpcProductAd.asin),
             ("target", PpcTarget, PpcTarget.amazon_target_id, PpcTarget.expression))
    for entity_type, model, id_column, label_column in scans:
        rows = session.execute(
            select(id_column, label_column, model.serving_status)
            .where(model.profile_id == profile.id, model.ad_product == AD_PRODUCT,
                   model.archived_at.is_(None),
                   model.state == "enabled", model.serving_status.isnot(None))).all()
        for entity_id, label, status in rows:
            status = (status or "").upper()
            if not status or status in BENIGN:
                continue
            if status in ACCOUNT_BLOCKED:
                blocked[status] = blocked.get(status, 0) + 1
                continue
            known = SERVING_PROBLEMS.get(status)
            if known is None:
                # An unmapped status is reported rather than dropped: Amazon adds to this enum, and
                # silently ignoring a new one would hide a real outage behind our own staleness.
                severity, because = "low", f"Amazon reports {status.lower().replace('_', ' ')}"
                log.info("serving status %s is not in our map; reporting it as-is", status)
            else:
                severity, because = known
            findings.append(Finding(
                type="ad_serving_problem", severity=severity,
                message=f"{label or entity_id} isn't serving — {because}.",
                entity_type=entity_type, amazon_entity_id=str(entity_id),
                entity_label=str(label) if label else None,
                details={"serving_status": status, "reason": because}))

    if blocked:
        reasons = sorted(ACCOUNT_BLOCKED[s] for s in blocked)
        affected = sum(blocked.values())
        findings.append(Finding(
            type="account_spend_cap", severity="high",
            message=(f"Advertising has stopped across the account — {reasons[0]}. "
                     f"{affected} live entities can't serve until that's sorted."),
            details={"statuses": blocked, "reasons": reasons, "affected": affected}))
    return findings


def intraday_spike(session: Session, profile: PpcProfile, *, at: datetime | None = None
                   ) -> list[Finding]:
    """Same-day spend spikes, which need hourly data and so need Marketing Stream.

    Without the stream this returns nothing at all rather than approximating. The daily detector
    already covers yesterday; pretending to cover today from yesterday's data would be a worse
    answer than saying nothing.
    """
    settings = get_settings()
    now = at or metrics.now_for(profile)
    day, hour = now.date(), now.hour
    if hour < 2:
        return []       # too little of the day has happened to compare anything to anything
    # Completed hours only, on both sides. The current hour is part-finished today and was a whole
    # hour on every past day, so including it compares ten and a half hours against eleven — a bias
    # that is small at 11pm and large at 2am, and always in the direction of hiding a spike.
    through = hour - 1
    today = session.execute(
        select(PpcHourlyMetric.amazon_campaign_id, func.sum(PpcHourlyMetric.spend).label("spend"))
        .where(PpcHourlyMetric.amazon_profile_id == profile.amazon_profile_id,
               PpcHourlyMetric.ad_product == AD_PRODUCT,
               PpcHourlyMetric.date == day, PpcHourlyMetric.hour <= through)
        .group_by(PpcHourlyMetric.amazon_campaign_id)).all()
    if not today:
        return []
    days = [day - timedelta(days=7 * n) for n in range(1, BASELINE_WEEKS + 1)]
    # Counting only the dates that carried traffic. `budget.record` writes this table too, with the
    # metric columns left at zero, so a plain count(distinct date) would include dates that
    # contributed no spend — inflating the divisor, shrinking the baseline, and firing a spike alert
    # on a campaign spending exactly what it always spends.
    base_rows = session.execute(
        select(PpcHourlyMetric.amazon_campaign_id,
               func.sum(PpcHourlyMetric.spend).label("spend"),
               func.count(func.distinct(
                   case((PpcHourlyMetric.spend > 0, PpcHourlyMetric.date), else_=None)
               )).label("days"))
        .where(PpcHourlyMetric.amazon_profile_id == profile.amazon_profile_id,
               PpcHourlyMetric.ad_product == AD_PRODUCT,
               PpcHourlyMetric.date.in_(days), PpcHourlyMetric.hour <= through)
        .group_by(PpcHourlyMetric.amazon_campaign_id)).all()
    baseline = {r.amazon_campaign_id: r for r in base_rows}
    labels = _labels(session, profile)
    findings: list[Finding] = []
    for row in today:
        base = baseline.get(row.amazon_campaign_id)
        if base is None or base.days < MIN_BASELINE_DAYS:
            continue
        spend = float(row.spend or 0)
        base_spend = float(base.spend or 0) / base.days
        if spend < MIN_SPEND or base_spend <= 0:
            continue
        change = (spend - base_spend) / base_spend * 100
        if change < settings.ppc_anomaly_spend_spike_pct:
            continue
        label = labels.get(row.amazon_campaign_id, row.amazon_campaign_id)
        findings.append(Finding(
            type="spend_spike",
            severity="high" if change >= 2 * settings.ppc_anomaly_spend_spike_pct else "medium",
            message=(f"{label} has spent {metrics.money(spend, profile.currency)} so far today, "
                     f"{change:.0f}% more than it usually has by {metrics.clock_of(through + 1)} on a "
                     f"{day.strftime('%A')}."),
            entity_type="campaign", amazon_entity_id=row.amazon_campaign_id, entity_label=label,
            details={"day": day.isoformat(), "through_hour": through, "spend": round(spend, 2),
                     "baseline_spend": round(base_spend, 2), "change_pct": round(change, 1),
                     "baseline_days": int(base.days), "source": "marketing_stream"}))
    return findings


# ---------------- the run ----------------
@dataclass
class Sweep:
    """What one pass over the account found, and what it closed."""

    profile_id: str
    raised: list[PpcAlert] = field(default_factory=list)
    resolved: list[PpcAlert] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.findings and not self.resolved:
            return "Nothing wrong that I can see."
        parts = []
        if self.findings:
            worst = min(self.findings, key=lambda f: ("high", "medium", "low").index(f.severity)
                        if f.severity in ("high", "medium", "low") else 3)
            parts.append(f"{len(self.findings)} alert(s), worst first: {worst.message}")
        if self.resolved:
            parts.append(f"{len(self.resolved)} cleared.")
        return " ".join(parts)


def run(session: Session, profile: PpcProfile, *, client=None, day: date | None = None,
        intraday: bool = True, at: datetime | None = None) -> Sweep:
    """One pass: look at everything we can, raise what's new, close what's fixed.

    A detector that throws takes itself out of the sweep and nothing else. Its alert types are then
    left out of the resolve step too, because a check that failed has no opinion about whether the
    thing it checks is fixed.
    """
    sweep = Sweep(profile_id=profile.amazon_profile_id)
    findings: list[Finding] = []
    # Types some detector failed on. `spend_spike` is produced by two detectors — the daily one and
    # the hourly one — so "one of them succeeded" is not enough to resolve it: the daily detector
    # knowing nothing about a spike happening *today* would otherwise close an alert the hourly
    # detector raised an hour ago and then failed to re-confirm.
    failed: set[str] = set()

    def attempt(name: str, types: tuple[str, ...], fn) -> None:
        try:
            found = fn()
        except Exception as exc:                      # noqa: BLE001 — one bad check, not a bad sweep
            log.exception("alert check %s failed", name)
            sweep.errors.append(f"{name}: {exc}")
            failed.update(types)
            return
        findings.extend(found)
        sweep.checked.extend(types)

    attempt("daily", ("spend_spike", "cpc_jump", "zero_sales_with_spend"),
            lambda: daily_anomalies(session, profile, day))
    attempt("serving", ("ad_serving_problem", "account_spend_cap"),
            lambda: serving_findings(session, profile))
    if intraday:
        attempt("intraday", ("spend_spike",), lambda: intraday_spike(session, profile, at=at))

        # Budget exhaustion is checked, but only *claims* to have checked while there is still day
        # left. `budget.exhausted_early` deliberately returns nothing after the cutoff, because a
        # campaign spending its budget by the evening has done its job — and the resolve step read
        # that silence as "fixed" and closed the 11am alert about a campaign that was still dark.
        # Declaring the type unchecked is the honest form of "I didn't look", and it is the rule
        # `resolve_missing` exists to enforce.
        def budget_check() -> list[Finding]:
            pace = budget.poll(session, profile, client=client, at=at)
            found = budget_findings(session, profile, pace)
            if pace.elapsed >= get_settings().ppc_budget_early_fraction or not pace.usable:
                # Past the cutoff, or with no current reading, this detector has no opinion. Saying
                # so is what keeps the resolve step from reading its silence as an all-clear.
                failed.add("budget_exhausted_early")
            return found

        attempt("budget", ("budget_exhausted_early",), budget_check)

    # Two detectors can report the same spike on the same campaign — the hourly one through today and
    # the daily one through yesterday. One campaign, one alert, and the intraday message wins because
    # it is about now.
    seen: dict[tuple, Finding] = {}
    for finding in findings:
        previous = seen.get(finding.key)
        if previous is None or finding.details.get("source") == "marketing_stream":
            seen[finding.key] = finding
    deduped = list(seen.values())

    for finding in deduped:
        sweep.raised.append(raise_finding(session, profile, finding))
    sweep.findings = deduped
    sweep.checked = [t for t in dict.fromkeys(sweep.checked) if t not in failed]
    sweep.resolved = resolve_missing(session, profile, deduped, types=set(sweep.checked))
    return sweep
