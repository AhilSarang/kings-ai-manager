"""The learned playbook: rules the agent proposes from its own evidence, and the owner approves.

This is the part people mean by "it learns". What it actually is, stated plainly:

* A rule can only be **proposed** from a completed experiment, from at least thirty measured actions
  of one change type, or from a pattern in the owner's own rejections. Never from intuition, and never
  from an article — a research note becomes an experiment first (M10), not a rule.
* Every rule has a **machine-readable** condition, so code applies it and the application is
  auditable. A rule nothing can evaluate is a sentence in a document, not a rule.
* A rule does nothing until **the owner approves it**, and the approval shows the evidence.
* An active rule is **scored against the outcomes of the recommendations it shaped**. A rule whose
  success rate falls goes on probation, then retires. Learning that cannot be unlearned is just a
  prejudice with a reference number.
* **the owner's own `ppc_rules` always win.** A learned rule may not raise a bid past his ceiling, touch a
  protected entity, or propose a change type he has blocked. That check is here, not left to the
  caller, because this is the module most likely to argue for something clever.

The three `action_kind`s are what Section 7.1 step 4 asks for: a rule may `add` a candidate the
analysers missed, `modify` one they produced, or `suppress` one. Every recommendation records the
rule that shaped it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (PpcAction, PpcActionOutcome, PpcExperiment, PpcPlaybook,
                           PpcRecommendation, PpcRule, PpcProfile)
from app.ppc.candidates import Candidate
from app.services import approvals, audit
from app.services.common import Actor, NotFound, ServiceError, utcnow

log = logging.getLogger(__name__)

MIN_ACTIONS_FOR_A_RULE = 30          # the schema enforces this too; both on purpose
MIN_REJECTIONS_FOR_A_CONSTRAINT = 3
# Below this success rate an active rule goes on probation; below it again, it retires.
PROBATION_BELOW = 0.5
MIN_APPLICATIONS_BEFORE_JUDGING = 8
# A clear enough pattern in the measured outcomes to be worth proposing as a rule.
CLEAR_PATTERN = 0.7


class PlaybookError(ServiceError):
    pass


def normalize_ref(ref: str) -> str:
    import re

    m = re.match(r"^\s*R-?\s*(\d+)\s*$", ref or "", re.I)
    if not m:
        raise NotFound(f"'{ref}' isn't a playbook reference (they look like R-4).")
    return f"R-{int(m.group(1))}"


def get_by_ref(session: Session, ref: str, *, lock: bool = False) -> PpcPlaybook:
    q = select(PpcPlaybook).where(PpcPlaybook.ref == normalize_ref(ref))
    row = session.execute(q.with_for_update() if lock else q).scalar_one_or_none()
    if row is None:
        raise NotFound(f"I can't find {normalize_ref(ref)}.")
    return row


def active(session: Session) -> list[PpcPlaybook]:
    """Rules in force. Probation still counts — a rule on probation is being watched, not switched off."""
    return list(session.execute(select(PpcPlaybook).where(
        PpcPlaybook.status.in_(("active", "on_probation"))).order_by(
        PpcPlaybook.created_at)).scalars())


def proposed(session: Session) -> list[PpcPlaybook]:
    return list(session.execute(select(PpcPlaybook).where(
        PpcPlaybook.status == "proposed").order_by(PpcPlaybook.created_at)).scalars())


# ---------------- proposing from evidence ----------------
def _exists_already(session: Session, *, title: str, change_type: str | None) -> bool:
    for row in session.execute(select(PpcPlaybook).where(
            PpcPlaybook.status.in_(("proposed", "active", "on_probation")))).scalars():
        if row.title.strip().lower() == title.strip().lower():
            return True
        if change_type and (row.conditions or {}).get("change_type") == change_type \
                and row.created_from == "owner_rejection_pattern":
            return True
    return False


def propose_from_experiment(session: Session, experiment: PpcExperiment) -> PpcPlaybook | None:
    """A finished experiment with a clear answer becomes a proposed rule.

    `not_supported` is as valuable as `supported` and produces a *suppress* rule: the agent learning
    not to propose something again is learning, and it is the half everyone forgets.
    """
    if experiment.conclusion not in ("supported", "not_supported"):
        return None
    if not (experiment.result or {}).get("settled"):
        return None
    if session.execute(select(PpcPlaybook).where(
            PpcPlaybook.created_from_experiment_id == experiment.id)).scalars().first():
        return None

    comparison = (experiment.result or {}).get("comparison") or {}
    worked = experiment.conclusion == "supported"
    change_type = experiment.change_type
    title = (f"{'Apply' if worked else 'Avoid'} {change_type.replace('_', ' ')} "
             f"where {experiment.ref} tested it")
    statement = (
        f"{experiment.ref} tested: {experiment.hypothesis} "
        f"Conversion went from {comparison.get('control', 0) * 100:.2f}% in the control to "
        f"{comparison.get('treatment', 0) * 100:.2f}% in the treated arm, interval "
        f"{(comparison.get('confidence_interval') or [0, 0])[0] * 100:+.2f} to "
        f"{(comparison.get('confidence_interval') or [0, 0])[1] * 100:+.2f} points "
        f"(p {comparison.get('p_value')}). "
        + ("So this change is worth making where the same conditions hold."
           if worked else "So this change should not be proposed again in these conditions."))
    rule = PpcPlaybook(
        ad_product=experiment.ad_product,
        title=title[:200], statement=statement,
        scope="account", conditions={"change_type": change_type,
                                     "learned_from": experiment.ref,
                                     "min_clicks": experiment.minimum_clicks_per_arm or 0},
        action_kind="modify" if worked else "suppress",
        action_template=({"confidence_multiplier": 1.2, "note": f"backed by {experiment.ref}"}
                         if worked else
                         {"reason": f"{experiment.ref} tested this and it did not work"}),
        evidence={"experiment": experiment.ref, "comparison": comparison,
                  "power": (experiment.result or {}).get("power"),
                  "units": {"treatment": (experiment.result or {}).get("treatment", {}).get("units"),
                            "control": (experiment.result or {}).get("control", {}).get("units")}},
        created_from="experiment", created_from_experiment_id=experiment.id,
        confidence=min(float(experiment.achieved_power or 0.5), 0.95), status="proposed")
    session.add(rule)
    session.flush()
    log.info("playbook rule proposed from an experiment", extra={"ref": rule.ref,
                                                                 "experiment": experiment.ref})
    return rule


def propose_from_outcomes(session: Session, profile: PpcProfile) -> list[PpcPlaybook]:
    """Thirty or more measured actions of one change type, with a clear pattern, become a rule.

    Thirty is the schema's floor and the brief's. It is not a magic number so much as the point at
    which "this keeps happening" stops being a story about the last three things that happened.
    """
    rows = session.execute(select(PpcActionOutcome, PpcAction).join(
        PpcAction, PpcActionOutcome.action_id == PpcAction.id).where(
        PpcAction.profile_id == profile.id,
        PpcActionOutcome.window_days == 14)).all()
    by_type: dict[str, list[str]] = {}
    for outcome, action in rows:
        if not (outcome.baseline_adjusted_effect or {}).get("settled"):
            continue
        if outcome.verdict == "inconclusive":
            continue
        # Keyed by ad product as well as change type. A bid decrease on a Sponsored Products
        # keyword and one on a Sponsored Display audience target are not the same change measured
        # twice; they run in different auctions, so pooling them would produce a rule learned from
        # a mixture and then applied to both.
        by_type.setdefault((action.ad_product, action.change_type), []).append(outcome.verdict)

    out = []
    for (ad_product, change_type), verdicts in sorted(by_type.items()):
        if len(verdicts) < MIN_ACTIONS_FOR_A_RULE:
            continue
        good = sum(1 for v in verdicts if v == "positive")
        bad = sum(1 for v in verdicts if v == "negative")
        share_good = good / len(verdicts)
        share_bad = bad / len(verdicts)
        if max(share_good, share_bad) < CLEAR_PATTERN:
            continue
        worked = share_good >= CLEAR_PATTERN
        title = (f"{'Lean into' if worked else 'Be wary of'} "
                 f"{change_type.replace('_', ' ')} on this account")
        if _exists_already(session, title=title, change_type=None):
            continue
        statement = (
            f"Across {len(verdicts)} measured changes of this kind, "
            f"{good} helped and {bad} made things worse "
            f"({max(share_good, share_bad) * 100:.0f}% one way). "
            + ("So this change type earns its place here and can be proposed with more confidence."
               if worked else
               "So this change type has a poor record here and should be proposed more cautiously, "
               "with the record shown."))
        rule = PpcPlaybook(
        ad_product=ad_product,
            title=title[:200], statement=statement, scope="account",
            conditions={"change_type": change_type, "min_measured_actions": len(verdicts)},
            action_kind="modify",
            action_template={"confidence_multiplier": 1.15 if worked else 0.7,
                             "note": (f"{good} of {len(verdicts)} measured changes of this kind "
                                      f"helped")},
            evidence={"measured": len(verdicts), "positive": good, "negative": bad,
                      "window_days": 14, "settled_only": True},
            created_from="outcome_analysis", evidence_action_count=len(verdicts),
            confidence=round(min(max(share_good, share_bad), 0.9), 3), status="proposed")
        session.add(rule)
        out.append(rule)
    session.flush()
    for rule in out:
        log.info("playbook rule proposed from outcomes", extra={"ref": rule.ref})
    return out


def propose_from_rejections(session: Session, profile: PpcProfile, *,
                            days: int = 60) -> list[PpcPlaybook]:
    """Repeated rejections of one change type become a proposed constraint (acceptance 10).

    The owner saying no three times is data. The agent proposing the same thing a fourth time is not
    persistence, it is not listening — and the reasons he gave are the most useful text in the system.
    """
    since = utcnow() - timedelta(days=days)
    rows = list(session.execute(select(PpcRecommendation).where(
        PpcRecommendation.profile_id == profile.id,
        PpcRecommendation.status == "rejected",
        PpcRecommendation.rejected_at >= since)).scalars())
    by_type: dict[str, list[PpcRecommendation]] = {}
    for row in rows:
        by_type.setdefault((row.ad_product, row.change_type), []).append(row)

    out = []
    for (ad_product, change_type), rejected in sorted(by_type.items()):
        if len(rejected) < MIN_REJECTIONS_FOR_A_CONSTRAINT:
            continue
        if _exists_already(session, title=f"Stop proposing {change_type.replace('_', ' ')}",
                           change_type=change_type):
            continue
        reasons = [r.rejected_reason for r in rejected if r.rejected_reason]
        rule = PpcPlaybook(
        ad_product=ad_product,
            title=f"Stop proposing {change_type.replace('_', ' ')}"[:200],
            statement=(
                f"You turned down {len(rejected)} of these in the last {days} days"
                + (f', saying things like "{reasons[0][:120]}"' if reasons else "")
                + ". Confirming this stops me proposing them; rejecting it means I had the wrong "
                  "idea about why you said no, and I'll keep offering them."),
            scope="account", conditions={"change_type": change_type},
            action_kind="suppress",
            action_template={"reason": f"you have turned down {len(rejected)} of these recently"},
            evidence={"rejections": len(rejected),
                      "refs": [r.ref for r in rejected[:10]],
                      "reasons": reasons[:5], "window_days": days},
            created_from="owner_rejection_pattern", evidence_rejection_count=len(rejected),
            confidence=0.6, status="proposed")
        session.add(rule)
        out.append(rule)
    session.flush()
    for rule in out:
        log.info("playbook constraint proposed from rejections", extra={"ref": rule.ref})
    return out


# ---------------- approval ----------------
def request_approval(session: Session, rule: PpcPlaybook, *, retire: bool = False) -> Any:
    """A rule shapes every later recommendation, so turning one on or off needs approval."""
    if not retire and rule.status != "proposed":
        raise PlaybookError(f"{rule.ref} is {rule.status}, not waiting to be turned on.")
    action_type = "ppc_retire_playbook_rule" if retire else "ppc_activate_playbook_rule"
    payload = {"rule": rule.ref, "retire": retire, "title": rule.title,
               "statement": rule.statement, "conditions": rule.conditions,
               "action_kind": rule.action_kind, "evidence": rule.evidence,
               "created_from": rule.created_from}
    approval = approvals.request_approval(
        session, action_type=action_type, payload=payload,
        summary=(f"{rule.ref}: {'retire' if retire else 'make active'} — {rule.title}"),
        risk_level="medium", requested_by_agent_key="ppc", notify=False)
    rule.approval_id = approval.id
    session.flush()
    return approval


def activate(session: Session, rule: PpcPlaybook, *, actor: Actor) -> PpcPlaybook:
    clash = conflicts_with_owner_rules(session, rule)
    if clash:
        raise PlaybookError(f"{rule.ref} can't be turned on: {clash}")
    rule.status = "active"
    rule.approved_by_owner_at = utcnow()
    rule.last_reviewed_at = utcnow()
    rule.probation_reason = None
    session.flush()
    audit.record(session, actor, "ppc_playbook_activated", target_type="ppc_playbook",
                 target_id=rule.ref, details={"title": rule.title,
                                              "created_from": rule.created_from})
    log.info("playbook rule active", extra={"ref": rule.ref})
    return rule


def retire(session: Session, rule: PpcPlaybook, *, actor: Actor,
           reason: str | None = None) -> PpcPlaybook:
    rule.status = "retired"
    rule.retired_at = utcnow()
    rule.probation_reason = reason or rule.probation_reason
    session.flush()
    audit.record(session, actor, "ppc_playbook_retired", target_type="ppc_playbook",
                 target_id=rule.ref, details={"reason": reason})
    log.info("playbook rule retired", extra={"ref": rule.ref, "reason": (reason or "")[:120]})
    return rule


def conflicts_with_owner_rules(session: Session, rule: PpcPlaybook) -> str | None:
    """Brief 12.2: learning never overrides the owner's constraints. Checked before a rule goes live."""
    change_type = (rule.conditions or {}).get("change_type")
    for owner_rule in session.execute(select(PpcRule).where(PpcRule.active.is_(True))).scalars():
        blocked = [str(b) for b in (owner_rule.blocked_change_types or [])]
        if change_type and change_type in blocked and rule.action_kind != "suppress":
            return (f"you have blocked {change_type.replace('_', ' ')} entirely, and this rule would "
                    "argue for doing it")
        template = rule.action_template or {}
        if owner_rule.max_bid is not None and template.get("max_bid") is not None:
            if float(template["max_bid"]) > float(owner_rule.max_bid):
                return (f"it would allow a bid above your {float(owner_rule.max_bid):.2f} ceiling")
        protected = {str(p).strip().lower() for p in (owner_rule.protected_entities or [])}
        scope_ref = (rule.scope_ref or "").strip().lower()
        if scope_ref and scope_ref in protected and rule.action_kind != "suppress":
            return f"{rule.scope_ref} is on your protected list"
    return None


