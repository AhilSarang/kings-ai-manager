"""Did the change help? Measured at 7, 14 and 28 days, against a baseline, with the context.

This is the module that decides whether the agent is any good, so its bias is deliberately toward
saying "I don't know". Three things make that necessary:

**A before-and-after is not a measurement.** If the whole account fell 20% that fortnight, a target
that fell 20% did not get worse — it did exactly what everything else did. So every effect is
*baseline-adjusted*: the change in the entity minus the change in the rest of the account over the
same days. That is a difference of differences, and it is the smallest honest unit here.

**Context contaminates.** A bid cut on a product that went out of stock mid-window tells you nothing
about the bid. Stockouts, price changes, suppression, Buy Box loss and calendar events are looked up
for the post window, and a contaminated outcome is `inconclusive` however good the numbers look.

**Small numbers are not evidence.** Four clicks and one order cannot distinguish a 20% improvement
from luck, so below a minimum sample the verdict is `inconclusive` and says which number was short.

Amazon also keeps restating conversions for about five weeks (D101), so an outcome is evaluated when
its window closes on finalised data and **re-evaluated until it settles**. The early read is useful;
only the settled one is allowed to teach the playbook anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (AmazonListing, AmazonListingVersion, AmazonSalesDaily, PpcAction,
                           PpcActionOutcome,
                           PpcCalendarEvent, PpcMetricsDaily, PpcPrediction, PpcProfile,
                           PpcRecommendation)
from app.ppc import metrics
from app.services.common import utcnow

log = logging.getLogger(__name__)

WINDOWS = (7, 14, 28)
# Below this, the window cannot tell an improvement from luck.
MIN_CLICKS = 20
MIN_ORDERS_FOR_SALES_VERDICT = 2
# An adjusted effect smaller than this is noise, not a result.
NOISE = 0.10
# Stock out on this share of the post window and the window is about stock, not about the bid.
STOCKOUT_SHARE = 0.2


@dataclass
class Effect:
    """One metric, before and after, with the baseline taken out."""

    metric: str
    before: float
    after: float
    baseline_before: float
    baseline_after: float

    @property
    def change(self) -> float | None:
        return round((self.after - self.before) / self.before, 4) if self.before else None

    @property
    def baseline_change(self) -> float | None:
        if not self.baseline_before:
            return None
        return round((self.baseline_after - self.baseline_before) / self.baseline_before, 4)

    @property
    def adjusted(self) -> float | None:
        """The entity's change less whatever the rest of the account did over the same days."""
        mine, theirs = self.change, self.baseline_change
        if mine is None:
            return None
        return round(mine - (theirs or 0.0), 4)

    def as_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "before": round(self.before, 2), "after": round(self.after, 2),
                "change": self.change, "baseline_change": self.baseline_change,
                "adjusted": self.adjusted}


@dataclass
class Window:
    """One entity's totals over a stretch of days."""

    clicks: int = 0
    spend: float = 0.0
    sales: float = 0.0
    orders: int = 0
    impressions: int = 0
    days: int = 0

    @property
    def acos(self) -> float | None:
        return round(self.spend / self.sales, 4) if self.sales else None

    @property
    def cvr(self) -> float | None:
        return round(self.orders / self.clicks, 4) if self.clicks else None

    @property
    def cpc(self) -> float | None:
        return round(self.spend / self.clicks, 4) if self.clicks else None

    def as_dict(self) -> dict[str, Any]:
        return {"clicks": self.clicks, "orders": self.orders, "spend": round(self.spend, 2),
                "sales": round(self.sales, 2), "impressions": self.impressions,
                "acos": self.acos, "cvr": self.cvr, "cpc": self.cpc, "days": self.days}


