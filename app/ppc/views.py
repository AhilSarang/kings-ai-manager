"""WhatsApp views for PPC: plain code, no LLM, so the numbers shown are the stored ones.

Everything here is deterministic on purpose: status, lookups and summaries are code. The
LLM's job in Phase 4 is reasoning and wording in recommendations, never arithmetic.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import PpcCampaign, PpcProductEconomics, PpcProfile
from app.ppc import economics, metrics, profiles, tacos
from app.ppc.profiles import PpcNotConnected
from app.services import settings_store


# Every view here reads Sponsored Products. Named rather than assumed, so that when Brands and
# Display arrive the views say which one they are showing instead of quietly blending all three.
AD_PRODUCT = "sp"


def _day(value) -> str:
    """"21 Sep" without the platform-specific %-d."""
    return f"{value.day} {value:%b}"


def _pct(value: float | None, places: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{places}f}%"


def _money(value: float | None, currency: str = "USD") -> str:
    return metrics.money(value, currency)


def not_connected_yet() -> str:
    return ("📊 Amazon Ads isn't connected yet. It's a separate registration from the Seller Central "
            "app: an Amazon Ads developer application approved for the Kings Distro advertising "
            "account. Once that's approved I need the client id, secret and refresh token, and I'll "
            "take it from there.")


def ppc_summary(session: Session) -> str:
    """`PPC` — where advertising stands, in the numbers the business runs on."""
    try:
        profile = profiles.require(session)
    except PpcNotConnected as exc:
        return not_connected_yet() if "isn't connected yet" in str(exc) else f"📊⚠️ {exc}"

    today = metrics.today_for(profile)
    lines = [f"📊 *Amazon Ads — {profile.country_code}*"]
    if settings_store.get(session, "ppc_paused", False):
        lines.append("⏸️ _Advertising data and analysis are paused — these numbers stop here until "
                     "you send PPC RESUME. Your campaigns are still running at Amazon._")

    if profile.last_synced_at is None:
        # Saying "0 campaigns" before ever reading the account states a fact we do not have.
        lines.append("I haven't read the account's campaigns yet.")
    else:
        live = session.execute(select(PpcCampaign).where(
            PpcCampaign.profile_id == profile.id, PpcCampaign.state == "enabled")).scalars().all()
        not_serving = [c for c in live if c.serving_status and c.serving_status.upper() not in
                       ("DELIVERING", "CAMPAIGN_STATUS_ENABLED")]
        lines.append(f"{len(live)} campaigns running" +
                     (f", {len(not_serving)} not actually serving" if not_serving else ""))

    for days in (7, 30):
        got = tacos.for_account(session, profile, days=days, today=today)
        if not got.total_sales and not got.ad_spend:
            lines.append(f"\n*Last {days} days:* nothing loaded yet")
            continue
        lines.append(
            f"\n*Last {days} days* ({_day(got.start)}–{_day(got.end)})\n"
            f"  spend {_money(got.ad_spend, profile.currency)} · "
            f"ad sales {_money(got.ad_sales, profile.currency)} · "
            f"total sales {_money(got.total_sales, profile.currency)}\n"
            f"  ACOS {_pct(got.acos)} · TACOS {_pct(got.tacos)} · "
            f"{_pct(got.ad_share)} of sales came from ads")
        for note in got.notes:
            lines.append(f"  ⚠️ {note}")

    moved = tacos.trend(session, profile, days=30, today=today)
    if moved["tacos_change"] is not None:
        direction = "down" if moved["tacos_change"] < 0 else "up"
        lines.append(f"\nTACOS is {direction} {abs(moved['tacos_change']) * 100:.1f} points on the "
                     f"30 days before, with sales {'up' if moved['sales_change'] >= 0 else 'down'} "
                     f"{_money(abs(moved['sales_change']), profile.currency)}.")

    no_cost = economics.missing_cost(session, profile)
    if no_cost:
        names = ", ".join(row.asin for row in no_cost[:5])
        more = f" and {len(no_cost) - 5} more" if len(no_cost) > 5 else ""
        lines.append(f"\n⚠️ {len(no_cost)} products have no target ACOS because I don't have your unit "
                     f"cost ({names}{more}). `COST <ASIN> <amount>` fixes each one.")
    losing = economics.loss_making(session, profile)
    if losing:
        names = ", ".join(row.asin for row in losing[:5])
        lines.append(f"\n⚠️ {len(losing)} products lose money on every sale before advertising "
                     f"({names}). No amount of bidding fixes that — it's a price or cost question.")

    lines.append(f"\nWrite mode: {profile.write_mode.replace('_', ' ')}. "
                 "Nothing changes at Amazon without your approval.")
    lines.append("`PPC ASIN <asin>` for one product · `PPC BRANDS` per brand · "
                 "`PPC ECONOMICS` for margins")
    return "\n".join(lines)


def asin_summary(session: Session, asin: str) -> str:
    """`PPC ASIN B0…` — one product's advertising, against what it can afford."""
    try:
        profile = profiles.require(session)
    except PpcNotConnected as exc:
        return not_connected_yet() if "isn't connected yet" in str(exc) else f"📊⚠️ {exc}"

    asin = asin.strip().upper()
    today = metrics.today_for(profile)
    row = economics.current(session, asin, profile.marketplace_id)
    lines = [f"📊 *{asin}*"]

    if row is None:
        lines.append("I have no economics for this ASIN yet — it may not be advertised, or the "
                     "catalogue hasn't been read since it was added.")
    else:
        # Amazon's cut is only shown when Amazon actually gave one: printing $0.00 for a missing
        # estimate reads as "Amazon takes nothing", which is never true.
        cut = (_money(float(row.referral_fee) + float(row.fba_fee or 0), row.currency)
               if row.referral_fee is not None else "not known yet")
        lines.append(
            f"price {_money(row.sale_price, row.currency)} · cost {_money(row.unit_cost, row.currency)} · "
            f"Amazon's cut {cut}")
        if row.contribution_margin is not None:
            lines.append(f"margin {_money(row.contribution_margin, row.currency)} per unit · "
                         f"break-even ACOS {_pct(float(row.break_even_acos))}")
        if row.target_acos is not None:
            source = {"owner": "you set it", "derived": "from the margin",
                      "learned": "learned"}.get(row.target_source, row.target_source)
            lines.append(f"target ACOS {_pct(float(row.target_acos))} ({source})")
        elif row.source_note:
            lines.append(f"⚠️ no target ACOS: {row.source_note}")

    for days in (7, 30):
        got = tacos.for_asin(session, profile, asin, days=days, today=today)
        if not (got.ad_spend or got.total_sales):
            continue
        line = (f"\n*Last {days} days:* spend {_money(got.ad_spend, profile.currency)} · "
                f"ad sales {_money(got.ad_sales, profile.currency)} · "
                f"total {_money(got.total_sales, profile.currency)}\n"
                f"  ACOS {_pct(got.acos)} · TACOS {_pct(got.tacos)}")
        if row is not None and row.target_acos is not None and got.acos is not None:
            over = got.acos > float(row.target_acos)
            line += f" · {'above' if over else 'within'} target"
        lines.append(line)
        for note in got.notes:
            lines.append(f"  ⚠️ {note}")
    return "\n".join(lines)


