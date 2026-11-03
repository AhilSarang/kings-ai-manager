"""The weekly strategy report: what happened, what it cost, what was learned, what is next.

Written in code rather than by the model, for the same reason the views are: the numbers have to be
the stored ones. The LLM's part in Phase 4 is wording a recommendation's reason, never arithmetic.

The report leads with TACOS rather than ACOS, because TACOS is the number the business runs on, and
it says plainly when the honest answer is "flat" or "not enough data yet". Acceptance 14 asks for
four consecutive useful weeks; a report that manufactures a trend out of two quiet weeks would fail
that on week three.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (PpcAction, PpcActionOutcome, PpcAnalysisRun, PpcExperiment, PpcPlaybook,
                           PpcProfile, PpcRecommendation, PpcResearchNote)
from app.ppc import economics, metrics, outcomes, tacos
from app.services.common import utcnow

log = logging.getLogger(__name__)


def _money(value: float | None, currency: str = "USD") -> str:
    return metrics.money(value, currency, places=0, absolute=True)


def _pct(value: float | None, places: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{places}f}%"


def gather(session: Session, profile: PpcProfile, *, today: date | None = None,
           days: int = 7) -> dict[str, Any]:
    """Everything the week's report is built from, as plain data."""
    today = today or metrics.today_for(profile)
    since = utcnow() - timedelta(days=days)
    account = tacos.for_account(session, profile, days=30, today=today)
    trend = tacos.trend(session, profile, days=30, today=today)

    runs = list(session.execute(select(PpcAnalysisRun).where(
        PpcAnalysisRun.profile_id == profile.id,
        PpcAnalysisRun.started_at >= since)).scalars())
    recommendations = list(session.execute(select(PpcRecommendation).where(
        PpcRecommendation.profile_id == profile.id,
        PpcRecommendation.created_at >= since)).scalars())
    actions = list(session.execute(select(PpcAction).where(
        PpcAction.profile_id == profile.id, PpcAction.executed_at >= since)).scalars())
    settled = [o for o in session.execute(select(PpcActionOutcome).join(
        PpcAction, PpcActionOutcome.action_id == PpcAction.id).where(
        PpcAction.profile_id == profile.id, PpcActionOutcome.window_days == 14)).scalars()
        if (o.baseline_adjusted_effect or {}).get("settled")]

    experiments_live = list(session.execute(select(PpcExperiment).where(
        PpcExperiment.profile_id == profile.id,
        PpcExperiment.status.in_(("running", "pending_approval", "designed")))).scalars())
    experiments_done = list(session.execute(select(PpcExperiment).where(
        PpcExperiment.profile_id == profile.id,
        PpcExperiment.evaluated_at >= since)).scalars())
    rules = list(session.execute(select(PpcPlaybook)).scalars())
    notes = list(session.execute(select(PpcResearchNote).where(
        PpcResearchNote.retrieved_at >= since)).scalars())

    return {
        "profile": profile, "today": today, "days": days,
        "tacos": account, "trend": trend,
        "runs": runs, "recommendations": recommendations, "actions": actions,
        "settled_outcomes": settled,
        "experiments_live": experiments_live, "experiments_done": experiments_done,
        "rules": rules, "notes": notes,
        "calibration": outcomes.calibration(session),
        "unverified": economics.missing_cost(session, profile),
        "loss_making": economics.loss_making(session, profile),
    }


def _open_alert_lines(session: Session, profile: PpcProfile, *, limit: int = 4) -> list[str]:
    """The open alerts, worst first — the same list `PPC ALERTS` shows, trimmed for a report."""
    from app.ppc import alerts as alerts_module

    rows = alerts_module.open_alerts(session, profile)
    if not rows:
        return []
    order = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda r: (order.get(r.severity, 3), r.created_at))
    out = ["\n*Still open*\n  " + "\n  ".join(
        f"· {(row.details or {}).get('message', row.type)}" for row in rows[:limit])]
    if len(rows) > limit:
        out.append(f"  …and {len(rows) - limit} more — `PPC ALERTS`.")
    return out