def _totals(session: Session, profile: PpcProfile, *, entity_type: str, entity_id: str,
            start: date, end: date) -> Window:
    row = session.execute(select(
        func.coalesce(func.sum(PpcMetricsDaily.clicks), 0),
        func.coalesce(func.sum(PpcMetricsDaily.spend), 0),
        func.coalesce(func.sum(PpcMetricsDaily.sales), 0),
        func.coalesce(func.sum(PpcMetricsDaily.orders), 0),
        func.coalesce(func.sum(PpcMetricsDaily.impressions), 0),
        func.count(PpcMetricsDaily.date)).where(
        PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
        PpcMetricsDaily.entity_type == entity_type,
        PpcMetricsDaily.amazon_entity_id == entity_id,
        PpcMetricsDaily.date >= start, PpcMetricsDaily.date <= end,
        PpcMetricsDaily.is_provisional.is_(False))).one()
    return Window(clicks=int(row[0] or 0), spend=float(row[1] or 0), sales=float(row[2] or 0),
                  orders=int(row[3] or 0), impressions=int(row[4] or 0), days=int(row[5] or 0))


def _baseline(session: Session, profile: PpcProfile, *, entity_type: str, exclude: str,
              start: date, end: date) -> Window:
    """Everything of the same kind *except* the entity that changed.

    Excluding it matters: leaving it in means the thing being measured is part of its own control, so
    a big effect partly cancels itself and a small account barely moves at all.
    """
    row = session.execute(select(
        func.coalesce(func.sum(PpcMetricsDaily.clicks), 0),
        func.coalesce(func.sum(PpcMetricsDaily.spend), 0),
        func.coalesce(func.sum(PpcMetricsDaily.sales), 0),
        func.coalesce(func.sum(PpcMetricsDaily.orders), 0),
        func.coalesce(func.sum(PpcMetricsDaily.impressions), 0),
        func.count(func.distinct(PpcMetricsDaily.date))).where(
        PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
        PpcMetricsDaily.entity_type == entity_type,
        PpcMetricsDaily.amazon_entity_id != exclude,
        PpcMetricsDaily.date >= start, PpcMetricsDaily.date <= end,
        PpcMetricsDaily.is_provisional.is_(False))).one()
    return Window(clicks=int(row[0] or 0), spend=float(row[1] or 0), sales=float(row[2] or 0),
                  orders=int(row[3] or 0), impressions=int(row[4] or 0), days=int(row[5] or 0))


# ---------------- what else was going on ----------------
def external_factors(session: Session, profile: PpcProfile, action: PpcAction, *,
                     start: date, end: date) -> dict[str, Any]:
    """Everything that could explain the numbers other than the change itself."""
    found: dict[str, Any] = {}
    asins = [action.asin] if action.asin else []
    if asins:
        days = (end - start).days + 1
        rows = session.execute(select(AmazonSalesDaily).where(
            AmazonSalesDaily.marketplace_id == profile.marketplace_id,
            AmazonSalesDaily.asin.in_(asins),
            AmazonSalesDaily.date >= start, AmazonSalesDaily.date <= end)).scalars().all()
        selling_days = {r.date for r in rows if (r.units or 0) > 0}
        quiet = days - len(selling_days)
        if quiet > 0:
            found["days_with_no_units"] = quiet
            if quiet / days >= STOCKOUT_SHARE:
                found["possible_stockout"] = True
        boxes = [float(r.buy_box_pct) for r in rows if r.buy_box_pct is not None]
        if boxes and min(boxes) < 70:
            found["buy_box_low"] = {"lowest": round(min(boxes), 1),
                                    "average": round(sum(boxes) / len(boxes), 1)}

        # A listing edited mid-window — a price change especially — moves conversion on its own.
        edits = session.execute(select(func.count()).select_from(AmazonListingVersion).join(
            AmazonListing, AmazonListingVersion.listing_id == AmazonListing.id).where(
            AmazonListing.asin.in_(asins),
            AmazonListing.marketplace_id == profile.marketplace_id,
            AmazonListingVersion.created_at >= _as_moment(start),
            AmazonListingVersion.created_at < _as_moment(end + timedelta(days=1)))).scalar_one()
        if edits:
            found["listing_changed"] = int(edits)
        prices = sorted({float(r.ordered_product_sales) / r.units
                         for r in rows if r.units and r.ordered_product_sales})
        if len(prices) > 1 and prices[-1] - prices[0] >= 0.01:
            # The average selling price moved inside the window, which changes ACOS by itself.
            found["price_moved"] = {"low": round(prices[0], 2), "high": round(prices[-1], 2)}

    events = session.execute(select(PpcCalendarEvent).where(
        PpcCalendarEvent.start_date <= end, PpcCalendarEvent.end_date >= start)).scalars().all()
    if events:
        found["calendar"] = [{"kind": e.kind, "name": e.name,
                              "from": e.start_date.isoformat(), "to": e.end_date.isoformat()}
                             for e in events]
    other = session.execute(select(func.count()).select_from(PpcAction).where(
        PpcAction.profile_id == profile.id,
        PpcAction.amazon_entity_id == action.amazon_entity_id,
        PpcAction.id != action.id,
        PpcAction.executed_at > action.executed_at,
        PpcAction.executed_at <= _as_moment(end + timedelta(days=1)))).scalar_one()
    if other:
        # Another change landed on the same entity inside the window, so neither can be measured.
        found["other_changes_to_the_same_entity"] = int(other)
    return found