# ---------------- applying ----------------
@dataclass
class Applied:
    """What the playbook did to one analysis run — kept so the digest can say which rule fired."""

    rules_fired: dict[str, int]
    suppressed: list[dict[str, Any]]
    modified: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {"rules_fired": self.rules_fired, "suppressed": self.suppressed,
                "modified": self.modified}


def _matches(rule: PpcPlaybook, candidate: Candidate) -> bool:
    """Whether a rule's machine-readable conditions apply to one candidate."""
    conditions = rule.conditions or {}
    wanted = conditions.get("change_type")
    if wanted and candidate.change_type != wanted:
        return False
    if rule.scope == "asin" and (rule.scope_ref or "") not in (candidate.asin or ""):
        return False
    if rule.scope == "campaign" and (rule.scope_ref or "") != (candidate.campaign_id or ""):
        return False
    floor = conditions.get("min_clicks")
    if floor:
        clicks = (candidate.evidence or {}).get("clicks")
        if clicks is not None and clicks < float(floor):
            return False
    acos_above = conditions.get("acos_above")
    if acos_above is not None:
        acos = (candidate.evidence or {}).get("acos")
        if acos is None or acos <= float(acos_above):
            return False
    return True


def apply_rules(session: Session, candidates: list[Candidate]) -> tuple[list[Candidate], Applied]:
    """Let the active rules add, modify or suppress candidates. Code does this, not the LLM.

    Every surviving candidate records the rules that touched it, so a recommendation can always
    answer "why did you propose this?" with a rule reference rather than a paraphrase.
    """
    rules = active(session)
    fired: dict[str, int] = {}
    suppressed: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    if not rules:
        return candidates, Applied(fired, suppressed, modified)

    kept: list[Candidate] = []
    for candidate in candidates:
        drop: PpcPlaybook | None = None
        for rule in rules:
            if not _matches(rule, candidate):
                continue
            fired[rule.ref] = fired.get(rule.ref, 0) + 1
            candidate.evidence.setdefault("playbook", []).append(
                {"rule": rule.ref, "title": rule.title, "action": rule.action_kind,
                 "status": rule.status})
            if rule.action_kind == "suppress":
                drop = rule
                break
            if rule.action_kind == "modify":
                template = rule.action_template or {}
                multiplier = float(template.get("confidence_multiplier") or 1.0)
                candidate.confidence = round(min(max(candidate.confidence * multiplier, 0.05), 0.95), 3)
                if template.get("note"):
                    candidate.notes.append(f"{rule.ref}: {template['note']}")
                modified.append({"rule": rule.ref, "entity": candidate.entity_id,
                                 "change_type": candidate.change_type,
                                 "confidence": candidate.confidence})
        if drop is not None:
            suppressed.append({"rule": drop.ref, "entity": drop.ref and candidate.entity_id,
                               "change_type": candidate.change_type,
                               "why": (drop.action_template or {}).get("reason", drop.title)})
            continue
        kept.append(candidate)
    return kept, Applied(fired, suppressed, modified)