def weekly(session: Session, profile: PpcProfile, *, today: date | None = None) -> str:
    """The Monday report. Honest about flat weeks and about not knowing yet."""
    data = gather(session, profile, today=today)
    currency = profile.currency
    account = data["tacos"]
    trend = data["trend"]
    lines = [f"📊 *Advertising — week to {data['today']:%-d %B}*"]

    # ---- the business numbers ----
    if not account.total_sales and not account.ad_spend:
        lines.append("\nNo finalised advertising data for the last 30 days yet, so there is nothing "
                     "to report. That is a data gap, not a quiet month.")
        # Except for the open alerts, which are the one thing worth saying when there are no numbers
        # — an account that is suspended or out of budget has no numbers *because* of that, and the
        # report that stopped one line earlier left the explanation out of the only message the owner
        # reads on a Monday.
        lines.extend(_open_alert_lines(session, profile))
        return "\n".join(lines)

    lines.append(f"\n*The month behind us*\n"
                 f"  spend {_money(account.ad_spend, currency)} · "
                 f"ad sales {_money(account.ad_sales, currency)} · "
                 f"total sales {_money(account.total_sales, currency)}\n"
                 f"  ACOS {_pct(account.acos)} · *TACOS {_pct(account.tacos)}* · "
                 f"{_pct(account.ad_share)} of sales came through an ad")

    moved = trend.get("tacos_change")
    if moved is None:
        lines.append("  _No comparable month before this one, so there is no trend to report yet._")
    elif abs(moved) < 0.005:
        lines.append("  _TACOS is flat on the month before. Flat is the honest answer, not a "
                     "failure to find a story._")
    else:
        direction = "down" if moved < 0 else "up"
        sales_moved = trend.get("sales_change", 0)
        lines.append(f"  TACOS {direction} {abs(moved) * 100:.1f} points on the previous 30 days, "
                     f"with total sales {'up' if sales_moved >= 0 else 'down'} "
                     f"{_money(abs(sales_moved), currency)}.")
        if moved < 0 and sales_moved < 0:
            lines.append("  ⚠️ Advertising is a smaller share of a smaller business — worth looking "
                         "at, because that is the trade the agent is built to avoid.")

    # ---- what was done ----
    applied = [a for a in data["actions"] if a.rolled_back_at is None]
    rolled = [a for a in data["actions"] if a.rolled_back_at is not None]
    proposed = data["recommendations"]
    approved = [r for r in proposed if r.status in ("approved", "executed")]
    rejected = [r for r in proposed if r.status == "rejected"]
    expired = [r for r in proposed if r.status == "expired"]
    lines.append(f"\n*What I did*\n  proposed {len(proposed)} changes, you approved "
                 f"{len(approved)}, turned down {len(rejected)}"
                 + (f", {len(expired)} expired unanswered" if expired else "")
                 + f"\n  {len(applied)} applied at Amazon"
                 + (f", {len(rolled)} put back" if rolled else ""))
    if rejected:
        reasons = [r.rejected_reason for r in rejected if r.rejected_reason]
        if reasons:
            lines.append(f'  Your reasons: "{reasons[0][:100]}"'
                         + (f" and {len(reasons) - 1} more" if len(reasons) > 1 else ""))

    # ---- what it did ----
    settled = data["settled_outcomes"]
    if settled:
        counts: dict[str, int] = {}
        for outcome in settled:
            counts[outcome.verdict] = counts.get(outcome.verdict, 0) + 1
        parts = ", ".join(f"{n} {v}" for v, n in sorted(counts.items()))
        lines.append(f"\n*What those changes actually did* (settled measurements only)\n  {parts}")
        if counts.get("inconclusive", 0) > sum(counts.values()) / 2:
            lines.append("  Most are still unreadable — too little traffic, or something else "
                         "happened to the product mid-window. That is normal early on.")
    else:
        lines.append("\n*What those changes actually did*\n  Nothing has settled yet. Amazon keeps "
                     "revising conversions for about five weeks, so the first honest measurements "
                     "arrive after that.")

    # ---- how good the predictions were ----
    calibration = data["calibration"]
    if calibration["scored"]:
        inside = [row["inside_range_pct"] for row in calibration["by_change_type"].values()
                  if row["inside_range_pct"] is not None]
        if inside:
            lines.append(f"\n*How good my predictions were*\n  "
                         f"{sum(inside) / len(inside):.0f}% landed inside the range I gave, across "
                         f"{calibration['scored']} scored."
                         + ("" if calibration["enough_to_judge"]
                            else " Under 50 scored, so treat that as a direction of travel."))

    # ---- experiments ----
    if data["experiments_live"] or data["experiments_done"]:
        lines.append("\n*Experiments*")
        for experiment in data["experiments_live"]:
            ends = f", ends {experiment.ends_at:%-d %b}" if experiment.ends_at else ""
            lines.append(f"  {experiment.ref} {experiment.status.replace('_', ' ')}{ends} — "
                         f"{experiment.hypothesis[:100]}")
        for experiment in data["experiments_done"]:
            lines.append(f"  {experiment.ref} {experiment.conclusion or 'evaluated'} — "
                         f"{(experiment.learning_note or '')[:160]}")

    # ---- the playbook ----
    rules = data["rules"]
    live = [r for r in rules if r.status in ("active", "on_probation")]
    waiting = [r for r in rules if r.status == "proposed"]
    if rules:
        lines.append(f"\n*The playbook*\n  {len(live)} rules in force"
                     + (f", {len([r for r in live if r.status == 'on_probation'])} on probation"
                        if any(r.status == "on_probation" for r in live) else "")
                     + (f", {len(waiting)} waiting for you" if waiting else ""))
        for rule in waiting[:3]:
            lines.append(f"  {rule.ref}: {rule.title}")

    # ---- research ----
    if data["notes"]:
        lines.append(f"\n*Read this week*\n  {len(data['notes'])} claims recorded from Amazon's own "
                     "material and the trade press. None of them changes anything until it has been "
                     "tested here.")

    # ---- the shape of the day (M11), when there is hourly data to see it in ----
    from app.ppc import dayparting

    shaped = dayparting.shape(session, profile, today=today)
    if shaped.has_data:
        lines.append(f"\n*The shape of the day*\n  {dayparting.summary(shaped)}")

    # ---- what is still wrong (M11) ----
    lines.extend(_open_alert_lines(session, profile))

    # ---- what is blocking better answers ----
    blocked = []
    if not shaped.has_data:
        # Named here rather than in the dayparting section, because a missing subscription is a
        # thing the owner could fix, not a thing the report is quietly doing without.
        blocked.append("no hourly data, so I can't see which hours of the day sell and which only "
                       "spend — that needs a Marketing Stream subscription")
    if data["unverified"]:
        blocked.append(f"{len(data['unverified'])} products have no unit cost, so I can't tell what "
                       "they can afford to spend")
    if data["loss_making"]:
        blocked.append(f"{len(data['loss_making'])} lose money before advertising")
    if blocked:
        lines.append("\n*What's holding me back*\n  " + "\n  ".join(f"· {b}" for b in blocked))

    lines.append("\n`PPC` for the current numbers · `PPC ALERTS` for what's wrong · "
                 "`OUTCOMES` for what changes did · `PLAYBOOK` for what I've learned")
    return "\n".join(lines)