def _as_moment(day: date):
    from datetime import datetime, time, timezone

    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def contaminated_by(factors: dict[str, Any]) -> str | None:
    """Which factor, if any, makes this window unreadable. The wording ends up in the report."""
    if factors.get("possible_stockout"):
        return (f"the product had no sales on {factors['days_with_no_units']} days of the window, "
                "so this is about stock rather than the change")
    if factors.get("other_changes_to_the_same_entity"):
        return (f"{factors['other_changes_to_the_same_entity']} other change(s) landed on the same "
                "thing inside the window, so neither can be told apart")
    if factors.get("buy_box_low"):
        return (f"the Buy Box dropped to {factors['buy_box_low']['lowest']}% during the window, "
                "which moves traffic regardless of the bid")
    if factors.get("calendar"):
        names = ", ".join(e["name"] for e in factors["calendar"][:2])
        return f"the window overlaps {names}, when nothing compares to a normal week"
    if factors.get("price_moved"):
        moved = factors["price_moved"]
        return (f"the selling price moved between {moved['low']:.2f} and {moved['high']:.2f} during "
                "the window, which changes ACOS on its own")
    return None


# ---------------- the verdict ----------------
@dataclass
class Assessment:
    verdict: str
    headline: str
    effects: list[Effect] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "headline": self.headline,
                "effects": [e.as_dict() for e in self.effects], "reasons": list(self.reasons)}


def _intent(change_type: str) -> tuple[str, str]:
    """What the change was *for*, so it can be judged on that and not on everything at once."""
    if change_type in ("bid_decrease", "budget_decrease", "pause_target", "add_negative_exact",
                       "add_negative_phrase"):
        return "spend", "down"
    if change_type in ("bid_increase", "budget_increase", "enable_target", "harvest_search_term"):
        return "sales", "up"
    if change_type == "placement_adjustment":
        return "acos", "down"
    return "sales", "up"


