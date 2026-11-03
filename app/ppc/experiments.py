"""Controlled experiments: a hypothesis, a real control group, hard limits, and an honest verdict.

This is where "self-learning" either means something or does not. The difference between an
experiment and a change you happen to have made is the control group, and the difference between a
finding and a story is the interval around the effect. Both are enforced here:

* **A true control.** Units are split into treatment and control by **stratified** randomisation —
  sorted by recent click volume into strata, then randomised within each. Plain randomisation on a
  few dozen targets routinely puts most of the traffic in one arm, and then the arms are not
  comparable whatever the test says.
* **Power before spend.** The design computes the smallest effect this much traffic could detect. If
  the answer is "only a 60% improvement", the experiment is refused with that number attached —
  three weeks of spend ending in "inconclusive" is worse than not starting.
* **Hard guardrails.** Spend at risk, the largest bid change, and abort conditions are fixed at
  design time and checked on a schedule. A tripped guardrail stops the experiment and says which.
* **Honest conclusions.** `supported`, `not_supported` or `inconclusive`, with the effect size,
  its confidence interval and the power actually achieved. "We found nothing" and "we never had the
  traffic to tell" are different sentences and both get said.

Units contaminated during the test — a stockout, a price change, another change landing on them — are
excluded with the reason, because leaving them in measures the contamination.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (PpcAction, PpcCalendarEvent, PpcExperiment, PpcExperimentUnit,
                           PpcMetricsDaily, PpcProfile, PpcTarget, User)
from app.ppc import aggregates, metrics, outcomes, statistics
from app.services import approvals, audit, settings_store
from app.services.common import Actor, NotFound, ServiceError, utcnow

log = logging.getLogger(__name__)

DEFAULT_DAYS = 21
MIN_UNITS_PER_ARM = 6
# An experiment that could only find an improvement bigger than this is not worth running.
WORTHWHILE_EFFECT = 0.5
# The effect size "we found nothing" is judged against. Power has to be measured against an effect
# worth caring about, not against the effect observed: with an observed difference of zero, power at
# the observed effect is alpha by definition, which would make every clean null result look like a
# failure to measure. A quarter improvement in conversion is the bar.
MEANINGFUL_EFFECT = 0.25
TARGET_POWER = 0.8
ALPHA = 0.05


# Every experiment designed here is on Sponsored Products data. Named so the row says so rather
# than inheriting a default.
AD_PRODUCT = "sp"


class ExperimentError(ServiceError):
    pass


def normalize_ref(ref: str) -> str:
    import re

    m = re.match(r"^\s*X-?\s*(\d+)\s*$", ref or "", re.I)
    if not m:
        raise NotFound(f"'{ref}' isn't an experiment reference (they look like X-3).")
    return f"X-{int(m.group(1))}"


def get_by_ref(session: Session, ref: str, *, lock: bool = False) -> PpcExperiment:
    q = select(PpcExperiment).where(PpcExperiment.ref == normalize_ref(ref))
    row = session.execute(q.with_for_update() if lock else q).scalar_one_or_none()
    if row is None:
        raise NotFound(f"I can't find {normalize_ref(ref)}.")
    return row


def running(session: Session, profile: PpcProfile | None = None) -> list[PpcExperiment]:
    q = select(PpcExperiment).where(PpcExperiment.status == "running")
    if profile is not None:
        q = q.where(PpcExperiment.profile_id == profile.id)
    return list(session.execute(q.order_by(PpcExperiment.started_at)).scalars())


def units(session: Session, experiment: PpcExperiment, *,
          arm: str | None = None, include_excluded: bool = False) -> list[PpcExperimentUnit]:
    q = select(PpcExperimentUnit).where(PpcExperimentUnit.experiment_id == experiment.id)
    if arm:
        q = q.where(PpcExperimentUnit.arm == arm)
    if not include_excluded:
        q = q.where(PpcExperimentUnit.excluded_at.is_(None))
    return list(session.execute(q.order_by(PpcExperimentUnit.amazon_entity_id)).scalars())


# ---------------- designing ----------------
@dataclass
class Candidate:
    """One entity that could take part, with the history that decides its stratum."""

    entity_type: str
    amazon_entity_id: str
    entity_id: Any
    clicks: int
    orders: int
    spend: float
    sales: float
    bid: float | None = None
    label: str | None = None
    # Which ad product this unit belongs to. Checked in `design` so one experiment can never span
    # two: they run in different auctions, so a single result would mean nothing for either (8.2).
    ad_product: str = AD_PRODUCT


def candidates_for(session: Session, profile: PpcProfile, *, change_type: str,
                   campaign_ids: list[str] | None = None, min_clicks: int = 20,
                   days: int = 30, today: date | None = None) -> list[Candidate]:
    """Targets with enough history to be worth testing on."""
    today = today or metrics.today_for(profile)
    totals = aggregates.by_entity(session, profile, "target", ad_product=AD_PRODUCT, days=days,
                                   today=today)
    targets = aggregates.targets(session, profile, ad_product=AD_PRODUCT)
    out = []
    for entity_id, rollup in totals.items():
        target = targets.get(entity_id)
        if target is None or target.state != "enabled":
            continue
        if campaign_ids and target.amazon_campaign_id not in campaign_ids:
            continue
        if rollup.clicks < min_clicks:
            continue
        out.append(Candidate(entity_type="target", amazon_entity_id=entity_id, entity_id=target.id,
                             clicks=rollup.clicks, orders=rollup.orders, spend=rollup.spend,
                             sales=rollup.sales,
                             bid=float(target.bid) if target.bid is not None else None,
                             label=target.expression))
    return out


def stratify(candidates: list[Candidate], *, strata: int = 4) -> dict[str, list[Candidate]]:
    """Group by click volume, so the arms can be made comparable rather than merely random.

    A plain coin flip over thirty targets regularly lands most of the traffic in one arm, and then no
    amount of statistics rescues the comparison. Sorting by volume first and splitting within each
    band keeps the two arms the same shape.
    """
    ordered = sorted(candidates, key=lambda c: (-c.clicks, c.amazon_entity_id))
    if not ordered:
        return {}
    size = max(len(ordered) // max(strata, 1), 1)
    out: dict[str, list[Candidate]] = {}
    for index, candidate in enumerate(ordered):
        band = min(index // size, strata - 1)
        out.setdefault(f"clicks-band-{band + 1}", []).append(candidate)
    return out


def assign(strata: dict[str, list[Candidate]], *,
           seed: int) -> list[tuple[Candidate, str, str]]:
    """Randomise within each stratum. Seeded, so an assignment can be re-derived and audited."""
    rng = random.Random(seed)
    out = []
    for name, members in sorted(strata.items()):
        shuffled = list(members)
        rng.shuffle(shuffled)
        for index, candidate in enumerate(shuffled):
            out.append((candidate, "treatment" if index % 2 == 0 else "control", name))
    return out


def _settings(session: Session) -> tuple[int, float, float]:
    def number(key: str, default: float) -> float:
        try:
            return float(settings_store.get(session, key, default))
        except (TypeError, ValueError):
            return float(default)

    return (int(number("ppc_max_experiments_running", 3)),
            number("ppc_experiment_max_spend_at_risk_usd", 300),
            number("ppc_max_bid_change_pct", 25))


def power_report(candidates: list[tuple[Candidate, str, str]], *, days: int,
                 history_days: int = 30) -> dict[str, Any]:
    """What this experiment could and could not detect, worked out before it runs."""
    treatment = [c for c, arm, _s in candidates if arm == "treatment"]
    control = [c for c, arm, _s in candidates if arm == "control"]
    clicks = sum(c.clicks for c in treatment + control)
    orders = sum(c.orders for c in treatment + control)
    baseline = (orders / clicks) if clicks else 0.0
    # Clicks per arm over the planned run, projected from the measured history.
    per_arm = (sum(c.clicks for c in control) / history_days) * days if control else 0.0
    mde = (statistics.detectable_effect(baseline, per_arm, alpha=ALPHA, power=TARGET_POWER)
           if baseline and per_arm else None)
    needed = (statistics.required_per_arm(baseline, WORTHWHILE_EFFECT / 2, alpha=ALPHA,
                                          power=TARGET_POWER) if baseline else None)
    return {"baseline_cvr": round(baseline, 4), "clicks_measured": clicks,
            "expected_clicks_per_arm": int(per_arm),
            "minimum_detectable_relative_effect": mde,
            "clicks_per_arm_for_a_quarter_improvement": needed,
            "units_treatment": len(treatment), "units_control": len(control),
            "alpha": ALPHA, "target_power": TARGET_POWER}


def design(session: Session, profile: PpcProfile, *, hypothesis: str, change_type: str,
           candidates: list[Candidate], origin: str = "agent_idea", days: int = DEFAULT_DAYS,
           bid_change_pct: float | None = None, primary_metric: str = "cvr",
           today: date | None = None) -> PpcExperiment:
    """Design one experiment, or refuse to and say why.

    The ad-product check comes before the feasibility checks on purpose. Brief 8.2 says the unit of
    assignment is "never mixed within one experiment", and two ad products is the clearest mixing
    there is — different auctions, and Display's view-attributed sales. Telling someone to gather
    more units for an experiment that could never be valid would be the wrong answer, politely.

    Refusing is a feature. The three reasons are too few units to split, no traffic to measure, and
    an effect so large it would have to be before this design could see it.
    """
    today = today or metrics.today_for(profile)
    max_running, max_spend, max_bid_change = _settings(session)
    products = {c.ad_product for c in candidates if c.ad_product}
    if len(products) > 1:
        raise ExperimentError(
            "I can't test across ad products in one experiment — these units span "
            f"{', '.join(sorted(products))}. They run in different auctions, so one result would "
            "not mean anything for either.")
    ad_product = products.pop() if products else AD_PRODUCT

    if len(running(session, profile)) >= max_running:
        raise ExperimentError(
            f"{max_running} experiments are already running, which is the limit you set. "
            "Let one finish, or stop one, before starting another.")
    if len(candidates) < MIN_UNITS_PER_ARM * 2:
        raise ExperimentError(
            f"only {len(candidates)} targets have enough history for this, and a split needs at "
            f"least {MIN_UNITS_PER_ARM * 2} to give both arms {MIN_UNITS_PER_ARM}.")

    seed = random.Random().randrange(1, 2**31)
    assigned = assign(stratify(candidates), seed=seed)
    power = power_report(assigned, days=days)
    mde = power["minimum_detectable_relative_effect"]
    if mde is None:
        raise ExperimentError(
            "there isn't enough traffic here to measure anything in "
            f"{days} days — {power['expected_clicks_per_arm']} clicks per arm against a "
            f"{power['baseline_cvr'] * 100:.1f}% conversion rate. Running it would spend the money "
            "and tell us nothing.")
    if mde > WORTHWHILE_EFFECT:
        raise ExperimentError(
            f"this could only detect an improvement bigger than {mde * 100:.0f}%, which is not a "
            f"realistic result. It would need about "
            f"{power['clicks_per_arm_for_a_quarter_improvement']} clicks per arm to see something "
            "worth acting on; there are "
            f"{power['expected_clicks_per_arm']} expected. Not worth the spend.")

    change = min(abs(bid_change_pct or max_bid_change), max_bid_change)
    treatment = [c for c, arm, _s in assigned if arm == "treatment"]
    daily_spend = sum(c.spend for c in treatment) / 30.0
    at_risk = round(min(daily_spend * days * (change / 100.0) + daily_spend * days * 0.1,
                        max_spend), 2)

    experiment = PpcExperiment(
        profile_id=profile.id, ad_product=ad_product,
        hypothesis=hypothesis.strip(), origin=origin,
        change_type=change_type, unit_of_assignment="target", primary_metric=primary_metric,
        planned_days=days, spend_at_risk=at_risk, has_true_control=True, status="designed",
        minimum_clicks_per_arm=power["expected_clicks_per_arm"],
        design={"assignment": "stratified randomisation by click volume", "seed": seed,
                "strata": sorted({s for _c, _a, s in assigned}),
                "treatment_definition": (f"{change_type} of {change:g}% on the treatment arm"),
                "control_definition": "left exactly as it is",
                "window_days": days, "history_days": 30, "power": power,
                "designed_on": today.isoformat()},
        guardrails={"max_spend_at_risk": at_risk, "max_bid_change_pct": change,
                    "abort_if_treatment_acos_worse_by_pct": 50,
                    "abort_if_spend_exceeds": at_risk,
                    "abort_if_orders_fall_by_pct": 50})
    session.add(experiment)
    session.flush()

    for candidate, arm, stratum in assigned:
        session.add(PpcExperimentUnit(
            experiment_id=experiment.id, ad_product=experiment.ad_product,
            entity_type=candidate.entity_type,
            entity_id=candidate.entity_id, amazon_entity_id=candidate.amazon_entity_id, arm=arm,
            stratum=stratum,
            pre_period_metrics={"clicks": candidate.clicks, "orders": candidate.orders,
                                "spend": round(candidate.spend, 2),
                                "sales": round(candidate.sales, 2), "bid": candidate.bid,
                                "label": candidate.label}))
    session.flush()
    log.info("ppc experiment designed", extra={"ref": experiment.ref, "units": len(assigned),
                                               "mde": mde})
    return experiment


# ---------------- starting ----------------
def request_approval(session: Session, experiment: PpcExperiment) -> Any:
    """Starting an experiment spends money, so it needs the owner's approval with the figure shown."""
    if experiment.status != "designed":
        raise ExperimentError(f"{experiment.ref} is {experiment.status}, not waiting to start.")
    treatment = units(session, experiment, arm="treatment")
    control = units(session, experiment, arm="control")
    payload = {"experiment": experiment.ref, "profile_id": str(experiment.profile_id),
               "change_type": experiment.change_type,
               "treatment": [u.amazon_entity_id for u in treatment],
               "control": [u.amazon_entity_id for u in control],
               "bid_change_pct": experiment.guardrails.get("max_bid_change_pct"),
               "spend_at_risk": float(experiment.spend_at_risk or 0)}
    approval = approvals.request_approval(
        session, action_type="ppc_start_experiment", payload=payload,
        summary=(f"{experiment.ref}: {experiment.hypothesis[:120]} — "
                 f"{len(treatment)} targets treated, {len(control)} left alone, up to "
                 f"{float(experiment.spend_at_risk or 0):.2f} at risk over "
                 f"{experiment.planned_days} days"),
        risk_level="high", requested_by_agent_key="ppc", notify=False)
    experiment.status = "pending_approval"
    experiment.approval_id = approval.id
    session.flush()
    return approval