def brands_summary(session: Session) -> str:
    """`PPC BRANDS` — TACOS per brand, which is how the owner thinks about the business."""
    try:
        profile = profiles.require(session)
    except PpcNotConnected as exc:
        return not_connected_yet() if "isn't connected yet" in str(exc) else f"📊⚠️ {exc}"

    today = metrics.today_for(profile)
    lines = ["📊 *Advertising by brand, last 30 days*"]
    seen = False
    for row in tacos.overview(session, profile, today=today, windows=(30,))["windows"][30]["brands"]:
        if not (row["ad_spend"] or row["total_sales"]):
            continue
        seen = True
        lines.append(f"\n*{row['label']}* — spend {_money(row['ad_spend'], profile.currency)} · "
                     f"total sales {_money(row['total_sales'], profile.currency)}\n"
                     f"  ACOS {_pct(row['acos'])} · TACOS {_pct(row['tacos'])}")
    if not seen:
        lines.append("\nNothing advertised under a brand yet.")
    return "\n".join(lines)


def economics_summary(session: Session, limit: int = 15) -> str:
    """`PPC ECONOMICS` — what each product can afford, worst margin first."""
    try:
        profile = profiles.require(session)
    except PpcNotConnected as exc:
        return not_connected_yet() if "isn't connected yet" in str(exc) else f"📊⚠️ {exc}"

    rows = list(session.execute(select(PpcProductEconomics).where(
        PpcProductEconomics.marketplace_id == profile.marketplace_id,
        PpcProductEconomics.valid_to.is_(None)).order_by(
        PpcProductEconomics.break_even_acos.asc().nulls_last())).scalars())
    if not rows:
        return ("📊 I haven't worked out the margins yet. That needs each product's price from "
                "Amazon, your unit cost, and Amazon's own fee estimate.")

    lines = [f"📊 *What each product can afford* ({profile.country_code})",
             "_break-even ACOS is where an advertised sale earns nothing_"]
    for row in rows[:limit]:
        if row.break_even_acos is None:
            lines.append(f"\n*{row.asin}* — ⚠️ {row.source_note or 'not enough information'}")
            continue
        lines.append(
            f"\n*{row.asin}* {_money(row.sale_price, row.currency)} → margin "
            f"{_money(row.contribution_margin, row.currency)}\n"
            f"  break-even {_pct(float(row.break_even_acos))} · "
            f"target {_pct(float(row.target_acos)) if row.target_acos is not None else '—'}")
    if len(rows) > limit:
        lines.append(f"\n…and {len(rows) - limit} more.")
    no_cost = economics.missing_cost(session, profile)
    if no_cost:
        lines.append(f"\n⚠️ {len(no_cost)} have no target because I don't have your unit cost. "
                     "`COST <ASIN> <amount>` fixes each.")
    losing = economics.loss_making(session, profile)
    if losing:
        lines.append(f"⚠️ {len(losing)} lose money before advertising. Bidding can't fix those.")
    return "\n".join(lines)


