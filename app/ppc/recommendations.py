"""Turning candidates into numbered recommendations, and asking the owner about them.

M5 produces candidates; this gives them refs (`P-14`), groups them into a batch (`B-7`), and asks for
one approval covering the batch. The brief's approval shapes (7.3) all work off that one code:

    APPROVE BATCH B-7 9F2A          the whole digest
    APPROVE P-14,P-15,P-22 9F2A     a subset
    REJECT P-16 too aggressive      with the reason, which is learning data (9.4)

The approval's payload is frozen at the moment it is created and holds every proposed value, so what
The owner approves is exactly what was shown to him — a later analysis run cannot quietly change the
numbers under an open approval. Approving a subset does not alter that payload; it records a decision
against each recommendation.

Nothing here writes to Amazon. Execution is `app/ppc/apply.py`, and it only ever runs from an
approval.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (PpcAdGroup, PpcAnalysisRun, PpcCampaign, PpcProfile, PpcRecommendation,
                           PpcRule, PpcTarget, User)
from app.ppc import analysers, apply as _apply_module, economics  # noqa: F401
from app.ppc.candidates import Candidate

# Imported for its side effect: it registers the `ppc_apply_change` executor, and an approval cannot
# be created without one. Phase 3 wires its executors the same way.
_ = _apply_module
from app.services import approvals, audit, settings_store
from app.services.common import Actor, NotFound, ServiceError, utcnow

log = logging.getLogger(__name__)

REF_RE = re.compile(r"^\s*P-?\s*(\d+)\s*$", re.I)
BATCH_RE = re.compile(r"^\s*B-?\s*(\d+)\s*$", re.I)
DEFAULT_TTL = timedelta(days=2)

# A budget rise above this needs the PIN, not just the code (brief 7.3).
PIN_BUDGET_INCREASE = 50.0


class PpcError(ServiceError):
    pass


def normalize_ref(ref: str) -> str:
    m = REF_RE.match(ref or "")
    if not m:
        raise NotFound(f"'{ref}' isn't a recommendation reference (they look like P-14).")
    return f"P-{int(m.group(1))}"


def normalize_batch(ref: str) -> str:
    m = BATCH_RE.match(ref or "")
    if not m:
        raise NotFound(f"'{ref}' isn't a batch reference (they look like B-7).")
    return f"B-{int(m.group(1))}"


def get_by_ref(session: Session, ref: str, *, lock: bool = False) -> PpcRecommendation:
    q = select(PpcRecommendation).where(PpcRecommendation.ref == normalize_ref(ref))
    row = session.execute(q.with_for_update() if lock else q).scalar_one_or_none()
    if row is None:
        raise NotFound(f"I can't find {normalize_ref(ref)}.")
    return row


def batch_by_ref(session: Session, ref: str) -> PpcAnalysisRun:
    row = session.execute(select(PpcAnalysisRun).where(
        PpcAnalysisRun.ref == normalize_batch(ref))).scalar_one_or_none()
    if row is None:
        raise NotFound(f"I can't find batch {normalize_batch(ref)}.")
    return row


def in_batch(session: Session, run: PpcAnalysisRun,
             statuses: tuple[str, ...] | None = None) -> list[PpcRecommendation]:
    q = select(PpcRecommendation).where(PpcRecommendation.batch_id == run.id)
    if statuses:
        q = q.where(PpcRecommendation.status.in_(statuses))
    return list(session.execute(q.order_by(PpcRecommendation.created_at,
                                           PpcRecommendation.ref)).scalars())


# ---------------- storing a run ----------------
def _our_entity_id(session: Session, profile: PpcProfile, candidate: Candidate):
    """Our uuid for the thing being changed, where there is one. Amazon's id is stored separately."""
    if candidate.entity_type == "target":
        row = session.execute(select(PpcTarget).where(
            PpcTarget.profile_id == profile.id,
            PpcTarget.amazon_target_id == candidate.entity_id)).scalars().first()
        return row.id if row else None
    if candidate.entity_type == "campaign":
        row = session.execute(select(PpcCampaign).where(
            PpcCampaign.profile_id == profile.id,
            PpcCampaign.amazon_campaign_id == candidate.entity_id)).scalars().first()
        return row.id if row else None
    if candidate.entity_type == "ad_group":
        row = session.execute(select(PpcAdGroup).where(
            PpcAdGroup.profile_id == profile.id,
            PpcAdGroup.amazon_ad_group_id == candidate.entity_id)).scalars().first()
        return row.id if row else None
    return None


