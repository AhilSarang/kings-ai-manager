"""What the day looks like: which hours sell, which hours only spend.

Daily totals hide this completely. A campaign at a 40% ACOS for the month can be at 18% between 6pm
and 11pm and 120% between 1am and 6am, and the monthly figure — the only one most people ever see —
is a blend of a good business and a bad one.

Two honesty problems make this harder than it sounds, and both are handled here rather than papered
over.

**Hours are small.** Splitting a month into 24 buckets divides the data by 24; into 168 hour-of-week
buckets, by 168. At that size noise looks exactly like a pattern, and "never advertise on Tuesdays at
3am" is the sort of confident nonsense a thin bucket produces. So an hour is only called good or bad
when a two-proportion test on its conversion rate against the rest of the day says the difference is
real, not merely visible. Hours that fail that test are still reported — as shape, not as a verdict.

**Nothing can act on it directly.** Sponsored Products has no hour-of-day bid modifier. Acting on
dayparting means making timed changes on a schedule, which is why `dayparting` sits in
`apply.ADVISORY`: approving one records that the owner wants it, and the change is his to make. Promising
an automatic 1am bid cut we cannot execute would be worse than saying so.

Everything here reads `ppc_hourly_metrics`, which is filled by Marketing Stream. Without that
subscription the module returns an empty shape and says why — it does not estimate a daily curve from
daily totals, because there is no honest way to do that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import PpcCampaign, PpcHourlyMetric, PpcProfile
from app.ppc import metrics, statistics
from app.ppc.candidates import Candidate

log = logging.getLogger(__name__)

# The analysis window. Four weeks gives each hour-of-week bucket four observations, which is thin;
# it gives each hour-of-day bucket twenty-eight, which is workable. Both are produced, and only the
# second is allowed to generate a recommendation.
WINDOW_DAYS = 28

# Floors below which an hour is shape, not evidence.
MIN_DAYS = 14               # distinct dates contributing to the bucket
MIN_CLICKS = 30             # clicks in the bucket, over the whole window
MIN_SPEND = 10.00           # money in the bucket, over the whole window

# How far an hour's ACOS has to sit from the account's before it is worth a sentence, on top of the
# significance test. Both gates, because a real 3% difference is still not worth anyone's attention.
MATERIAL_GAP = 0.30

# The interval and the p-value both have to clear this. See `Verdict.proven` for why one is not
# enough on its own.
ALPHA = 0.05

# Sponsored Products. Display's hourly shape is a different question rather than more data about
# the same one: it bids differently and its sales include view-through attribution.
AD_PRODUCT = "sp"

HOURS = tuple(range(24))
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass
class Bucket:
    """One hour, either of the day (weekday None) or of the week."""

    hour: int
    weekday: int | None
    impressions: int = 0
    clicks: int = 0
    spend: float = 0.0
    sales: float = 0.0
    orders: int = 0
    days: int = 0

    @property
    def acos(self) -> float | None:
        """None, not infinity: "spent and sold nothing" is a sentence, not a ratio."""
        return (self.spend / self.sales) if self.sales > 0 else None

    @property
    def cvr(self) -> float | None:
        return (self.orders / self.clicks) if self.clicks else None

    @property
    def cpc(self) -> float | None:
        return (self.spend / self.clicks) if self.clicks else None

    @property
    def label(self) -> str:
        clock = metrics.clock_of(self.hour)
        return clock if self.weekday is None else f"{WEEKDAYS[self.weekday]} {clock}"

    @property
    def substantial(self) -> bool:
        return (self.days >= MIN_DAYS and self.clicks >= MIN_CLICKS
                and self.spend >= MIN_SPEND)

    def as_dict(self) -> dict[str, Any]:
        return {"hour": self.hour, "weekday": self.weekday, "label": self.label,
                "impressions": self.impressions, "clicks": self.clicks,
                "spend": round(self.spend, 2), "sales": round(self.sales, 2),
                "orders": self.orders, "days": self.days,
                "acos": None if self.acos is None else round(self.acos, 4),
                "cvr": None if self.cvr is None else round(self.cvr, 4),
                "cpc": None if self.cpc is None else round(self.cpc, 2)}


@dataclass
class Verdict:
    """One hour, judged — with the test that judged it attached."""

    bucket: Bucket
    kind: str                      # "wasteful" | "profitable"
    comparison: statistics.Comparison | None
    rest_of_day_acos: float | None
    gap: float | None              # how far this hour's ACOS sits from the rest of the day's

    @property
    def proven(self) -> bool:
        """Whether the conversion-rate test actually supports *this* label.

        Three gates, and the first two are the ones an earlier version of this got wrong.

        **Direction.** `Comparison.significant` is two-sided: it says this hour's conversion rate
        differs from the rest of the day, not which way. Because ACOS is cpc ÷ (cvr × order value),
        an hour can convert significantly *better* and still carry a worse ACOS through a higher
        CPC — and that hour was being reported as "proven wasteful" on the strength of evidence that
        it converts well. So a wasteful label needs the conversion rate to be significantly lower,
        and a profitable one needs it higher.

        **The p-value as well as the interval.** The interval is unpooled, so an hour with zero
        orders contributes exactly zero variance and the interval narrows to the other arm's width
        regardless of how few clicks this hour had. Thirty clicks and no orders came out "proven"
        on an interval while the pooled p-value was 0.068. Both gates now have to agree.

        **The test has to exist**, which it does not when there is no rest-of-day to compare to.
        """
        test = self.comparison
        if test is None or not test.significant or test.p_value >= ALPHA:
            return False
        if self.kind == "wasteful":
            return test.difference < 0
        return test.difference > 0

    def as_dict(self) -> dict[str, Any]:
        return {"hour": self.bucket.hour, "label": self.bucket.label, "kind": self.kind,
                "gap": None if self.gap is None else round(self.gap, 3),
                "proven": self.proven, "bucket": self.bucket.as_dict(),
                "test": self.comparison.as_dict() if self.comparison else None}


@dataclass
class Shape:
    """A profile's day, as far as the data supports saying anything about it."""

    profile_id: str
    from_date: date | None = None
    to_date: date | None = None
    by_hour: list[Bucket] = field(default_factory=list)
    by_hour_of_week: list[Bucket] = field(default_factory=list)
    campaign_id: str | None = None
    currency: str = "USD"                # the marketplace's own, so summary() can quote money
    reason: str | None = None            # why there is nothing here, when there is nothing

    @property
    def has_data(self) -> bool:
        return any(b.clicks or b.spend for b in self.by_hour)

    @property
    def days(self) -> int:
        return max((b.days for b in self.by_hour), default=0)

    @property
    def total_spend(self) -> float:
        return sum(b.spend for b in self.by_hour)

    @property
    def total_sales(self) -> float:
        return sum(b.sales for b in self.by_hour)

    @property
    def account_acos(self) -> float | None:
        return (self.total_spend / self.total_sales) if self.total_sales > 0 else None

    @property
    def coverage(self) -> str:
        """How much weight the conclusions below can carry."""
        if not self.has_data:
            return "none"
        substantial = sum(1 for b in self.by_hour if b.substantial)
        if self.days < MIN_DAYS or substantial < 6:
            return "thin"
        return "good"