CHANGE_WORDS = {
    "bid_increase": "raise a bid", "bid_decrease": "cut a bid", "pause_target": "pause a target",
    "enable_target": "switch a target back on", "add_negative_exact": "block a search",
    "add_negative_phrase": "block a pattern", "harvest_search_term": "keep a winning search",
    "budget_increase": "raise a budget", "budget_decrease": "cut a budget",
    "placement_adjustment": "adjust a placement", "campaign_structure": "change the structure",
    "dayparting": "change the hours", "other": "something else",
}


def findings_summary(session: Session, limit: int = 12) -> str:
    """`PPC FINDINGS` — what the analysers make of the account right now. Nothing is stored or sent.

    This is the read-only view the rollout leans on: the owner can compare what the agent would propose
    against what he would do, before anything can be approved. M6 turns the same candidates into
    numbered recommendations with approvals.
    """
    from app.ppc import analysers

    try:
        profile = profiles.require(session)
    except PpcNotConnected as exc:
        return not_connected_yet() if "isn't connected yet" in str(exc) else f"📊⚠️ {exc}"
    if profile.last_synced_at is None:
        return ("📊 I haven't read the account yet, so there is nothing to analyse. The campaigns and "
                "their numbers come in on the next sync.")

    if settings_store.get(session, "ppc_paused", False):
        header = ("⏸️ _Advertising analysis is paused, so this is from whatever data I already "
                  "have. Your campaigns are still running at Amazon._\n")
    else:
        header = ""
    result = analysers.run_all(session, profile)
    found = result["candidates"]
    if not (found or result["held"] or result["in_cooldown"]):
        return (header + f"📊 Nothing worth changing in the last {result['window_days']} days of "
                "finalised data. That is a real answer, not an empty one — the analysers ran and "
                "every target is inside its limits.")

    by_type: dict[str, list] = {}
    for candidate in found:
        by_type.setdefault(candidate.change_type, []).append(candidate)
    lines = [header.rstrip("\n")] if header else []
    if found:
        lines.append(f"📊 *What I'd change* — {len(found)} things, from the last "
                     f"{result['window_days']} days")
    else:
        # Everything found was held or cooling. Saying "0 things" under a "what I'd change" heading
        # reads as a broken message rather than as the answer it is.
        lines.append(f"📊 *Nothing I'd change today* — from the last {result['window_days']} days")
    for change_type, group in sorted(by_type.items(), key=lambda kv: -sum(
            abs(c.monthly_effect) for c in kv[1])):
        worth = sum(c.monthly_effect for c in group)
        money = f" (about {_money(abs(worth), profile.currency)}/month)" if abs(worth) >= 1 else ""
        lines.append(f"• {len(group)} × {CHANGE_WORDS.get(change_type, change_type)}{money}")

    if found:
        lines.append("\n*The biggest ones:*")
    for candidate in found[:limit]:
        value = ""
        if candidate.current_value is not None and not isinstance(candidate.proposed_value, dict):
            value = f" — {candidate.current_value} → {candidate.proposed_value}"
        lines.append(f"\n*{candidate.label or candidate.entity_id}*{value}\n  {candidate.reason}")

    if result["held"]:
        lines.append(f"\n⚠️ *{len(result['held'])} held back* — the numbers say spend more, the "
                     "product says not yet:")
        for candidate in result["held"][:5]:
            lines.append(f"  • {candidate.label or candidate.entity_id}: {candidate.why_not}")
    if result["in_cooldown"]:
        lines.append(f"\n⏳ {len(result['in_cooldown'])} left alone — changed too recently to judge "
                     "the last change yet.")
    lines.append("\nNothing here is queued or sent. Approvals come with the daily digest.")
    return "\n".join(lines)


VERDICT_WORDS = {"positive": "✅ helped", "negative": "⛔ made it worse",
                 "neutral": "➖ no real change", "inconclusive": "❓ can't tell"}