def record_application(session: Session, fired: dict[str, int]) -> None:
    """Count how often each rule fired, which is the denominator of its success rate."""
    for ref, count in (fired or {}).items():
        rule = session.execute(select(PpcPlaybook).where(
            PpcPlaybook.ref == ref).with_for_update()).scalars().first()
        if rule is not None:
            rule.times_applied = int(rule.times_applied or 0) + int(count)
    session.flush()


# ---------------- the weekly review ----------------
def score(session: Session, rule: PpcPlaybook) -> dict[str, Any]:
    """How the recommendations this rule shaped actually turned out."""
    shaped = list(session.execute(select(PpcRecommendation).where(
        PpcRecommendation.evidence["playbook"].isnot(None))).scalars())
    mine = [r for r in shaped
            if any((entry or {}).get("rule") == rule.ref
                   for entry in (r.evidence or {}).get("playbook") or [])]
    executed = [r for r in mine if r.status in ("executed", "rolled_back")]
    verdicts: list[str] = []
    for recommendation in executed:
        action = session.execute(select(PpcAction).where(
            PpcAction.recommendation_id == recommendation.id)).scalars().first()
        if action is None:
            continue
        outcome = session.execute(select(PpcActionOutcome).where(
            PpcActionOutcome.action_id == action.id,
            PpcActionOutcome.window_days == 14)).scalars().first()
        if outcome is None or not (outcome.baseline_adjusted_effect or {}).get("settled"):
            continue
        if outcome.verdict != "inconclusive":
            verdicts.append(outcome.verdict)
    good = sum(1 for v in verdicts if v == "positive")
    return {"shaped": len(mine), "executed": len(executed), "measured": len(verdicts),
            "positive": good,
            "success_rate": round(good / len(verdicts), 3) if verdicts else None}