# `ppc_hourly_metrics` has two writers. Marketing Stream fills the metric columns; `budget.record`
# writes `budget_consumed_pct` every couple of hours and leaves the metrics at their default zero.
# Both land in the same row shape, so a plain `count(distinct date)` counts budget-reading dates as
# days of traffic — and three days of real hourly data behind a month of budget readings reported
# itself as twenty-four days, cleared `coverage == "good"`, and recommended off three days. Every
# day count here therefore counts only the dates that actually carried traffic.
_HAS_TRAFFIC = or_(PpcHourlyMetric.impressions > 0, PpcHourlyMetric.clicks > 0,
                   PpcHourlyMetric.spend > 0)


def _rows(session: Session, profile: PpcProfile, *, start: date, end: date,
          campaign_id: str | None, group_by_weekday: bool):
    traffic_days = func.count(func.distinct(
        case((_HAS_TRAFFIC, PpcHourlyMetric.date), else_=None))).label("days")
    columns = [PpcHourlyMetric.hour,
               func.sum(PpcHourlyMetric.impressions).label("impressions"),
               func.sum(PpcHourlyMetric.clicks).label("clicks"),
               func.sum(PpcHourlyMetric.spend).label("spend"),
               func.sum(PpcHourlyMetric.sales).label("sales"),
               func.sum(PpcHourlyMetric.orders).label("orders"),
               traffic_days]
    grouping = [PpcHourlyMetric.hour]
    if group_by_weekday:
        columns.insert(1, PpcHourlyMetric.weekday)
        grouping.append(PpcHourlyMetric.weekday)
    query = (select(*columns)
             .where(PpcHourlyMetric.amazon_profile_id == profile.amazon_profile_id,
                    PpcHourlyMetric.ad_product == AD_PRODUCT,
                    PpcHourlyMetric.date >= start, PpcHourlyMetric.date <= end)
             .group_by(*grouping))
    if campaign_id:
        query = query.where(PpcHourlyMetric.amazon_campaign_id == campaign_id)
    return session.execute(query).all()