def _as_value(value: Any) -> dict[str, Any] | None:
    """The columns are JSONB, so a bare bid or state is wrapped rather than coerced to text."""
    if value is None:
        return None
    return value if isinstance(value, dict) else {"value": value}


def _protected(session: Session, candidate: Candidate) -> str | None:
    """The owner's protected entities are never proposed for change (Section 5).

    A protected campaign or keyword is one he has decided about; an analyser noticing it is cheaper
    than him having to notice the agent noticing it.
    """
    names = set()
    for rule in session.execute(select(PpcRule).where(PpcRule.active.is_(True))).scalars():
        for entry in rule.protected_entities or []:
            if isinstance(entry, str):
                names.add(entry.strip().lower())
            elif isinstance(entry, dict) and entry.get("id"):
                names.add(str(entry["id"]).strip().lower())
    if not names:
        return None
    for value in (candidate.entity_id, candidate.label, candidate.campaign_id,
                  candidate.ad_group_id, candidate.asin):
        if value and str(value).strip().lower() in names:
            return str(value)
    return None


def _tacos_impact(session: Session, profile: PpcProfile, candidate: Candidate) -> dict[str, Any]:
    """What this change would do to TACOS, which is the number the business runs on (6.7, 7.2)."""
    from app.ppc import tacos

    account = tacos.for_account(session, profile, days=30)
    if not account.total_sales:
        return {"note": "no total sales for the window yet, so TACOS impact can't be estimated"}
    change = candidate.monthly_effect
    direction = "down" if candidate.change_type.endswith("_decrease") or \
        candidate.change_type in ("pause_target", "add_negative_exact", "add_negative_phrase") else "up"
    spend_change = -abs(change) if direction == "down" else abs(candidate.spend_at_risk)
    new_tacos = (account.ad_spend + spend_change) / account.total_sales
    return {"tacos_now": account.tacos, "tacos_after_estimate": round(max(new_tacos, 0), 4),
            "monthly_spend_change": round(spend_change, 2),
            "basis": f"account TACOS over {account.start} to {account.end}"}


def _effect_range(candidate: Candidate) -> dict[str, Any]:
    """A predicted effect with a range, as 7.2 requires — a point estimate pretends to know more.

    The range is deliberately wide and derived from confidence rather than from a model: it says
    "this is the size of thing we expect", which is the honest claim, instead of a figure to two
    decimal places that nothing could justify.
    """
    base = candidate.predicted_effect or {}
    size = abs(candidate.monthly_effect)
    spread = max(0.4, 1.0 - candidate.confidence)
    return {**base, "monthly_effect_estimate": round(candidate.monthly_effect, 2),
            "monthly_effect_low": round(candidate.monthly_effect - size * spread, 2),
            "monthly_effect_high": round(candidate.monthly_effect + size * spread, 2),
            "confidence": candidate.confidence,
            "basis": "scaled from the measured window; the range widens as confidence falls"}