def assess(before: Window, after: Window, baseline_before: Window, baseline_after: Window, *,
           change_type: str, factors: dict[str, Any]) -> Assessment:
    """Judge one window. Honest about what it cannot tell."""
    metric, wanted = _intent(change_type)
    effects = [
        Effect("spend", before.spend, after.spend, baseline_before.spend, baseline_after.spend),
        Effect("sales", before.sales, after.sales, baseline_before.sales, baseline_after.sales),
        Effect("clicks", float(before.clicks), float(after.clicks),
               float(baseline_before.clicks), float(baseline_after.clicks)),
        Effect("orders", float(before.orders), float(after.orders),
               float(baseline_before.orders), float(baseline_after.orders)),
    ]
    reasons = []

    spoiled = contaminated_by(factors)
    if spoiled:
        return Assessment("inconclusive", f"Can't tell: {spoiled}.", effects, [spoiled])

    if before.clicks < MIN_CLICKS or after.clicks < MIN_CLICKS:
        short = "before" if before.clicks < MIN_CLICKS else "after"
        reasons.append(f"only {getattr(before if short == 'before' else after, 'clicks')} clicks "
                       f"{short} the change, and {MIN_CLICKS} is the least that could show anything")
        return Assessment("inconclusive", "Not enough traffic to tell.", effects, reasons)

    chosen = next(e for e in effects if e.metric == metric)
    if metric == "sales" and max(before.orders, after.orders) < MIN_ORDERS_FOR_SALES_VERDICT:
        reasons.append(f"{max(before.orders, after.orders)} orders either side — too few to judge a "
                       "change in sales")
        return Assessment("inconclusive", "Too few orders to tell.", effects, reasons)

    adjusted = chosen.adjusted if metric != "acos" else _acos_effect(before, after)
    if adjusted is None:
        return Assessment("inconclusive", f"No {metric} to compare.", effects,
                          [f"there was no {metric} before the change, so there is no change to measure"])

    improved = (adjusted < 0) if wanted == "down" else (adjusted > 0)
    size = abs(adjusted)
    if size < NOISE:
        return Assessment("neutral", f"{metric.title()} barely moved ({adjusted:+.0%} after taking "
                                     "the rest of the account out).", effects, reasons)
    direction = "down" if adjusted < 0 else "up"
    headline = (f"{metric.title()} went {direction} {size:.0%} more than the rest of the account "
                f"did — which is {'what it was for' if improved else 'the opposite of the intent'}.")
    return Assessment("positive" if improved else "negative", headline, effects, reasons)


def _acos_effect(before: Window, after: Window) -> float | None:
    """ACOS is a ratio, so its change is in points of itself, not a ratio of ratios."""
    if before.acos is None or after.acos is None:
        return None
    return round((after.acos - before.acos) / before.acos, 4)


# ---------------- evaluating an action ----------------
def windows_for(action: PpcAction, window_days: int) -> tuple[date, date, date, date]:
    """The equal stretches either side of the change. The day of the change belongs to neither."""
    changed = action.executed_at.date()
    post_start = changed + timedelta(days=1)
    post_end = post_start + timedelta(days=window_days - 1)
    pre_end = changed - timedelta(days=1)
    pre_start = pre_end - timedelta(days=window_days - 1)
    return pre_start, pre_end, post_start, post_end


def evaluate(session: Session, profile: PpcProfile, action: PpcAction, window_days: int, *,
             today: date | None = None) -> PpcActionOutcome | None:
    """Measure one action over one window. Returns None while the window is still open."""
    today = today or metrics.today_for(profile)
    pre_start, pre_end, post_start, post_end = windows_for(action, window_days)
    # The window has to be over *and* out of the provisional zone before it is worth reading.
    if post_end > today - timedelta(days=metrics.PROVISIONAL_DAYS):
        return None

    entity_type = action.entity_type
    entity_id = action.amazon_entity_id
    before = _totals(session, profile, entity_type=entity_type, entity_id=entity_id,
                     start=pre_start, end=pre_end)
    after = _totals(session, profile, entity_type=entity_type, entity_id=entity_id,
                    start=post_start, end=post_end)
    baseline_before = _baseline(session, profile, entity_type=entity_type, exclude=entity_id,
                                start=pre_start, end=pre_end)
    baseline_after = _baseline(session, profile, entity_type=entity_type, exclude=entity_id,
                               start=post_start, end=post_end)
    factors = external_factors(session, profile, action, start=post_start, end=post_end)
    assessment = assess(before, after, baseline_before, baseline_after,
                        change_type=action.change_type, factors=factors)

    settled = metrics.is_settled(post_end, today)
    row = session.execute(select(PpcActionOutcome).where(
        PpcActionOutcome.action_id == action.id,
        PpcActionOutcome.window_days == window_days)).scalars().first()
    if row is None:
        row = PpcActionOutcome(action_id=action.id, ad_product=action.ad_product,
                               window_days=window_days)
        session.add(row)
    row.pre_metrics = {**before.as_dict(), "from": pre_start.isoformat(), "to": pre_end.isoformat(),
                       "baseline": baseline_before.as_dict()}
    row.post_metrics = {**after.as_dict(), "from": post_start.isoformat(),
                        "to": post_end.isoformat(), "baseline": baseline_after.as_dict()}
    row.baseline_adjusted_effect = {**assessment.as_dict(), "settled": settled,
                                    "settles_after": metrics.settled_before(today).isoformat()}
    row.external_factors = factors
    row.contaminated = contaminated_by(factors) is not None
    row.verdict = assessment.verdict
    row.evaluated_at = utcnow()
    session.flush()
    log.info("ppc outcome measured", extra={"action": str(action.id), "window": window_days,
                                            "verdict": row.verdict, "settled": settled})
    if settled:
        score_prediction(session, action, row)
    return row