def shape(session: Session, profile: PpcProfile, *, today: date | None = None,
          days: int = WINDOW_DAYS, campaign_id: str | None = None) -> Shape:
    """Aggregate the window into 24 hours of the day and up to 168 hours of the week.

    The window ends at the last settled-enough day rather than today: today is a partial day, and a
    half-finished Tuesday would drag every afternoon bucket down.
    """
    today = today or metrics.today_for(profile)
    end = today - timedelta(days=metrics.PROVISIONAL_DAYS)
    start = end - timedelta(days=days - 1)
    out = Shape(profile_id=profile.amazon_profile_id, from_date=start, to_date=end,
                campaign_id=campaign_id, currency=profile.currency or "USD")

    hourly = _rows(session, profile, start=start, end=end, campaign_id=campaign_id,
                   group_by_weekday=False)
    if not hourly:
        out.by_hour = [Bucket(hour=h, weekday=None) for h in HOURS]
        out.reason = ("No hourly data yet. Hourly metrics come from Amazon Marketing Stream, which "
                      "needs its own subscription — until that's set up I can only see whole days.")
        return out

    found = {int(r.hour): r for r in hourly}
    out.by_hour = [Bucket(hour=h, weekday=None,
                          impressions=int(getattr(found.get(h), "impressions", 0) or 0),
                          clicks=int(getattr(found.get(h), "clicks", 0) or 0),
                          spend=float(getattr(found.get(h), "spend", 0) or 0),
                          sales=float(getattr(found.get(h), "sales", 0) or 0),
                          orders=int(getattr(found.get(h), "orders", 0) or 0),
                          days=int(getattr(found.get(h), "days", 0) or 0))
                   for h in HOURS]

    weekly = _rows(session, profile, start=start, end=end, campaign_id=campaign_id,
                   group_by_weekday=True)
    out.by_hour_of_week = [
        Bucket(hour=int(r.hour), weekday=None if r.weekday is None else int(r.weekday),
               impressions=int(r.impressions or 0), clicks=int(r.clicks or 0),
               spend=float(r.spend or 0), sales=float(r.sales or 0), orders=int(r.orders or 0),
               days=int(r.days or 0))
        for r in weekly]
    out.by_hour_of_week.sort(key=lambda b: (b.weekday if b.weekday is not None else 7, b.hour))
    return out