def review(session: Session, profile: PpcProfile, *, actor: Actor | None = None) -> dict[str, Any]:
    """Score every live rule and move it: active, probation, or retired.

    A rule is only judged once enough of the recommendations it shaped have been *measured* — judging
    it on two outcomes would retire good rules and keep lucky ones.
    """
    actor = actor or Actor("agent", "ppc")
    moved = []
    for rule in active(session):
        result = score(session, rule)
        rule.last_reviewed_at = utcnow()
        if result["success_rate"] is not None:
            rule.success_rate = result["success_rate"]
        if result["measured"] < MIN_APPLICATIONS_BEFORE_JUDGING:
            continue
        rate = result["success_rate"] or 0.0
        if rate < PROBATION_BELOW and rule.status == "active":
            rule.status = "on_probation"
            rule.probation_reason = (
                f"of {result['measured']} measured changes this rule shaped, only "
                f"{result['positive']} helped ({rate * 100:.0f}%). Watching it before trusting it.")
            moved.append({"rule": rule.ref, "to": "on_probation", "rate": rate})
            audit.record(session, actor, "ppc_playbook_probation", target_type="ppc_playbook",
                         target_id=rule.ref, details=result)
        elif rate < PROBATION_BELOW and rule.status == "on_probation":
            retire(session, rule, actor=actor,
                   reason=(f"still only {rate * 100:.0f}% of the changes it shaped helped, across "
                           f"{result['measured']} measured. Retired rather than left to keep "
                           "shaping recommendations."))
            moved.append({"rule": rule.ref, "to": "retired", "rate": rate})
        elif rate >= PROBATION_BELOW and rule.status == "on_probation":
            rule.status = "active"
            rule.probation_reason = None
            moved.append({"rule": rule.ref, "to": "active", "rate": rate})
            audit.record(session, actor, "ppc_playbook_reinstated", target_type="ppc_playbook",
                         target_id=rule.ref, details=result)
    session.flush()
    return {"reviewed": len(active(session)) + len([m for m in moved if m["to"] == "retired"]),
            "moved": moved}