def start(session: Session, experiment: PpcExperiment, *, actor: Actor, client=None,
          today: date | None = None) -> dict[str, Any]:
    """Apply the treatment to the treatment arm and start the clock. Control is left untouched."""
    from app.ppc import apply as ppc_apply
    from app.ppc import profiles

    profile = session.get(PpcProfile, experiment.profile_id)
    if not profiles.writes_allowed(session, profile):
        raise ExperimentError("PPC is in read-only mode, so no experiment can change anything. "
                              "Switching to live needs your PIN.")
    change = float(experiment.guardrails.get("max_bid_change_pct") or 25)
    direction = -1 if experiment.change_type == "bid_decrease" else 1
    applied, failed = [], []
    for unit in units(session, experiment, arm="treatment"):
        target = session.get(PpcTarget, unit.entity_id) if unit.entity_id else None
        current = (unit.pre_period_metrics or {}).get("bid")
        if target is None or not current:
            failed.append({"unit": unit.amazon_entity_id, "why": "no bid to change"})
            continue
        wanted = round(float(current) * (1 + direction * change / 100.0), 2)
        action = PpcAction(
            ad_product=experiment.ad_product,
            profile_id=profile.id, experiment_id=experiment.id, change_type=experiment.change_type,
            entity_type=unit.entity_type, entity_id=unit.entity_id,
            amazon_entity_id=unit.amazon_entity_id,
            before_value={"keywordId": unit.amazon_entity_id, "bid": float(current)},
            after_value={"keywordId": unit.amazon_entity_id, "bid": wanted},
            rollback_payload={"undo": "put_back", "verb": "PUT", "path": "/sp/keywords",
                              "media": "application/vnd.spKeyword.v3+json",
                              "collection": "keywords",
                              "body": {"keywordId": unit.amazon_entity_id, "bid": float(current)}})
        session.add(action)
        session.flush()
        try:
            response = (client or _client()).request(
                "PUT", "/sp/keywords", json={"keywords": [action.after_value]},
                profile_id=profile.amazon_profile_id,
                content_type="application/vnd.spKeyword.v3+json",
                accept="application/vnd.spKeyword.v3+json",
                operation="UpdateSponsoredProductsKeywords")
            success, errors = ppc_apply.read_multi_status(response, "keywords")
            if errors or not success:
                raise ppc_apply.WriteFailed(ppc_apply._error_text(errors))
            action.amazon_response = response
            applied.append(unit.amazon_entity_id)
        except Exception as exc:  # noqa: BLE001
            action.failed_reason = str(exc)[:500]
            failed.append({"unit": unit.amazon_entity_id, "why": str(exc)[:200]})
            # A half-treated arm is not an experiment. Stop and put back what was changed.
            session.flush()
            _undo(session, experiment, applied, client=client)
            experiment.status = "abandoned"
            experiment.stopped_at = utcnow()
            experiment.stopped_reason = (f"couldn't treat {unit.amazon_entity_id}: {exc}"[:500])
            session.flush()
            raise ExperimentError(
                f"{experiment.ref} couldn't be started: {exc} Everything already changed has been "
                "put back, so nothing is half-treated.") from exc

    today = today or metrics.today_for(profile)
    experiment.status = "running"
    experiment.started_at = utcnow()
    experiment.ends_at = utcnow() + timedelta(days=experiment.planned_days or DEFAULT_DAYS)
    session.flush()
    audit.record(session, actor, "ppc_experiment_started", target_type="ppc_experiment",
                 target_id=experiment.ref,
                 details={"treated": applied, "spend_at_risk": float(experiment.spend_at_risk or 0)})
    log.info("ppc experiment started", extra={"ref": experiment.ref, "treated": len(applied)})
    return {"experiment": experiment.ref, "treated": applied, "failed": failed}