def store_run(session: Session, profile: PpcProfile, result: dict[str, Any], *,
              cap: int | None = None) -> PpcAnalysisRun:
    """Write one analysis cycle and its recommendations. Returns the batch."""
    thresholds = analysers.candidates.Thresholds.load(session)
    cap = cap or thresholds.daily_cap
    run = PpcAnalysisRun(
        profile_id=profile.id, ad_product=result.get("ad_product"),
        data_window_start=result.get("window_start"),
        data_window_end=result.get("window_end"),
        analysers_run=sorted(result.get("per_analyser", {})),
        suppressed_counts={"held": len(result.get("held") or []),
                           "in_cooldown": len(result.get("in_cooldown") or [])},
        candidates_considered=sum(max(n, 0) for n in (result.get("per_analyser") or {}).values()))
    session.add(run)
    session.flush()

    kept: list[PpcRecommendation] = []
    protected_skipped = []
    for candidate in result.get("candidates") or []:
        if len(kept) >= cap:
            break
        blocked = _protected(session, candidate)
        if blocked:
            protected_skipped.append({"entity": blocked, "change_type": candidate.change_type})
            continue
        unverified = _target_unverified(session, profile, candidate)
        if not candidate.ad_product:
            # `analysers.run_all` stamps this from the scene. An unstamped candidate means something
            # produced one outside that path, and guessing "sp" here is how a Sponsored Display
            # recommendation would end up sent to a Sponsored Products endpoint.
            raise ValueError(f"{candidate.analyser} produced a candidate with no ad product")
        row = PpcRecommendation(
            batch_id=run.id, profile_id=profile.id, ad_product=candidate.ad_product,
            change_type=candidate.change_type,
            entity_type=candidate.entity_type,
            entity_id=_our_entity_id(session, profile, candidate),
            amazon_entity_id=str(candidate.entity_id)[:40], entity_label=candidate.label,
            asin=candidate.asin, current_value=_as_value(candidate.current_value),
            proposed_value=_as_value(candidate.proposed_value), reason=candidate.reason,
            evidence={**candidate.evidence, "analyser": candidate.analyser,
                      "tacos_impact": _tacos_impact(session, profile, candidate),
                      "notes": candidate.notes},
            analyser=candidate.analyser, predicted_effect=_effect_range(candidate),
            playbook_rule_id=_rule_id(session, candidate),
            confidence=candidate.confidence, risk=candidate.risk,
            spend_at_risk=round(candidate.spend_at_risk, 2),
            target_acos_unverified=unverified, status="proposed",
            expires_at=utcnow() + DEFAULT_TTL)
        session.add(row)
        kept.append(row)

    applied = result.get("playbook") or {}
    run.playbook_rules_applied = sorted((applied.get("rules_fired") or {}).keys())
    run.suppressed_by_rule = protected_skipped + list(applied.get("suppressed") or [])
    run.recommendations_made = len(kept)
    run.finished_at = utcnow()
    session.flush()
    # Counted once per run, not once per rule match, so a rule's success rate has an honest
    # denominator.
    from app.ppc import playbook

    playbook.record_application(session, applied.get("rules_fired") or {})
    log.info("ppc analysis run stored", extra={"batch": run.ref, "made": len(kept),
                                               "considered": run.candidates_considered})
    return run


def _rule_id(session: Session, candidate: Candidate):
    """The playbook rule that shaped this candidate, where one did (brief 7.1 step 4)."""
    from app.db.models import PpcPlaybook

    entries = (candidate.evidence or {}).get("playbook") or []
    for entry in entries:
        ref = (entry or {}).get("rule")
        if not ref:
            continue
        rule = session.execute(select(PpcPlaybook).where(PpcPlaybook.ref == ref)).scalars().first()
        if rule is not None:
            return rule.id
    return None


def _target_unverified(session: Session, profile: PpcProfile, candidate: Candidate) -> bool:
    """Brief 6.6: an ASIN we cannot price properly is flagged on every recommendation touching it."""
    asins = [candidate.asin] if candidate.asin else []
    if not asins and candidate.ad_group_id:
        from app.ppc import aggregates

        asins = aggregates.asin_for_ad_group(session, profile,
                                        ad_product=candidate.ad_product).get(
                                            candidate.ad_group_id, [])
    for asin in [a for a in asins if a]:
        value, _source = economics.target_for(session, asin, profile.marketplace_id)
        if value is None:
            return True
    return False


# ---------------- the digest ----------------
CHANGE_WORDS = {
    "bid_increase": "bid increase", "bid_decrease": "bid cut", "pause_target": "pause",
    "enable_target": "re-enable", "add_negative_exact": "negative keyword",
    "add_negative_phrase": "negative phrase", "harvest_search_term": "harvest",
    "budget_increase": "budget increase", "budget_decrease": "budget cut",
    "placement_adjustment": "placement adjustment", "campaign_structure": "structure change",
    "dayparting": "dayparting", "other": "other",
}