def outcomes_summary(session: Session, limit: int = 10) -> str:
    """`OUTCOMES` — what the changes actually did, measured against the rest of the account."""
    from app.db.models import PpcAction, PpcActionOutcome
    from app.ppc import outcomes

    try:
        profile = profiles.require(session)
    except PpcNotConnected as exc:
        return not_connected_yet() if "isn't connected yet" in str(exc) else f"📊⚠️ {exc}"

    rows = session.execute(select(PpcActionOutcome, PpcAction).join(
        PpcAction, PpcActionOutcome.action_id == PpcAction.id).where(
        PpcAction.profile_id == profile.id).order_by(
        PpcActionOutcome.evaluated_at.desc())).all()
    if not rows:
        return ("📊 Nothing measured yet. A change is measured 7, 14 and 28 days after it is made, "
                "and only once Amazon has stopped revising the numbers — so the first readings "
                "arrive about ten days after the first approved change.")

    counts = outcomes.verdict_counts(session, profile)
    lines = ["📊 *What the changes actually did*"]
    if counts:
        for change_type, verdicts in sorted(counts.items()):
            total = sum(verdicts.values())
            parts = ", ".join(f"{n} {VERDICT_WORDS.get(v, v)}" for v, n in sorted(verdicts.items()))
            lines.append(f"• *{change_type.replace('_', ' ')}* ({total} measured): {parts}")
    else:
        lines.append("_Nothing has settled yet — the numbers are still being revised by Amazon._")

    lines.append("\n*The most recent:*")
    for outcome, action in rows[:limit]:
        effect = outcome.baseline_adjusted_effect or {}
        settled = "" if effect.get("settled") else " _(early read; Amazon still revising)_"
        lines.append(f"\n{VERDICT_WORDS.get(outcome.verdict, outcome.verdict)} — "
                     f"{action.change_type.replace('_', ' ')} on "
                     f"{action.amazon_entity_id} at {outcome.window_days} days{settled}\n"
                     f"  {effect.get('headline', '')}")
        if outcome.contaminated:
            lines.append("  ⚠️ " + (outcomes.contaminated_by(outcome.external_factors) or ""))
    lines.append("\nEvery figure has the rest of the account taken out of it, so an account-wide "
                 "change isn't credited to one bid.")
    return "\n".join(lines)


def calibration_summary(session: Session) -> str:
    """`CALIBRATION` — how often the agent's predictions actually come true (Section 11)."""
    from app.ppc import outcomes

    got = outcomes.calibration(session)
    if not got["scored"]:
        return ("📊 No predictions scored yet. Each approved change carries a predicted effect with "
                "a range; once the change has been measured and Amazon has stopped revising the "
                "numbers, the prediction is scored against what happened.")
    lines = [f"📊 *How good my predictions have been* — {got['scored']} scored"]
    if not got["enough_to_judge"]:
        lines.append("_Under 50, so treat this as a direction of travel rather than a verdict._")
    for kind, row in got["by_change_type"].items():
        inside = (f"{row['inside_range_pct']:.0f}% landed inside my own range"
                  if row["inside_range_pct"] is not None else "no range to compare")
        lines.append(f"\n*{kind.replace('_', ' ')}* — {row['scored']} scored\n"
                     f"  {inside} · {row['right_direction_pct']:.0f}% right about the direction\n"
                     f"  typical miss {row['mean_absolute_error']}/month · "
                     f"I claimed {(row['stated_confidence'] or 0) * 100:.0f}% confidence")
    lines.append("\nLanding inside the range is the number that matters: being right about the "
                 "direction and wrong about the size is still badly calibrated.")
    return "\n".join(lines)


CONCLUSION_WORDS = {"supported": "✅ it worked", "not_supported": "⛔ it didn't work",
                    "inconclusive": "❓ couldn't tell"}
EXPERIMENT_STATUS = {"designed": "designed, waiting for you", "pending_approval": "waiting for you",
                     "running": "running", "stopped_early": "stopped early",
                     "completed": "finished", "abandoned": "abandoned"}


def experiments_summary(session: Session) -> str:
    """`EXPERIMENTS` — what is being tested, and what the finished ones showed."""
    from app.db.models import PpcExperiment
    from app.ppc import experiments as exp

    try:
        profile = profiles.require(session)
    except PpcNotConnected as exc:
        return not_connected_yet() if "isn't connected yet" in str(exc) else f"📊⚠️ {exc}"

    rows = list(session.execute(select(PpcExperiment).where(
        PpcExperiment.profile_id == profile.id).order_by(
        PpcExperiment.created_at.desc())).scalars())
    if not rows:
        return ("🧪 No experiments yet. An experiment is a controlled test: half the targets get the "
                "change, half are left exactly as they are, and the two are compared at the end. "
                "I'll propose one when there is enough traffic for the answer to mean something.")

    live = [r for r in rows if r.status == "running"]
    lines = [f"🧪 *Experiments* — {len(live)} running"]
    for row in rows[:8]:
        state = EXPERIMENT_STATUS.get(row.status, row.status)
        lines.append(f"\n*{row.ref}* — {state}\n  _{row.hypothesis[:140]}_")
        if row.status == "running" and row.ends_at:
            lines.append(f"  ends {row.ends_at:%-d %b} · up to "
                         f"{_money(float(row.spend_at_risk or 0), profile.currency)} at risk")
        if row.conclusion:
            lines.append(f"  {CONCLUSION_WORDS.get(row.conclusion, row.conclusion)} — "
                         f"{(row.learning_note or '')[:200]}")
    lines.append("\n`X-1` for one in full. `STOP X-1` ends one and puts the bids back.")
    return "\n".join(lines)