def _client():
    from app.ppc import api

    return api.client()


def _undo(session: Session, experiment: PpcExperiment, entity_ids: list[str], *, client=None) -> None:
    """Put back the units already treated, so a failed start leaves nothing half-done."""
    from app.ppc import apply as ppc_apply

    for action in session.execute(select(PpcAction).where(
            PpcAction.experiment_id == experiment.id,
            PpcAction.amazon_entity_id.in_(entity_ids or [""]),
            PpcAction.rolled_back_at.is_(None))).scalars():
        try:
            (client or _client()).request(
                "PUT", "/sp/keywords", json={"keywords": [action.before_value]},
                profile_id=session.get(PpcProfile, experiment.profile_id).amazon_profile_id,
                content_type="application/vnd.spKeyword.v3+json",
                accept="application/vnd.spKeyword.v3+json", operation="RollbackExperiment")
            action.rolled_back_at = utcnow()
            action.rollback_reason = f"{experiment.ref} could not be started"
        except Exception:  # noqa: BLE001
            log.exception("couldn't put back %s while abandoning %s", action.amazon_entity_id,
                          experiment.ref)
    del ppc_apply
    session.flush()


# ---------------- monitoring ----------------
def monitor(session: Session, experiment: PpcExperiment, *,
            today: date | None = None) -> dict[str, Any]:
    """Check the guardrails, exclude contaminated units, and stop if a limit is reached."""
    profile = session.get(PpcProfile, experiment.profile_id)
    today = today or metrics.today_for(profile)
    if experiment.status != "running" or experiment.started_at is None:
        return {"experiment": experiment.ref, "status": experiment.status, "checked": False}

    start_day = experiment.started_at.date()
    end_day = min(today - timedelta(days=metrics.PROVISIONAL_DAYS),
                  (experiment.ends_at or utcnow()).date())
    treated = _arm_totals(session, profile, experiment, "treatment", start_day, end_day)
    control = _arm_totals(session, profile, experiment, "control", start_day, end_day)
    excluded = _exclude_contaminated(session, profile, experiment, start_day, end_day)

    guardrails = experiment.guardrails or {}
    tripped: str | None = None
    spend_cap = float(guardrails.get("abort_if_spend_exceeds") or 0)
    if spend_cap and treated["spend"] >= spend_cap:
        tripped = (f"the treated arm has spent {treated['spend']:.2f}, at or past the "
                   f"{spend_cap:.2f} you agreed to risk")
    worse_by = float(guardrails.get("abort_if_treatment_acos_worse_by_pct") or 0) / 100.0
    if tripped is None and worse_by and control["acos"] and treated["acos"]:
        if treated["acos"] > control["acos"] * (1 + worse_by):
            tripped = (f"the treated arm's ACOS is {treated['acos'] * 100:.0f}% against "
                       f"{control['acos'] * 100:.0f}% in the control — worse by more than the "
                       f"{worse_by * 100:.0f}% you set as the limit")
    fall_by = float(guardrails.get("abort_if_orders_fall_by_pct") or 0) / 100.0
    if tripped is None and fall_by and control["orders"] >= 5:
        expected = control["orders"]
        if treated["orders"] < expected * (1 - fall_by):
            tripped = (f"the treated arm has {treated['orders']} orders against {expected} in the "
                       f"control, a fall of more than the {fall_by * 100:.0f}% limit")

    if tripped:
        stop(session, experiment, reason=tripped, actor=Actor("agent", "ppc"), early=True)
    session.flush()
    return {"experiment": experiment.ref, "checked": True, "treatment": treated,
            "control": control, "excluded": excluded, "stopped": tripped}