def _money(value: float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "CAD": "C$", "GBP": "£", "EUR": "€"}.get(currency, "")
    return f"{symbol}{abs(float(value)):,.0f}"


def digest(session: Session, run: PpcAnalysisRun, *, approval=None) -> str:
    """The grouped summary the owner actually reads (7.3). One line per kind, then how to approve."""
    profile = session.get(PpcProfile, run.profile_id)
    currency = profile.currency if profile else "USD"
    rows = in_batch(session, run, ("proposed",))
    held = (run.suppressed_counts or {}).get("held") or 0
    cooling = (run.suppressed_counts or {}).get("in_cooldown") or 0
    if not rows:
        # "Nothing worth changing" and "everything I found was held back" are different answers, and
        # the second one is the interesting one.
        if held or cooling:
            lines = [f"📊 {run.ref}: nothing to approve."]
            if held:
                lines.append(f"{held} held back because the product can't take more spend right "
                             "now — out of stock, low, not buyable, or losing the Buy Box.")
            if cooling:
                lines.append(f"{cooling} left alone to settle after a recent change.")
            lines.append(f"`{run.ref} DETAILS` has the rest.")
            return "\n".join(lines)
        return (f"📊 {run.ref}: the analysis ran and found nothing worth changing. "
                f"{run.candidates_considered} things were looked at.")

    groups: dict[str, list[PpcRecommendation]] = {}
    for row in rows:
        groups.setdefault(row.change_type, []).append(row)

    saving = sum(float(r.predicted_effect.get("monthly_effect_estimate") or 0)
                 for r in rows if r.change_type.endswith("_decrease")
                 or r.change_type in ("pause_target", "add_negative_exact", "add_negative_phrase"))
    gain = sum(float(r.predicted_effect.get("monthly_effect_estimate") or 0)
               for r in rows if r.change_type in ("bid_increase", "budget_increase",
                                                  "harvest_search_term"))
    lines = [f"📊 *{run.ref} — {len(rows)} changes to approve*"]
    headline = []
    if saving:
        headline.append(f"about {_money(saving, currency)}/month saved")
    if gain:
        headline.append(f"about {_money(gain, currency)}/month of sales to chase")
    if headline:
        lines.append("_" + ", ".join(headline) + "_")

    for change_type, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        worth = sum(float(r.predicted_effect.get("monthly_effect_estimate") or 0) for r in group)
        money = f", {_money(worth, currency)}/month" if abs(worth) >= 1 else ""
        refs = ", ".join(r.ref for r in group[:6]) + ("…" if len(group) > 6 else "")
        label = CHANGE_WORDS.get(change_type, change_type)
        plural = "s" if len(group) != 1 else ""
        lines.append(f"• *{len(group)} {label}{plural}*{money} — {refs}")

    risky = [r for r in rows if r.risk in ("high", "critical")]
    if risky:
        lines.append(f"\n⚠️ {len(risky)} of these are higher risk: "
                     + ", ".join(r.ref for r in risky[:5]))
    unverified = [r for r in rows if r.target_acos_unverified]
    if unverified:
        lines.append(f"⚠️ {len(unverified)} touch products whose target ACOS I can't verify "
                     f"({', '.join(r.ref for r in unverified[:4])}) — usually a missing unit cost.")
    if held or cooling:
        lines.append(f"\nAlso: {held} held back because the product can't take more spend, "
                     f"{cooling} left alone to settle after a recent change.")

    lines.append(f"\n`{run.ref} DETAILS` for the numbers behind each one.")
    if approval is not None:
        pin = " <PIN>" if approval.risk_level == "critical" else ""
        lines.append(f"\n*To approve everything:* `APPROVE BATCH {run.ref} "
                     f"{approval.confirmation_code}{pin}`")
        lines.append(f"*Some of them:* `APPROVE P-14,P-15 {approval.confirmation_code}{pin}`")
        lines.append(f"*To turn one down:* `REJECT P-16 why` — the reason teaches me something.")
    return "\n".join(lines)