def propose_all(session: Session, profile: PpcProfile) -> list[PpcPlaybook]:
    """Everything the evidence currently supports proposing. Run weekly."""
    out: list[PpcPlaybook] = []
    for experiment in session.execute(select(PpcExperiment).where(
            PpcExperiment.profile_id == profile.id,
            PpcExperiment.conclusion.in_(("supported", "not_supported")))).scalars():
        rule = propose_from_experiment(session, experiment)
        if rule is not None:
            out.append(rule)
    out.extend(propose_from_outcomes(session, profile))
    out.extend(propose_from_rejections(session, profile))
    return out


# ---------------- the approval executors ----------------
@approvals.executor("ppc_activate_playbook_rule")
def _run_activate(session: Session, payload: dict[str, Any], approval) -> dict[str, Any]:
    rule = get_by_ref(session, payload["rule"], lock=True)
    if (rule.conditions or {}) != (payload.get("conditions") or {}):
        raise PlaybookError(f"{rule.ref} has changed since you approved it; it was not turned on.")
    activate(session, rule, actor=Actor("agent", "ppc"))
    return {"summary": (f"{rule.ref} is now part of the playbook: {rule.title}. It will shape every "
                        "recommendation from the next analysis on, and I'll score it against what "
                        "actually happens."), "complete_task": True}


@approvals.executor("ppc_retire_playbook_rule")
def _run_retire(session: Session, payload: dict[str, Any], approval) -> dict[str, Any]:
    rule = get_by_ref(session, payload["rule"], lock=True)
    retire(session, rule, actor=Actor("agent", "ppc"), reason="you retired it")
    return {"summary": f"{rule.ref} retired. It won't shape recommendations any more.",
            "complete_task": True}