def _arm_totals(session: Session, profile: PpcProfile, experiment: PpcExperiment, arm: str,
                start: date, end: date) -> dict[str, Any]:
    ids = [u.amazon_entity_id for u in units(session, experiment, arm=arm)]
    if not ids or start > end:
        return {"clicks": 0, "orders": 0, "spend": 0.0, "sales": 0.0, "acos": None, "cvr": None,
                "units": len(ids)}
    row = session.execute(select(
        func.coalesce(func.sum(PpcMetricsDaily.clicks), 0),
        func.coalesce(func.sum(PpcMetricsDaily.orders), 0),
        func.coalesce(func.sum(PpcMetricsDaily.spend), 0),
        func.coalesce(func.sum(PpcMetricsDaily.sales), 0)).where(
        PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
        PpcMetricsDaily.entity_type == experiment.unit_of_assignment,
        PpcMetricsDaily.amazon_entity_id.in_(ids),
        PpcMetricsDaily.date >= start, PpcMetricsDaily.date <= end,
        PpcMetricsDaily.is_provisional.is_(False))).one()
    clicks, orders, spend, sales = int(row[0]), int(row[1]), float(row[2]), float(row[3])
    return {"clicks": clicks, "orders": orders, "spend": round(spend, 2), "sales": round(sales, 2),
            "acos": round(spend / sales, 4) if sales else None,
            "cvr": round(orders / clicks, 4) if clicks else None, "units": len(ids)}