def experiment_detail(session: Session, ref: str) -> str:
    """`X-1` — the design, the arms, the guardrails and the result, with nothing hidden."""
    from app.ppc import experiments as exp

    row = exp.get_by_ref(session, ref)
    design = row.design or {}
    power = design.get("power") or {}
    treatment = exp.units(session, row, arm="treatment")
    control = exp.units(session, row, arm="control")
    lines = [f"🧪 *{row.ref}* — {EXPERIMENT_STATUS.get(row.status, row.status)}",
             f"_{row.hypothesis}_",
             f"\n*The test:* {design.get('treatment_definition', row.change_type)}; "
             f"the control arm is {design.get('control_definition', 'left alone')}.",
             f"*The split:* {len(treatment)} treated, {len(control)} left alone, assigned by "
             f"{design.get('assignment', 'randomisation')}."]
    if power:
        mde = power.get("minimum_detectable_relative_effect")
        lines.append(f"*What it can see:* with about {power.get('expected_clicks_per_arm')} clicks "
                     f"per arm against a {power.get('baseline_cvr', 0) * 100:.1f}% conversion rate, "
                     f"the smallest change it could detect is "
                     f"{'—' if mde is None else f'{mde * 100:.0f}%'}.")
    guardrails = row.guardrails or {}
    if guardrails:
        lines.append(f"*Limits:* stops if spend passes "
                     f"{float(guardrails.get('abort_if_spend_exceeds') or 0):.2f}, if the treated "
                     f"arm's ACOS is more than "
                     f"{guardrails.get('abort_if_treatment_acos_worse_by_pct')}% worse, or if orders "
                     f"fall by more than {guardrails.get('abort_if_orders_fall_by_pct')}%.")
    if row.stopped_reason:
        lines.append(f"\n⚠️ *Stopped:* {row.stopped_reason}")

    result = row.result or {}
    if result:
        comparison = result.get("comparison") or {}
        lines.append(f"\n*Result:* {CONCLUSION_WORDS.get(row.conclusion, row.conclusion or '—')}")
        lines.append(f"{row.learning_note or ''}")
        if comparison:
            lines.append(f"\nTreated {comparison['treatment'] * 100:.2f}% · "
                         f"control {comparison['control'] * 100:.2f}% · "
                         f"interval {comparison['confidence_interval'][0] * 100:+.2f} to "
                         f"{comparison['confidence_interval'][1] * 100:+.2f} points · "
                         f"p {comparison['p_value']:.3f} ({comparison['test']})")
        lines.append(f"Power achieved: {float(row.achieved_power or 0) * 100:.0f}%")
        if not result.get("settled"):
            lines.append("_Amazon is still revising these numbers, so this is an early read._")
        for dropped in (result.get("excluded_units") or [])[:4]:
            lines.append(f"· {dropped['unit']} dropped from the {dropped['arm']} arm: "
                         f"{dropped['why']}")
    return "\n".join(lines)


PLAYBOOK_STATUS = {"proposed": "waiting for you", "active": "in force",
                   "on_probation": "on probation", "retired": "retired"}
ORIGIN_WORDS = {"experiment": "from an experiment", "outcome_analysis": "from measured outcomes",
                "owner_rejection_pattern": "from what you keep turning down"}


def playbook_summary(session: Session) -> str:
    """`PLAYBOOK` — what the agent has actually learned, and how each rule is doing."""

    from app.db.models import PpcPlaybook

    rules = list(session.execute(select(PpcPlaybook).order_by(
        PpcPlaybook.created_at)).scalars())
    if not rules:
        return ("📘 The playbook is empty, which is the honest state of it. A rule can only come from "
                "a finished experiment, from at least 30 measured changes of one kind, or from a "
                "pattern in what you turn down — so it fills up at the speed the data allows, not "
                "faster.")
    live = [r for r in rules if r.status in ("active", "on_probation")]
    lines = [f"📘 *The playbook* — {len(live)} rules in force"]
    for rule in rules:
        if rule.status == "retired":
            continue
        state = PLAYBOOK_STATUS.get(rule.status, rule.status)
        origin = ORIGIN_WORDS.get(rule.created_from, rule.created_from)
        lines.append(f"\n*{rule.ref}* — {state}, {origin}\n  {rule.title}")
        if rule.times_applied:
            rate = (f", {float(rule.success_rate) * 100:.0f}% of them helped"
                    if rule.success_rate is not None else ", not measured yet")
            lines.append(f"  _applied {rule.times_applied} times{rate}_")
        if rule.probation_reason:
            lines.append(f"  ⚠️ {rule.probation_reason}")
    retired = [r for r in rules if r.status == "retired"]
    if retired:
        lines.append(f"\n{len(retired)} retired: " + ", ".join(r.ref for r in retired[:6]))
    lines.append("\n`R-1` for one in full, with the evidence behind it.")
    return "\n".join(lines)