def judge(shaped: Shape, *, material: float = MATERIAL_GAP) -> list[Verdict]:
    """Pick out the hours that are really different, and say how we know.

    Each hour is tested against *the rest of the day* rather than against the account average, which
    would include the hour itself and so shrink its own difference.
    """
    verdicts: list[Verdict] = []
    if not shaped.has_data:
        return verdicts
    total_clicks = sum(b.clicks for b in shaped.by_hour)
    total_orders = sum(b.orders for b in shaped.by_hour)
    total_spend = shaped.total_spend
    total_sales = shaped.total_sales

    for bucket in shaped.by_hour:
        if not bucket.substantial:
            continue
        rest_clicks = total_clicks - bucket.clicks
        rest_orders = total_orders - bucket.orders
        if rest_clicks <= 0:
            continue
        comparison = statistics.two_proportions(bucket.orders, bucket.clicks,
                                                rest_orders, rest_clicks, metric="cvr")
        # Against the rest of the day, not against the account total — which includes this hour, so
        # a campaign concentrated into a few hours would shrink its own difference and go unreported.
        rest_spend = total_spend - bucket.spend
        rest_sales = total_sales - bucket.sales
        rest_acos = (rest_spend / rest_sales) if rest_sales > 0 else None

        # An hour with spend and no sales is the clearest waste there is, and has no ACOS to compare.
        if bucket.acos is None:
            verdicts.append(Verdict(bucket=bucket, kind="wasteful", comparison=comparison,
                                    rest_of_day_acos=rest_acos, gap=None))
            continue
        if rest_acos is None or rest_acos <= 0:
            continue
        gap = (bucket.acos - rest_acos) / rest_acos
        if gap >= material:
            verdicts.append(Verdict(bucket=bucket, kind="wasteful", comparison=comparison,
                                    rest_of_day_acos=rest_acos, gap=gap))
        elif gap <= -material:
            verdicts.append(Verdict(bucket=bucket, kind="profitable", comparison=comparison,
                                    rest_of_day_acos=rest_acos, gap=gap))
    verdicts.sort(key=lambda v: (not v.proven, -abs(v.gap or 99)))
    return verdicts


def wasteful(verdicts: list[Verdict]) -> list[Verdict]:
    return [v for v in verdicts if v.kind == "wasteful"]


def profitable(verdicts: list[Verdict]) -> list[Verdict]:
    return [v for v in verdicts if v.kind == "profitable"]


def _runs(hours: list[int]) -> list[tuple[int, int]]:
    """Collapse scattered hours into clock ranges, including the one that wraps past midnight.

    "1am-5am" is a thing the owner can act on in Seller Central. "1am, 2am, 3am, 5am" is a list he has to
    turn into that himself.
    """
    if not hours:
        return []
    ordered = sorted(set(hours))
    runs: list[list[int]] = [[ordered[0], ordered[0]]]
    for hour in ordered[1:]:
        if hour == runs[-1][1] + 1:
            runs[-1][1] = hour
        else:
            runs.append([hour, hour])
    # 11pm and midnight are adjacent on a clock even though 23 and 0 are not adjacent as integers.
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][1] == 23:
        runs[0][0] = runs[-1][0]
        runs.pop()
    return [(a, b) for a, b in runs]


def describe_hours(hours: list[int]) -> str:
    clock = metrics.clock_of
    if len(set(hours)) >= 24:
        return "all day"           # a wrapping 12am-12am range reads as a typo, not as everything
    parts = []
    for start, end in _runs(hours):
        parts.append(clock(start) if start == end else f"{clock(start)}–{clock((end + 1) % 24)}")
    return ", ".join(parts)