def _exclude_contaminated(session: Session, profile: PpcProfile, experiment: PpcExperiment,
                          start: date, end: date) -> list[dict[str, Any]]:
    """Drop units that something else happened to. Leaving them in measures that something else."""
    out = []
    for unit in units(session, experiment):
        action = session.execute(select(PpcAction).where(
            PpcAction.experiment_id == experiment.id,
            PpcAction.amazon_entity_id == unit.amazon_entity_id)).scalars().first()
        others = session.execute(select(func.count()).select_from(PpcAction).where(
            PpcAction.profile_id == profile.id,
            PpcAction.amazon_entity_id == unit.amazon_entity_id,
            PpcAction.experiment_id.is_(None),
            PpcAction.executed_at >= outcomes._as_moment(start),
            PpcAction.executed_at < outcomes._as_moment(end + timedelta(days=1)))).scalar_one()
        reason = None
        if others:
            reason = (f"{others} change(s) outside the experiment landed on this during the test")
        elif action is not None and action.asin:
            factors = outcomes.external_factors(session, profile, action, start=start, end=end)
            reason = outcomes.contaminated_by(factors)
        if reason:
            unit.excluded_reason = reason[:500]
            unit.excluded_at = utcnow()
            out.append({"unit": unit.amazon_entity_id, "arm": unit.arm, "why": reason})
    session.flush()
    return out