def needs_evaluating(session: Session, profile: PpcProfile, *,
                     today: date | None = None) -> list[tuple[PpcAction, int]]:
    """Every (action, window) pair that is ready to be measured, or re-measured until it settles."""
    today = today or metrics.today_for(profile)
    out: list[tuple[PpcAction, int]] = []
    actions = session.execute(select(PpcAction).where(
        PpcAction.profile_id == profile.id,
        PpcAction.executed_at.is_not(None),
        PpcAction.rolled_back_at.is_(None))).scalars().all()
    existing = {(o.action_id, o.window_days): o for o in session.execute(
        select(PpcActionOutcome)).scalars()}
    for action in actions:
        for window_days in WINDOWS:
            _pre_start, _pre_end, _post_start, post_end = windows_for(action, window_days)
            if post_end > today - timedelta(days=metrics.PROVISIONAL_DAYS):
                continue
            row = existing.get((action.id, window_days))
            if row is None:
                out.append((action, window_days))
            elif not (row.baseline_adjusted_effect or {}).get("settled"):
                # Measured early, and Amazon is still restating it. Read it again.
                out.append((action, window_days))
    return out


def evaluate_due(session: Session, profile: PpcProfile, *, today: date | None = None,
                 limit: int = 200) -> dict[str, Any]:
    """Measure everything that is ready. Run daily."""
    due = needs_evaluating(session, profile, today=today)[:limit]
    verdicts: dict[str, int] = {}
    for action, window_days in due:
        row = evaluate(session, profile, action, window_days, today=today)
        if row is not None:
            verdicts[row.verdict] = verdicts.get(row.verdict, 0) + 1
    session.flush()
    return {"measured": len(due), "verdicts": verdicts}


# ---------------- calibration ----------------
def score_prediction(session: Session, action: PpcAction, outcome: PpcActionOutcome) -> PpcPrediction | None:
    """Compare what the agent said would happen with what did (brief 7.2, Section 11).

    Only settled outcomes are scored. A calibration built on numbers Amazon is still revising would
    measure the revision, not the prediction.
    """
    if not action.recommendation_id:
        return None
    recommendation = session.get(PpcRecommendation, action.recommendation_id)
    if recommendation is None or not recommendation.predicted_effect:
        return None
    predicted = recommendation.predicted_effect
    estimate = predicted.get("monthly_effect_estimate")
    if estimate is None:
        return None

    effect = next((e for e in (outcome.baseline_adjusted_effect or {}).get("effects") or []
                   if e.get("metric") == _intent(action.change_type)[0]), None)
    if effect is None:
        return None
    # The predicted figure is a month of effect; the measured one is this window's, scaled to a month
    # so the two are the same unit.
    scale = 30.0 / max(outcome.window_days, 1)
    actual_monthly = round((effect.get("after", 0) - effect.get("before", 0)) * scale, 2)
    if _intent(action.change_type)[1] == "down":
        actual_monthly = -actual_monthly       # a saving is a positive outcome

    low = predicted.get("monthly_effect_low")
    high = predicted.get("monthly_effect_high")
    row = session.execute(select(PpcPrediction).where(
        PpcPrediction.subject_type == "recommendation",
        PpcPrediction.subject_id == recommendation.id)).scalars().first()
    if row is None:
        row = PpcPrediction(subject_type="recommendation", subject_id=recommendation.id,
                            ad_product=recommendation.ad_product)
        session.add(row)
    row.change_type = action.change_type
    row.metric = _intent(action.change_type)[0]
    row.predicted = predicted
    row.actual = {"monthly_effect": actual_monthly, "window_days": outcome.window_days,
                  "verdict": outcome.verdict}
    row.error = round(actual_monthly - float(estimate), 4)
    row.absolute_error = abs(row.error)
    row.within_interval = (None if low is None or high is None
                           else bool(min(low, high) <= actual_monthly <= max(low, high)))
    row.stated_confidence = recommendation.confidence
    row.model_note = outcome.baseline_adjusted_effect.get("headline")
    row.evaluated_at = utcnow()
    session.flush()
    return row