def details(session: Session, ref: str) -> str:
    """`P-14 DETAILS` — every number behind one recommendation (7.2)."""
    row = get_by_ref(session, ref)
    evidence = row.evidence or {}
    lines = [f"📊 *{row.ref} — {CHANGE_WORDS.get(row.change_type, row.change_type)}*",
             f"_{row.entity_label or row.amazon_entity_id}_"]
    if row.current_value or row.proposed_value:
        now = (row.current_value or {}).get("value", row.current_value)
        want = (row.proposed_value or {}).get("value", row.proposed_value)
        lines.append(f"\n{now} → {want}")
    lines.append(f"\n{row.reason}")

    window = f"{evidence.get('from')} to {evidence.get('to')}" if evidence.get("from") else None
    numbers = [f"clicks {evidence.get('clicks')}" if evidence.get("clicks") is not None else None,
               f"orders {evidence.get('orders')}" if evidence.get("orders") is not None else None,
               f"spend {evidence.get('spend')}" if evidence.get("spend") is not None else None,
               f"sales {evidence.get('sales')}" if evidence.get("sales") is not None else None,
               f"ACOS {evidence['acos'] * 100:.0f}%" if evidence.get("acos") else None,
               f"CVR {evidence['cvr'] * 100:.1f}%" if evidence.get("cvr") else None,
               f"CPC {evidence['cpc']:.2f}" if evidence.get("cpc") else None]
    shown = [n for n in numbers if n]
    if shown:
        lines.append("\n*The numbers:* " + " · ".join(shown))
    if window:
        lines.append(f"Measured over {window}" +
                     (f" ({evidence.get('days_with_data')} days with data)"
                      if evidence.get("days_with_data") else ""))
    if evidence.get("target_acos"):
        lines.append(f"Target ACOS {evidence['target_acos'] * 100:.0f}% "
                     f"({evidence.get('target_acos_source', 'derived')})")
    effect = row.predicted_effect or {}
    if effect.get("monthly_effect_estimate"):
        lines.append(f"\n*Expected:* {effect['monthly_effect_estimate']:+.2f}/month, somewhere "
                     f"between {effect.get('monthly_effect_low')} and "
                     f"{effect.get('monthly_effect_high')}")
    tacos_impact = evidence.get("tacos_impact") or {}
    if tacos_impact.get("tacos_now") is not None:
        lines.append(f"TACOS now {tacos_impact['tacos_now'] * 100:.1f}%, after this about "
                     f"{(tacos_impact.get('tacos_after_estimate') or 0) * 100:.1f}%")
    elif tacos_impact.get("note"):
        lines.append(f"TACOS impact: {tacos_impact['note']}")
    lines.append(f"\nRisk {row.risk} · confidence {float(row.confidence or 0) * 100:.0f}% · "
                 f"found by the {row.analyser} analyser")
    if row.spend_at_risk:
        lines.append(f"Spend at risk: {float(row.spend_at_risk):.2f}")
    if row.target_acos_unverified:
        lines.append("⚠️ This product's target ACOS isn't verified — usually a missing unit cost.")
    for note in evidence.get("notes") or []:
        lines.append(f"· {note}")
    lines.append(f"\nStatus: {row.status}")
    return "\n".join(lines)


def batch_details(session: Session, ref: str) -> str:
    """`B-7 DETAILS` — one line per recommendation, with its ref so it can be approved or refused."""
    run = batch_by_ref(session, ref)
    rows = in_batch(session, run)
    if not rows:
        return f"📊 {run.ref} has no recommendations in it."
    profile = session.get(PpcProfile, run.profile_id)
    currency = profile.currency if profile else "USD"
    window = (f"{run.data_window_start} to {run.data_window_end}"
              if run.data_window_start else "the recent window")
    lines = [f"📊 *{run.ref}* — {len(rows)} recommendations, from {window}"]
    for row in rows:
        now = (row.current_value or {}).get("value", row.current_value)
        want = (row.proposed_value or {}).get("value", row.proposed_value)
        if isinstance(want, dict):
            want = want.get("expression") or want.get("bid") or "see details"
        worth = float((row.predicted_effect or {}).get("monthly_effect_estimate") or 0)
        money = f" · {_money(worth, currency)}/mo" if abs(worth) >= 1 else ""
        mark = {"proposed": "•", "approved": "👍", "executed": "✅", "rejected": "🚫",
                "expired": "⌛", "failed": "⛔", "rolled_back": "↩️"}.get(row.status, "•")
        change = CHANGE_WORDS.get(row.change_type, row.change_type)
        lines.append(f"{mark} *{row.ref}* {change} — {row.entity_label or row.amazon_entity_id}"
                     + (f" ({now} → {want})" if now is not None and want is not None else "")
                     + money)
    lines.append("\n`P-14 DETAILS` for the numbers behind any one of them.")
    return "\n".join(lines)