def playbook_detail(session: Session, ref: str) -> str:
    """`R-1` — one rule, its statement, its evidence and its record."""
    from app.ppc import playbook

    rule = playbook.get_by_ref(session, ref)
    evidence = rule.evidence or {}
    lines = [f"📘 *{rule.ref}* — {PLAYBOOK_STATUS.get(rule.status, rule.status)}",
             f"*{rule.title}*", f"\n{rule.statement}",
             f"\n_Where it came from:_ {ORIGIN_WORDS.get(rule.created_from, rule.created_from)}"]
    if evidence.get("experiment"):
        lines.append(f"Experiment: {evidence['experiment']}")
    if evidence.get("measured"):
        lines.append(f"Measured changes: {evidence['measured']} "
                     f"({evidence.get('positive', 0)} helped, {evidence.get('negative', 0)} didn't)")
    if evidence.get("rejections"):
        lines.append(f"You turned down {evidence['rejections']}: "
                     + ", ".join(evidence.get("refs", [])[:5]))
        for reason in (evidence.get("reasons") or [])[:3]:
            lines.append(f'  · "{reason[:120]}"')
    lines.append(f"\n_What it does:_ it can {rule.action_kind} a recommendation when "
                 f"{_conditions_in_words(rule.conditions)}.")
    if rule.times_applied:
        lines.append(f"\n_Its record:_ applied {rule.times_applied} times"
                     + (f", {float(rule.success_rate) * 100:.0f}% of the measured ones helped."
                        if rule.success_rate is not None
                        else ", none measured yet."))
    if rule.probation_reason:
        lines.append(f"\n⚠️ {rule.probation_reason}")
    if rule.status == "proposed":
        lines.append("\nIt does nothing until you approve it.")
    return "\n".join(lines)


def _conditions_in_words(conditions: dict) -> str:
    parts = []
    if conditions.get("change_type"):
        parts.append(f"the change is a {conditions['change_type'].replace('_', ' ')}")
    if conditions.get("min_clicks"):
        parts.append(f"there are at least {conditions['min_clicks']} clicks behind it")
    if conditions.get("acos_above") is not None:
        parts.append(f"ACOS is above {float(conditions['acos_above']) * 100:.0f}%")
    return " and ".join(parts) or "it applies at all"


def playbook_proposal(session: Session, ref: str, *, approval=None) -> str:
    """The message that asks the owner to adopt a rule, with the evidence in front of him."""
    text = playbook_detail(session, ref)
    if approval is None:
        return text
    return (text + f"\n\n*To adopt it:* `APPROVE {approval.ref} {approval.confirmation_code}`"
                   f"\n*To turn it down:* `REJECT {approval.ref} why` — and I won't propose it again.")


def asin_deep_dive(session: Session, asin: str, limit: int = 6) -> str:
    """`PPC DEEP <asin>` — one product's advertising from every angle the data allows.

    The shallow view (`PPC ASIN`) answers "how is it doing". This answers "what is going on", which
    needs the search terms, the placements and the targets as well as the totals.
    """
    from app.db.models import PpcSearchTerm
    from app.ppc import aggregates

    try:
        profile = profiles.require(session)
    except PpcNotConnected as exc:
        return not_connected_yet() if "isn't connected yet" in str(exc) else f"📊⚠️ {exc}"

    asin = asin.strip().upper()
    today = metrics.today_for(profile)
    lines = [asin_summary(session, asin)]

    ad_groups = [group for group, asins in aggregates.asin_for_ad_group(session, profile, ad_product=AD_PRODUCT).items()
                 if asin in asins]
    if not ad_groups:
        lines.append("\n_Nothing is advertising this product, so there is no detail to show._")
        return "\n".join(lines)

    targets = aggregates.targets(session, profile, ad_product=AD_PRODUCT,
                                 states=("enabled", "paused"))
    totals = aggregates.by_entity(session, profile, "target", ad_product=AD_PRODUCT, days=30,
                                   today=today)
    mine = [(targets[tid], rollup) for tid, rollup in totals.items()
            if tid in targets and targets[tid].amazon_ad_group_id in ad_groups]
    worst = sorted((pair for pair in mine if pair[1].clicks),
                   key=lambda pair: -(pair[1].acos or 9.9))[:limit]
    best = sorted((pair for pair in mine if pair[1].orders),
                  key=lambda pair: (pair[1].acos or 9.9))[:limit]
    target_acos, source = economics.target_for(session, asin, profile.marketplace_id)
    if target_acos:
        lines.append(f"\n_Judged against a {_pct(target_acos)} target ({source})._")

    if best:
        lines.append("\n*Working hardest*")
        for target, rollup in best:
            lines.append(f"  {target.expression or target.amazon_target_id} — "
                         f"{rollup.orders} orders, ACOS {_pct(rollup.acos)}, "
                         f"{rollup.clicks} clicks")
    if worst:
        lines.append("\n*Costing most per sale*")
        for target, rollup in worst:
            verdict = "no orders at all" if not rollup.orders else f"ACOS {_pct(rollup.acos)}"
            lines.append(f"  {target.expression or target.amazon_target_id} — {verdict}, "
                         f"{_money(rollup.spend, profile.currency)} over {rollup.clicks} clicks")

    terms = [t for t in aggregates.search_terms(session, profile, ad_product=AD_PRODUCT, days=30, today=today)
             if t.ad_group_id in ad_groups]
    sellers = sorted((t for t in terms if t.orders), key=lambda t: -t.orders)[:limit]
    wasters = sorted((t for t in terms if not t.orders), key=lambda t: -t.spend)[:limit]
    if sellers:
        lines.append("\n*What shoppers actually searched, and bought*")
        for term in sellers:
            lines.append(f"  \u201c{term.search_term}\u201d — {term.orders} orders, "
                         f"ACOS {_pct(term.acos)} ({term.harvest_status})")
    if wasters:
        lines.append("\n*Searches costing money and selling nothing*")
        for term in wasters:
            lines.append(f"  \u201c{term.search_term}\u201d — "
                         f"{_money(term.spend, profile.currency)}, {term.clicks} clicks")

    placements = aggregates.placements(session, profile, ad_product=AD_PRODUCT, days=30, today=today)
    relevant = [row for rows in placements.values() for row in rows
                if row.clicks and any(t.amazon_campaign_id == row.campaign_id for t, _r in mine)]
    if relevant:
        lines.append("\n*Where it converts*")
        for row in sorted(relevant, key=lambda r: -(r.clicks or 0))[:4]:
            lines.append(f"  {row.placement.replace('_', ' ')} — ACOS {_pct(row.acos)}, "
                         f"{row.clicks} clicks")
    return "\n".join(lines)