def calibration(session: Session, *, change_type: str | None = None,
                since: date | None = None) -> dict[str, Any]:
    """Predicted against actual, by change type — the foundation of Section 11's report.

    `inside_range` is the number that matters. A well-calibrated agent lands inside its own stated
    range about as often as its confidence says it should; one that is always right about direction
    and always wrong about size is still badly calibrated, and that shows up here rather than in a
    story about how well it is doing.
    """
    q = select(PpcPrediction).where(PpcPrediction.evaluated_at.is_not(None))
    if change_type:
        q = q.where(PpcPrediction.change_type == change_type)
    if since:
        q = q.where(PpcPrediction.evaluated_at >= _as_moment(since))
    rows = list(session.execute(q).scalars())
    by_type: dict[str, list[PpcPrediction]] = {}
    for row in rows:
        by_type.setdefault(row.change_type or "other", []).append(row)

    out: dict[str, Any] = {"scored": len(rows), "by_change_type": {}}
    for kind, group in sorted(by_type.items()):
        inside = [r for r in group if r.within_interval is True]
        judged = [r for r in group if r.within_interval is not None]
        errors = [float(r.absolute_error) for r in group if r.absolute_error is not None]
        directions = [r for r in group
                      if r.predicted and r.actual
                      and (float(r.predicted.get("monthly_effect_estimate") or 0) >= 0)
                      == (float(r.actual.get("monthly_effect") or 0) >= 0)]
        out["by_change_type"][kind] = {
            "scored": len(group),
            "inside_range": len(inside),
            "inside_range_pct": round(100 * len(inside) / len(judged), 1) if judged else None,
            "right_direction_pct": round(100 * len(directions) / len(group), 1) if group else None,
            "mean_absolute_error": round(sum(errors) / len(errors), 2) if errors else None,
            "stated_confidence": round(
                sum(float(r.stated_confidence or 0) for r in group) / len(group), 2) if group else None,
        }
    out["enough_to_judge"] = len(rows) >= 50      # Section 11 asks for at least fifty
    return out


def verdict_counts(session: Session, profile: PpcProfile, *,
                   settled_only: bool = True) -> dict[str, Any]:
    """How the agent's changes have actually turned out, by change type."""
    rows = session.execute(select(PpcActionOutcome, PpcAction).join(
        PpcAction, PpcActionOutcome.action_id == PpcAction.id).where(
        PpcAction.profile_id == profile.id)).all()
    out: dict[str, dict[str, int]] = {}
    for outcome, action in rows:
        if settled_only and not (outcome.baseline_adjusted_effect or {}).get("settled"):
            continue
        if outcome.window_days != 14:          # one window per action, so nothing is counted thrice
            continue
        bucket = out.setdefault(action.change_type, {})
        bucket[outcome.verdict] = bucket.get(outcome.verdict, 0) + 1
    return out