# ---------------- approval ----------------
def _needs_pin(session: Session, rows: list[PpcRecommendation]) -> tuple[bool, str | None]:
    """Which of 7.3's PIN conditions this batch trips, if any."""
    threshold = float(settings_store.get(session, "ppc_pin_budget_increase_usd",
                                         PIN_BUDGET_INCREASE) or PIN_BUDGET_INCREASE)
    for row in rows:
        if row.change_type == "budget_increase":
            now = float((row.current_value or {}).get("value") or 0)
            want = float((row.proposed_value or {}).get("value") or 0)
            if want - now >= threshold:
                return True, f"{row.ref} raises a daily budget by {want - now:.2f}"
        if row.change_type == "campaign_structure":
            return True, f"{row.ref} changes how the account is laid out"
    total_increase = sum(
        float((r.proposed_value or {}).get("value") or 0) - float((r.current_value or {}).get("value") or 0)
        for r in rows if r.change_type == "budget_increase")
    cap = session.execute(select(PpcRule).where(
        PpcRule.active.is_(True), PpcRule.max_daily_account_spend.is_not(None))).scalars().first()
    if cap is not None and total_increase and total_increase >= float(cap.max_daily_account_spend):
        return True, (f"the batch adds {total_increase:.2f} a day, at or above your "
                      f"{float(cap.max_daily_account_spend):.2f} account cap")
    return False, None


def request_batch_approval(session: Session, run: PpcAnalysisRun, *,
                           actor_key: str = "ppc") -> Any:
    """One approval for the batch, with every proposed value frozen into its payload."""
    rows = in_batch(session, run, ("proposed",))
    if not rows:
        raise PpcError(f"{run.ref} has nothing to approve.")
    needs_pin, why = _needs_pin(session, rows)
    risk = "critical" if needs_pin else (
        "high" if any(r.risk == "high" for r in rows) else "medium")
    payload = {
        "batch": run.ref,
        "profile_id": str(run.profile_id),
        "recommendations": [
            {"ref": r.ref, "change_type": r.change_type, "entity_type": r.entity_type,
             "amazon_entity_id": r.amazon_entity_id, "label": r.entity_label,
             "current_value": r.current_value, "proposed_value": r.proposed_value,
             "risk": r.risk} for r in rows],
        "pin_reason": why,
    }
    approval = approvals.request_approval(
        session, action_type="ppc_apply_change", payload=payload,
        summary=(f"{run.ref}: {len(rows)} advertising changes"
                 + (f" — {why}" if why else "")),
        risk_level=risk, requested_by_agent_key=actor_key, ttl=DEFAULT_TTL, notify=False)
    run.approval_id = approval.id
    for row in rows:
        row.approval_id = approval.id
    session.flush()
    return approval