def research_summary(session: Session, limit: int = 12) -> str:
    """`RESEARCH` — what the agent has read, and what it has done about it, which is nothing yet."""
    from app.db.models import PpcResearchNote

    rows = list(session.execute(select(PpcResearchNote).order_by(
        PpcResearchNote.retrieved_at.desc())).scalars())
    if not rows:
        return ("📚 Nothing read yet. Once a week I read Amazon's own advertising documentation and "
                "changelog and record any testable claim in my own words. A claim never changes "
                "anything here until it has been tested on this account.")
    status_words = {"untested": "untested", "experiment_designed": "being tested",
                    "tested_supported": "✅ held up here", "tested_rejected": "⛔ didn't hold up here",
                    "not_applicable": "doesn't apply to us"}
    lines = [f"📚 *What I've read* — {len(rows)} claims recorded"]
    for note in rows[:limit]:
        where = (note.source_type or "").replace("_", " ")
        lines.append(f"\n*{status_words.get(note.status, note.status)}* · _{where}_\n"
                     f"  {note.claim}")
        if note.applicability_note:
            lines.append(f"  _{note.applicability_note[:180]}_")
    untested = [n for n in rows if n.status == "untested"]
    if untested:
        lines.append(f"\n{len(untested)} untested. None of them is affecting anything — a claim "
                     "becomes an experiment or it stays a claim.")
    return "\n".join(lines)


# ---------- alerts, budgets and the shape of the day (Phase 4 M11) ----------
def alerts_summary(session: Session) -> str:
    """`PPC ALERTS` — what is wrong right now, worst first."""
    from app.db.models import PpcAlert
    from app.ppc import alerts as alerts_module

    try:
        profile = profiles.require(session)
    except PpcNotConnected as exc:
        return not_connected_yet() if "isn't connected yet" in str(exc) else f"📊⚠️ {exc}"

    rows = alerts_module.open_alerts(session, profile)      # muted ones included: still open
    if not rows:
        recent = session.execute(
            select(PpcAlert).where(PpcAlert.profile_id == profile.id,
                                   PpcAlert.status == "resolved")
            .order_by(PpcAlert.resolved_at.desc()).limit(3)).scalars().all()
        if recent:
            cleared = ", ".join((r.details or {}).get("message", r.type)[:60] for r in recent)
            return f"✅ Nothing wrong that I can see. Recently cleared: {cleared}"
        return "✅ Nothing wrong that I can see."

    order = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda r: (order.get(r.severity, 3), r.created_at))
    mark = {"high": "🚨", "medium": "⚠️", "low": "·"}
    lines = [f"📊 {len(rows)} open alert(s):", ""]
    for row in rows[:15]:
        age = (metrics.now_for(profile).date() - row.created_at.date()).days
        since = "today" if age == 0 else f"{age}d"
        quiet = ", muted" if row.status == "muted" else ""
        lines.append(f"{mark.get(row.severity, '·')} {(row.details or {}).get('message', row.type)} "
                     f"({since}{quiet})")
    if len(rows) > 15:
        lines.append(f"…and {len(rows) - 15} more.")
    muted = [r for r in rows if r.status == "muted"]
    if muted:
        lines.append(f"\n{len(muted)} of these are muted — still open, just not messaged about.")
    return "\n".join(lines)