def summary(shaped: Shape, verdicts: list[Verdict] | None = None) -> str:
    """One paragraph about the day, honest about what it can and cannot support."""
    if not shaped.has_data:
        return shaped.reason or "No hourly data yet."
    verdicts = judge(shaped) if verdicts is None else verdicts
    proven = [v for v in verdicts if v.proven]
    if shaped.coverage == "thin":
        return (f"{shaped.days} days of hourly data so far — enough to see the shape of the day, "
                "not enough to act on it. Ask me again in a couple of weeks.")
    if not proven:
        return (f"Across {shaped.days} days the hours look much alike: no part of the day converts "
                "differently enough to be worth advertising differently.")
    bad = [v.bucket.hour for v in proven if v.kind == "wasteful"]
    good = [v.bucket.hour for v in proven if v.kind == "profitable"]
    parts = []
    if bad:
        waste = sum(v.bucket.spend for v in proven if v.kind == "wasteful") / max(shaped.days, 1) * 30
        parts.append(f"{describe_hours(bad)} is where the money goes to die — about "
                     f"{metrics.money(waste, shaped.currency, places=0)} a month at the current "
                     "rate.")
    if good:
        parts.append(f"{describe_hours(good)} converts well above the rest of the day.")
    return " ".join(parts)


def candidates(session: Session, profile: PpcProfile, *, today: date | None = None,
               shaped: Shape | None = None) -> list[Candidate]:
    """Advisory recommendations: what to change, by hand, and what it is worth.

    Only *proven* hours become candidates. The shape is interesting; the test is what makes it
    actionable, and an unproven hour belongs in the report as an observation, not here as a change.
    """
    shaped = shaped if shaped is not None else shape(session, profile, today=today)
    if not shaped.has_data or shaped.coverage != "good":
        return []
    verdicts = judge(shaped)
    proven = [v for v in verdicts if v.proven]
    out: list[Candidate] = []

    bad = [v for v in proven if v.kind == "wasteful"]
    if bad:
        hours = sorted(v.bucket.hour for v in bad)
        spend = sum(v.bucket.spend for v in bad)
        sales = sum(v.bucket.sales for v in bad)
        # Per-month, from the window actually measured, not from a guessed 30/28 ratio.
        monthly_spend = spend / max(shaped.days, 1) * 30
        monthly_sales = sales / max(shaped.days, 1) * 30
        # What this is worth, and the arithmetic matters because an earlier version got the sign
        # wrong. `monthly_spend - monthly_sales` values the forgone sales at a 100% margin, so it
        # came out *negative* for every wasteful hour whose ACOS was under 100% — which is most of
        # them — on a field `candidates.py` documents as "signed: positive is better". The digest
        # then printed the magnitude of a loss as though it were a saving.
        #
        # The honest figure is what these hours would cost if they performed like the rest of the
        # day: the same sales bought at the rest-of-the-day ACOS. The difference is the overspend,
        # it is positive exactly when the hour is worse than the rest, and it claims nothing about
        # margin, which this module has no per-hour view of.
        rest_acos = next((v.rest_of_day_acos for v in bad if v.rest_of_day_acos), None)
        overspend = (monthly_spend - monthly_sales * rest_acos) if rest_acos else monthly_spend
        overspend = max(round(overspend, 2), 0.0)
        out.append(Candidate(
            analyser="dayparting",
            change_type="dayparting",
            entity_type="campaign",
            entity_id=shaped.campaign_id or profile.amazon_profile_id,
            label="the whole account" if not shaped.campaign_id else shaped.campaign_id,
            reason=(f"Between {describe_hours(hours)} the account spends "
                    f"{metrics.money(monthly_spend, profile.currency, places=0)} a month and brings "
                    f"back {metrics.money(monthly_sales, profile.currency, places=0)}. The rest of "
                    f"the day converts materially better, and the difference holds up to a test, so "
                    f"this isn't noise. Sponsored Products has no hour-of-day setting, so this needs "
                    f"a bid or budget schedule — it's a change for you to make, not one I can send."),
            evidence={"hours": hours, "hour_ranges": describe_hours(hours),
                      "days_measured": shaped.days,
                      "window": [shaped.from_date.isoformat() if shaped.from_date else None,
                                 shaped.to_date.isoformat() if shaped.to_date else None],
                      "account_acos": None if shaped.account_acos is None
                      else round(shaped.account_acos, 4),
                      "monthly_spend": round(monthly_spend, 2),
                      "monthly_sales": round(monthly_sales, 2),
                      "rest_of_day_acos": None if rest_acos is None else round(rest_acos, 4),
                      "overspend_vs_rest_of_day": overspend,
                      "verdicts": [v.as_dict() for v in bad]},
            current_value={"schedule": "flat"},
            proposed_value={"reduce_bids_between": describe_hours(hours)},
            risk="medium",
            confidence=0.7,
            spend_at_risk=round(monthly_spend, 2),
            monthly_effect=overspend,
            notes=["Advisory: approving this records that you want it. The change is made in "
                   "Seller Central, or with a bid schedule I don't run yet."]))

    good = [v for v in proven if v.kind == "profitable"]
    if good:
        hours = sorted(v.bucket.hour for v in good)
        spend = sum(v.bucket.spend for v in good)
        monthly_spend = spend / max(shaped.days, 1) * 30
        out.append(Candidate(
            analyser="dayparting",
            change_type="dayparting",
            entity_type="campaign",
            entity_id=shaped.campaign_id or profile.amazon_profile_id,
            label="the whole account" if not shaped.campaign_id else shaped.campaign_id,
            reason=(f"{describe_hours(hours)} converts well above the rest of the day and only takes "
                    f"{metrics.money(monthly_spend, profile.currency, places=0)} a month. There is "
                    f"room to bid harder in those hours — again, a schedule you'd set rather than "
                    f"something I can send."),
            evidence={"hours": hours, "hour_ranges": describe_hours(hours),
                      "days_measured": shaped.days,
                      "monthly_spend": round(monthly_spend, 2),
                      "verdicts": [v.as_dict() for v in good]},
            current_value={"schedule": "flat"},
            proposed_value={"raise_bids_between": describe_hours(hours)},
            risk="medium",
            confidence=0.6,
            spend_at_risk=round(monthly_spend, 2),
            monthly_effect=0.0,
            notes=["Advisory, and the upside is a guess: buying more of a good hour usually costs "
                   "more per click than the hour currently costs."]))
    return out