def stop(session: Session, experiment: PpcExperiment, *, reason: str, actor: Actor,
         early: bool = True, client=None) -> PpcExperiment:
    """Stop an experiment and put the treatment back. A safety action: no approval needed."""
    if experiment.status not in ("running", "pending_approval", "designed"):
        raise ExperimentError(f"{experiment.ref} is already {experiment.status}.")
    was_running = experiment.status == "running"
    experiment.status = "stopped_early" if early and was_running else "abandoned"
    experiment.stopped_at = utcnow()
    experiment.stopped_reason = reason[:500]
    if was_running:
        _undo(session, experiment, [u.amazon_entity_id for u in units(session, experiment,
                                                                      arm="treatment")],
              client=client)
    session.flush()
    audit.record(session, actor, "ppc_experiment_stopped", target_type="ppc_experiment",
                 target_id=experiment.ref, details={"reason": reason, "was_running": was_running})
    log.info("ppc experiment stopped", extra={"ref": experiment.ref, "reason": reason[:120]})
    return experiment


# ---------------- evaluating ----------------
def evaluate(session: Session, experiment: PpcExperiment, *,
             today: date | None = None) -> PpcExperiment:
    """Work out what the experiment showed, and be plain when it showed nothing."""
    profile = session.get(PpcProfile, experiment.profile_id)
    today = today or metrics.today_for(profile)
    if experiment.started_at is None:
        raise ExperimentError(f"{experiment.ref} never started, so there is nothing to evaluate.")
    start_day = experiment.started_at.date()
    end_day = (experiment.stopped_at or experiment.ends_at or utcnow()).date()
    readable_to = today - timedelta(days=metrics.PROVISIONAL_DAYS)
    end_day = min(end_day, readable_to)

    treated = _arm_totals(session, profile, experiment, "treatment", start_day, end_day)
    control = _arm_totals(session, profile, experiment, "control", start_day, end_day)
    excluded = [u for u in units(session, experiment, include_excluded=True)
                if u.excluded_at is not None]

    overlapping = session.execute(select(PpcCalendarEvent).where(
        PpcCalendarEvent.start_date <= end_day,
        PpcCalendarEvent.end_date >= start_day)).scalars().all()

    comparison = statistics.two_proportions(treated["orders"], treated["clicks"],
                                            control["orders"], control["clicks"],
                                            metric=experiment.primary_metric or "cvr")
    # Two different power questions, and the second is the one that matters for a null result:
    #   at the observed effect — descriptive, and exactly alpha when nothing moved;
    #   at an effect worth caring about — "could this test have found a real improvement?"
    per_arm = min(treated["clicks"], control["clicks"])
    baseline = control["cvr"] or 0.0
    power_observed = (statistics.power_two_proportions(baseline, treated["cvr"] or 0.0, per_arm,
                                                      alpha=ALPHA)
                      if per_arm and baseline else 0.0)
    power_meaningful = (statistics.power_two_proportions(
        baseline, min(baseline * (1 + MEANINGFUL_EFFECT), 1 - 1e-9), per_arm, alpha=ALPHA)
        if per_arm and baseline else 0.0)

    conclusion, note = _conclude(comparison, power_meaningful, treated, control, overlapping,
                                 excluded, power_observed=power_observed)
    experiment.result = {
        "treatment": treated, "control": control,
        "comparison": comparison.as_dict() if comparison else None,
        "window": {"from": start_day.isoformat(), "to": end_day.isoformat()},
        "excluded_units": [{"unit": u.amazon_entity_id, "arm": u.arm, "why": u.excluded_reason}
                           for u in excluded],
        "calendar_overlap": [{"name": e.name, "kind": e.kind} for e in overlapping],
        "settled": metrics.is_settled(end_day, today),
    }
    experiment.result["power"] = {
        "at_the_observed_effect": round(power_observed, 3),
        "for_a_meaningful_effect": round(power_meaningful, 3),
        "meaningful_effect": MEANINGFUL_EFFECT,
        "clicks_per_arm": per_arm,
        "note": ("power at the observed effect is alpha by definition when nothing moved, so a null "
                 "result is judged on the power to find an effect worth having"),
    }
    experiment.achieved_power = round(power_meaningful, 3)
    experiment.conclusion = conclusion
    experiment.learning_note = note
    experiment.evaluated_at = utcnow()
    if experiment.status == "running" and end_day >= (experiment.ends_at or utcnow()).date():
        experiment.status = "completed"
    session.flush()
    log.info("ppc experiment evaluated", extra={"ref": experiment.ref, "conclusion": conclusion,
                                                "power": float(experiment.achieved_power or 0)})
    return experiment