def decide(session: Session, user: User, *, batch_ref: str | None = None,
           refs: list[str] | None = None, code: str | None = None,
           pin: str | None = None) -> dict[str, Any]:
    """Approve a whole batch or a subset of it. The approval is consumed either way.

    A subset does not rewrite the frozen payload: the payload is what was shown, and the decision is
    recorded per recommendation. Anything not chosen stays proposed and expires with the batch, which
    is how `ppc_predictions` later tells "he said no" from "he never said".
    """
    if user.role != "owner":
        raise PpcError("Only an owner can approve advertising changes.")
    run = batch_by_ref(session, batch_ref) if batch_ref else _run_for_refs(session, refs or [])
    rows = in_batch(session, run, ("proposed",))
    if not rows:
        raise PpcError(f"{run.ref} has nothing left to approve.")
    chosen = rows if batch_ref else [r for r in rows if r.ref in {normalize_ref(x) for x in refs or []}]
    missing = ({normalize_ref(x) for x in refs or []} - {r.ref for r in rows}) if refs else set()
    if refs and not chosen:
        raise PpcError(f"None of those are waiting in {run.ref}"
                       + (f" ({', '.join(sorted(missing))} aren't)." if missing else "."))

    # The code is checked first. A wrong code commits inside `approvals` to keep its bad-attempt
    # counter, so anything set before that would be committed with it — the statuses have to come
    # after. They still land in the same transaction as the enqueued executor, which only becomes
    # visible to a worker when the caller commits, so the subset can never race the job.
    actor = Actor.user(user.id)
    approval = approvals.get_by_ref(session, _approval_ref(session, run), lock=True)
    approvals.approve(session, user, approval.ref, code, pin=pin)

    for row in chosen:
        row.status = "approved"
    for row in rows:
        if row not in chosen:
            row.status = "expired"
            row.rejected_reason = "not chosen when the batch was approved"
    session.flush()
    audit.record(session, actor, "ppc_batch_approved", target_type="ppc_batch", target_id=run.ref,
                 details={"approved": [r.ref for r in chosen],
                          "not_chosen": [r.ref for r in rows if r not in chosen]})
    return {"batch": run.ref, "approved": [r.ref for r in chosen],
            "skipped": [r.ref for r in rows if r not in chosen], "unknown": sorted(missing),
            "approval": approval.ref}


def _run_for_refs(session: Session, refs: list[str]) -> PpcAnalysisRun:
    if not refs:
        raise PpcError("Tell me which recommendations to approve, or approve the batch.")
    first = get_by_ref(session, refs[0])
    run = session.get(PpcAnalysisRun, first.batch_id)
    if run is None:
        raise PpcError(f"{first.ref} isn't part of a batch I can approve.")
    for ref in refs[1:]:
        other = get_by_ref(session, ref)
        if other.batch_id != run.id:
            raise PpcError(f"{normalize_ref(ref)} is in a different batch from {first.ref}. "
                           "Approve one batch at a time.")
    return run


def _approval_ref(session: Session, run: PpcAnalysisRun) -> str:
    from app.db.models import Approval

    approval = session.get(Approval, run.approval_id) if run.approval_id else None
    if approval is None:
        raise PpcError(f"{run.ref} has no open approval. Ask for the digest again.")
    return approval.ref


def reject(session: Session, user: User, ref: str, reason: str | None = None) -> PpcRecommendation:
    """Turn one down, with the reason — which is the most useful thing in the learning data (9.4)."""
    row = get_by_ref(session, ref, lock=True)
    if row.status not in ("proposed", "approved"):
        raise PpcError(f"{row.ref} is already {row.status}.")
    row.status = "rejected"
    row.rejected_reason = (reason or "").strip() or None
    row.rejected_at = utcnow()
    session.flush()
    audit.record(session, Actor.user(user.id), "ppc_recommendation_rejected",
                 target_type="ppc_recommendation", target_id=row.ref,
                 details={"reason": row.rejected_reason, "change_type": row.change_type})
    return row


def waiting(session: Session, profile: PpcProfile | None = None) -> list[PpcRecommendation]:
    q = select(PpcRecommendation).where(PpcRecommendation.status == "proposed")
    if profile is not None:
        q = q.where(PpcRecommendation.profile_id == profile.id)
    return list(session.execute(q.order_by(PpcRecommendation.created_at)).scalars())


def expire_due(session: Session, *, now=None) -> list[PpcRecommendation]:
    """Recommendations nobody answered. Left as a record, not deleted."""
    now = now or utcnow()
    due = list(session.execute(select(PpcRecommendation).where(
        PpcRecommendation.status == "proposed",
        PpcRecommendation.expires_at.is_not(None),
        PpcRecommendation.expires_at <= now)).scalars())
    for row in due:
        row.status = "expired"
    session.flush()
    return due