def worst_campaigns(session: Session, profile: PpcProfile, *, today: date | None = None,
                    limit: int = 5) -> list[tuple[str, Shape]]:
    """The campaigns whose days are most lopsided — where a schedule would be worth the most."""
    today = today or metrics.today_for(profile)
    end = today - timedelta(days=metrics.PROVISIONAL_DAYS)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    ids = session.scalars(
        select(PpcHourlyMetric.amazon_campaign_id)
        .where(PpcHourlyMetric.amazon_profile_id == profile.amazon_profile_id,
               PpcHourlyMetric.ad_product == AD_PRODUCT,
               PpcHourlyMetric.date >= start, PpcHourlyMetric.date <= end, _HAS_TRAFFIC)
        .group_by(PpcHourlyMetric.amazon_campaign_id)
        .order_by(func.sum(PpcHourlyMetric.spend).desc()).limit(limit * 4)).all()
    names = {c.amazon_campaign_id: c.name or c.amazon_campaign_id for c in session.scalars(
        select(PpcCampaign).where(PpcCampaign.profile_id == profile.id,
                                  PpcCampaign.ad_product == AD_PRODUCT)).all()}
    scored: list[tuple[float, str, Shape]] = []
    for campaign_id in ids:
        shaped = shape(session, profile, today=today, campaign_id=campaign_id)
        proven = [v for v in judge(shaped) if v.proven and v.kind == "wasteful"]
        if not proven:
            continue
        scored.append((sum(v.bucket.spend for v in proven), names.get(campaign_id, campaign_id),
                       shaped))
    scored.sort(key=lambda row: -row[0])
    return [(name, shaped) for _, name, shaped in scored[:limit]]