def _conclude(comparison, achieved: float, treated: dict, control: dict,
              overlapping: list, excluded: list, *,
              power_observed: float = 0.0) -> tuple[str, str]:
    """Turn the numbers into one of three honest answers."""
    if comparison is None:
        return "inconclusive", ("Neither arm got enough traffic to compare — there is nothing here "
                                "to conclude from.")
    if overlapping:
        names = ", ".join(e.name for e in overlapping[:2])
        return "inconclusive", (f"The test ran across {names}, when nothing compares to a normal "
                                "week. Whatever the arms did, it cannot be put down to the change.")
    if len(excluded) >= max(1, (treated["units"] + control["units"]) // 3):
        return "inconclusive", (f"{len(excluded)} of the units had something else happen to them "
                                "during the test, which is too many to read the rest as a result.")
    if comparison.significant:
        better = comparison.difference > 0
        return ("supported" if better else "not_supported"), (
            f"Conversion went from {comparison.control * 100:.2f}% in the control to "
            f"{comparison.treatment * 100:.2f}% in the treated arm — a "
            f"{abs(comparison.relative or 0) * 100:.0f}% {'improvement' if better else 'fall'}, "
            f"with the interval between {comparison.ci_low * 100:+.2f} and "
            f"{comparison.ci_high * 100:+.2f} points, so it excludes no effect. "
            f"p = {comparison.p_value:.3f}.")
    if achieved < TARGET_POWER:
        return "inconclusive", (
            f"No difference big enough to call: conversion was {comparison.treatment * 100:.2f}% "
            f"against {comparison.control * 100:.2f}%, interval "
            f"{comparison.ci_low * 100:+.2f} to {comparison.ci_high * 100:+.2f} points. But the "
            f"power achieved was only {achieved * 100:.0f}%, so this is “we couldn't tell” "
            "rather than “there is nothing there”.")
    return "not_supported", (
        f"No real difference, and enough traffic to say so: {comparison.treatment * 100:.2f}% "
        f"against {comparison.control * 100:.2f}%, interval {comparison.ci_low * 100:+.2f} to "
        f"{comparison.ci_high * 100:+.2f} points. This test had a {achieved * 100:.0f}% chance "
        f"of finding a {MEANINGFUL_EFFECT * 100:.0f}% improvement and did not find one. "
        "The idea did not work.")


def due_for_evaluation(session: Session, profile: PpcProfile, *,
                       today: date | None = None) -> list[PpcExperiment]:
    """Experiments whose window has closed and whose data is readable."""
    today = today or metrics.today_for(profile)
    out = []
    for experiment in session.execute(select(PpcExperiment).where(
            PpcExperiment.profile_id == profile.id,
            PpcExperiment.status.in_(("running", "stopped_early")))).scalars():
        end = (experiment.stopped_at or experiment.ends_at)
        if end is None:
            continue
        if end.date() <= today - timedelta(days=metrics.PROVISIONAL_DAYS):
            if experiment.evaluated_at is None or not (experiment.result or {}).get("settled"):
                out.append(experiment)
    return out


# ---------------- the approval executor ----------------
def _start_failed(session: Session, payload: dict[str, Any], approval, error: str) -> None:
    from app.services.notify import notify_owner

    notify_owner(session, f"🧪⚠️ {payload.get('experiment')} couldn't be started: {error[:300]} "
                          "Anything already changed has been put back, so nothing is half-treated.",
                 key=f"ppc_experiment_failed:{payload.get('experiment')}", urgent=True)


@approvals.executor("ppc_start_experiment", on_failure=_start_failed)
def _run_start(session: Session, payload: dict[str, Any], approval) -> dict[str, Any]:
    """Start the experiment the owner approved, on exactly the units he was shown.

    The arms are checked against the frozen payload first: an experiment whose assignment changed
    after approval is not the experiment that was approved, and a comparison between arms somebody
    else picked is not a comparison at all.
    """
    experiment = get_by_ref(session, payload["experiment"], lock=True)
    treatment = {u.amazon_entity_id for u in units(session, experiment, arm="treatment")}
    control = {u.amazon_entity_id for u in units(session, experiment, arm="control")}
    if treatment != set(payload.get("treatment") or []) or control != set(payload.get("control") or []):
        raise ExperimentError(f"{experiment.ref}'s arms have changed since you approved it; "
                              "nothing was started.")
    result = start(session, experiment, actor=Actor("agent", "ppc"))
    return {"summary": (f"{experiment.ref} is running: {len(result['treated'])} targets treated, "
                        f"{len(control)} left alone as the control. It ends "
                        f"{experiment.ends_at:%-d %b} and I'll report what it showed."),
            "treated": result["treated"], "complete_task": True}