def budgets_now(session: Session, *, now: datetime | None = None) -> str:
    """`PPC BUDGETS` — where today's budgets stand, from the last reading taken.

    Reads the stored readings rather than calling Amazon: a command has to answer at once, and the
    two-hourly `ppc_budget_pace` job has already asked.

    `now` is injectable for the same reason it is in `budget.poll` — everything here is relative to
    how much of the day has gone, and a function that reads the wall clock itself answers
    differently depending on when it is asked rather than on what is in the table.
    """
    from app.db.models import PpcHourlyMetric
    from app.ppc import budget

    try:
        profile = profiles.require(session)
    except PpcNotConnected as exc:
        return not_connected_yet() if "isn't connected yet" in str(exc) else f"📊⚠️ {exc}"

    now = now or metrics.now_for(profile)
    today = now.astimezone(metrics.zone_for(profile)).date()
    # Each campaign's own most recent reading today, not one global latest hour. Amazon's 207 can
    # fail for individual campaigns, so the newest poll may not contain all of them — and filtering
    # on a single `max(hour)` dropped every campaign missing from it, including, in the case that
    # matters, one sitting at 98% of its budget from an hour earlier.
    newest = (select(PpcHourlyMetric.amazon_campaign_id,
                     func.max(PpcHourlyMetric.hour).label("hour"))
              .where(PpcHourlyMetric.amazon_profile_id == profile.amazon_profile_id,
                     PpcHourlyMetric.ad_product == AD_PRODUCT,
                     PpcHourlyMetric.date == today,
                     PpcHourlyMetric.budget_consumed_pct.isnot(None))
              .group_by(PpcHourlyMetric.amazon_campaign_id).subquery())
    rows = session.execute(
        select(PpcHourlyMetric.amazon_campaign_id, PpcHourlyMetric.budget_consumed_pct,
               PpcHourlyMetric.hour)
        .join(newest, (PpcHourlyMetric.amazon_campaign_id == newest.c.amazon_campaign_id)
              & (PpcHourlyMetric.hour == newest.c.hour))
        .where(PpcHourlyMetric.amazon_profile_id == profile.amazon_profile_id,
               PpcHourlyMetric.ad_product == AD_PRODUCT,
               PpcHourlyMetric.date == today)
        .order_by(PpcHourlyMetric.budget_consumed_pct.desc())).all()
    if not rows:
        return ("📊 No budget readings for today yet. I take one every couple of hours — if this "
                "stays empty, advertising data collection isn't running.")

    campaigns = session.execute(
        select(PpcCampaign).where(PpcCampaign.profile_id == profile.id)).scalars().all()
    names = {c.amazon_campaign_id: (c.name or c.amazon_campaign_id) for c in campaigns}
    budgets = {c.amazon_campaign_id: c.daily_budget for c in campaigns}

    elapsed = budget.elapsed_fraction(profile, now)
    latest_hour = max(int(r.hour) for r in rows)
    clock = metrics.clock_of(latest_hour)
    lines = [f"📊 Budgets as of {clock} — {elapsed * 100:.0f}% of the day gone:", ""]
    spent_through = [r for r in rows if float(r.budget_consumed_pct) >= 95]
    for row in rows[:12]:
        used = float(row.budget_consumed_pct)
        name = names.get(row.amazon_campaign_id, row.amazon_campaign_id)[:34]
        cap = budgets.get(row.amazon_campaign_id)
        bar = "🔴" if used >= 95 else ("🟠" if used >= 80 else "·")
        # The hour is named only when it is not the newest one, so a reading carried over from
        # earlier is never presented as current.
        stamp = "" if int(row.hour) == latest_hour else f" (as of {metrics.clock_of(int(row.hour))})"
        lines.append(f"{bar} {name} — {used:.0f}% of {_money(cap, profile.currency)}{stamp}")
    if len(rows) > 12:
        lines.append(f"…and {len(rows) - 12} more.")
    # The same cutoff the alert uses, so the view and the alert never disagree about whether a
    # campaign going dark at this hour is worth mentioning.
    if spent_through and elapsed < get_settings().ppc_budget_early_fraction:
        lines.append(f"\n{len(spent_through)} already out of budget with "
                     f"{round((1 - elapsed) * 24)} hours of the day left.")
    return "\n".join(lines)


def day_shape(session: Session) -> str:
    """`PPC HOURS` — which hours sell and which only spend."""
    from app.ppc import dayparting, stream

    try:
        profile = profiles.require(session)
    except PpcNotConnected as exc:
        return not_connected_yet() if "isn't connected yet" in str(exc) else f"📊⚠️ {exc}"

    shaped = dayparting.shape(session, profile)
    if not shaped.has_data:
        return f"🕐 {stream.status(session, profile)}"

    verdicts = dayparting.judge(shaped)
    lines = [f"🕐 The shape of the day — {shaped.days} days to "
             f"{_day(shaped.to_date) if shaped.to_date else 'now'}:", "",
             dayparting.summary(shaped, verdicts)]

    busiest = sorted((b for b in shaped.by_hour if b.spend), key=lambda b: -b.spend)[:6]
    if busiest:
        lines.append("\nWhere the money goes:")
        for bucket in busiest:
            acos = _pct(bucket.acos) if bucket.acos is not None else "no sales"
            lines.append(f"· {bucket.label} — {_money(bucket.spend, profile.currency)}, {acos}")
    if shaped.coverage == "thin":
        lines.append(f"\nStill thin: {dayparting.MIN_DAYS} days is the minimum before I'll "
                     "recommend anything off this.")
    else:
        proven = [v for v in verdicts if v.proven]
        if proven:
            lines.append("\nSponsored Products has no hour-of-day setting, so acting on this means a "
                         "bid or budget schedule you'd set yourself. Any recommendation I make here "
                         "is advisory.")
    return "\n".join(lines)
