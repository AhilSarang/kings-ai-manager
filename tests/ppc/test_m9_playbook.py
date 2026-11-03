"""P4 M9: the learned playbook — where rules come from, what they do, and how they are unlearned.

The claim "it learns" is only worth anything if a rule needs evidence to exist, approval to act, and
can be taken away when it stops working. Each of those three is tested here, including the awkward
one: a rule that was right for a while and then wasn't.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import (Approval, PpcAction, PpcActionOutcome, PpcExperiment, PpcPlaybook,
                           PpcRecommendation, PpcRule)
from app.ppc import playbook, views
from app.ppc.candidates import Candidate
from app.services.common import Actor, utcnow
from tests.ppc.test_m5_analysers import MARKETPLACE, Build, account, profile  # noqa: F401


@pytest.fixture
def build(db, profile, account):
    return Build(db, profile, account)


def a_finished_experiment(build, *, conclusion="supported", change_type="bid_increase"):
    row = PpcExperiment(ad_product="sp", 
        profile_id=build.profile.id, hypothesis="Raising bids 25% on efficient exact terms wins "
        "top-of-search without wrecking ACOS", change_type=change_type,
        primary_metric="cvr", planned_days=21, spend_at_risk=120.0, status="completed",
        started_at=utcnow() - timedelta(days=40), ends_at=utcnow() - timedelta(days=19),
        minimum_clicks_per_arm=250, achieved_power=0.86, conclusion=conclusion,
        result={"settled": True,
                "comparison": {"treatment": 0.14, "control": 0.10, "p_value": 0.006,
                               "confidence_interval": [0.012, 0.068], "significant": True},
                "treatment": {"units": 8}, "control": {"units": 8},
                "power": {"for_a_meaningful_effect": 0.86}})
    build.db.add(row)
    build.db.commit()
    return row


def measured_actions(build, *, change_type, count, positive):
    """`count` settled, measured actions of one change type, `positive` of them having helped."""
    for n in range(count):
        action = PpcAction(ad_product="sp", profile_id=build.profile.id, change_type=change_type,
                           entity_type="target", amazon_entity_id=f"K{n}", before_value={},
                           after_value={}, executed_at=utcnow() - timedelta(days=40))
        build.db.add(action)
        build.db.flush()
        build.db.add(PpcActionOutcome(ad_product="sp", 
            action_id=action.id, window_days=14,
            verdict="positive" if n < positive else "negative",
            baseline_adjusted_effect={"settled": True, "headline": "x"}))
    build.db.commit()


# ---------------- where a rule may come from ----------------
def test_a_rule_cannot_exist_without_evidence(build):
    """The schema refuses it, which is the backstop behind the code refusing it."""
    from sqlalchemy.exc import IntegrityError

    build.db.add(PpcPlaybook(ad_product="sp", title="A hunch", statement="Bids should be higher",
                             created_from="outcome_analysis", evidence_action_count=5))
    with pytest.raises(IntegrityError):
        build.db.flush()
    build.db.rollback()


def test_a_successful_experiment_becomes_a_proposed_rule(build):
    experiment = a_finished_experiment(build)
    rule = playbook.propose_from_experiment(build.db, experiment)
    assert rule is not None and rule.status == "proposed"
    assert rule.created_from == "experiment" and rule.created_from_experiment_id == experiment.id
    assert rule.action_kind == "modify"
    assert experiment.ref in rule.statement and "interval" in rule.statement
    assert rule.evidence["comparison"]["p_value"] == 0.006
    assert float(rule.confidence) == pytest.approx(0.86, abs=0.01)


def test_an_experiment_that_failed_teaches_the_agent_not_to_try_it(build):
    """Learning not to propose something is the half everyone forgets."""
    experiment = a_finished_experiment(build, conclusion="not_supported")
    rule = playbook.propose_from_experiment(build.db, experiment)
    assert rule.action_kind == "suppress"
    assert "should not be proposed again" in rule.statement


def test_an_unfinished_or_unsettled_experiment_teaches_nothing(build):
    running = a_finished_experiment(build)
    running.conclusion = "inconclusive"
    build.db.commit()
    assert playbook.propose_from_experiment(build.db, running) is None

    early = a_finished_experiment(build)
    early.result = {**early.result, "settled": False}
    build.db.commit()
    assert playbook.propose_from_experiment(build.db, early) is None


def test_one_experiment_proposes_one_rule(build):
    experiment = a_finished_experiment(build)
    assert playbook.propose_from_experiment(build.db, experiment) is not None
    assert playbook.propose_from_experiment(build.db, experiment) is None


def test_thirty_measured_actions_with_a_clear_pattern_become_a_rule(build):
    measured_actions(build, change_type="add_negative_exact", count=32, positive=26)
    [rule] = playbook.propose_from_outcomes(build.db, build.profile)
    assert rule.created_from == "outcome_analysis" and rule.evidence_action_count == 32
    assert "26 helped" in rule.statement and "earns its place" in rule.statement
    assert rule.action_template["confidence_multiplier"] > 1


def test_twenty_nine_measured_actions_are_not_enough(build):
    measured_actions(build, change_type="add_negative_exact", count=29, positive=25)
    assert playbook.propose_from_outcomes(build.db, build.profile) == []


def test_thirty_actions_with_no_clear_pattern_teach_nothing(build):
    measured_actions(build, change_type="bid_decrease", count=40, positive=21)
    assert playbook.propose_from_outcomes(build.db, build.profile) == [], \
        "half and half is not a lesson"


def test_a_poor_record_becomes_a_rule_to_be_careful(build):
    measured_actions(build, change_type="bid_increase", count=30, positive=4)
    [rule] = playbook.propose_from_outcomes(build.db, build.profile)
    assert "poor record" in rule.statement
    assert rule.action_template["confidence_multiplier"] < 1


def test_inconclusive_outcomes_do_not_count_toward_the_thirty(build):
    measured_actions(build, change_type="bid_decrease", count=25, positive=22)
    for n in range(10):
        action = PpcAction(ad_product="sp", profile_id=build.profile.id, change_type="bid_decrease",
                           entity_type="target", amazon_entity_id=f"X{n}", before_value={},
                           after_value={}, executed_at=utcnow() - timedelta(days=40))
        build.db.add(action)
        build.db.flush()
        build.db.add(PpcActionOutcome(ad_product="sp", action_id=action.id, window_days=14, verdict="inconclusive",
                                      baseline_adjusted_effect={"settled": True}))
    build.db.commit()
    assert playbook.propose_from_outcomes(build.db, build.profile) == [], \
        "25 measurable outcomes is still 25"


def test_unsettled_outcomes_do_not_count_either(build):
    for n in range(35):
        action = PpcAction(ad_product="sp", profile_id=build.profile.id, change_type="bid_decrease",
                           entity_type="target", amazon_entity_id=f"K{n}", before_value={},
                           after_value={}, executed_at=utcnow() - timedelta(days=10))
        build.db.add(action)
        build.db.flush()
        build.db.add(PpcActionOutcome(ad_product="sp", action_id=action.id, window_days=14, verdict="positive",
                                      baseline_adjusted_effect={"settled": False}))
    build.db.commit()
    assert playbook.propose_from_outcomes(build.db, build.profile) == []


def test_repeated_rejections_become_a_proposed_constraint(build, owner):
    """The owner saying no three times is data; proposing it a fourth time is not listening."""
    for n, reason in enumerate(["too aggressive on a new product", "I never raise bids in Q4",
                                "not on that brand"]):
        build.db.add(PpcRecommendation(ad_product="sp", 
            profile_id=build.profile.id, change_type="bid_increase", entity_type="target",
            amazon_entity_id=f"K{n}", reason="x", status="rejected", rejected_reason=reason,
            rejected_at=utcnow() - timedelta(days=5)))
    build.db.commit()
    [rule] = playbook.propose_from_rejections(build.db, build.profile)
    assert rule.created_from == "owner_rejection_pattern" and rule.evidence_rejection_count == 3
    assert rule.action_kind == "suppress"
    assert "too aggressive on a new product" in rule.statement
    assert "I had the wrong idea about why you said no" in rule.statement


def test_two_rejections_are_not_a_pattern(build, owner):
    for n in range(2):
        build.db.add(PpcRecommendation(ad_product="sp", 
            profile_id=build.profile.id, change_type="bid_increase", entity_type="target",
            amazon_entity_id=f"K{n}", reason="x", status="rejected", rejected_reason="no",
            rejected_at=utcnow() - timedelta(days=5)))
    build.db.commit()
    assert playbook.propose_from_rejections(build.db, build.profile) == []


def test_the_same_constraint_is_not_proposed_twice(build, owner):
    for n in range(4):
        build.db.add(PpcRecommendation(ad_product="sp", 
            profile_id=build.profile.id, change_type="bid_increase", entity_type="target",
            amazon_entity_id=f"K{n}", reason="x", status="rejected", rejected_reason="no",
            rejected_at=utcnow() - timedelta(days=5)))
    build.db.commit()
    assert len(playbook.propose_from_rejections(build.db, build.profile)) == 1
    assert playbook.propose_from_rejections(build.db, build.profile) == []


# ---------------- approval, and the owner's rules winning ----------------
def test_a_rule_does_nothing_until_it_is_approved(build):
    experiment = a_finished_experiment(build)
    rule = playbook.propose_from_experiment(build.db, experiment)
    build.db.commit()
    assert playbook.active(build.db) == []
    candidate = Candidate(analyser="under_exposed", change_type="bid_increase",
                          entity_type="target", entity_id="K1", reason="x", confidence=0.6)
    kept, applied = playbook.apply_rules(build.db, [candidate])
    assert kept == [candidate] and applied.rules_fired == {}


def test_approving_a_rule_puts_it_in_force(build, owner):
    experiment = a_finished_experiment(build)
    rule = playbook.propose_from_experiment(build.db, experiment)
    approval = playbook.request_approval(build.db, rule)
    build.db.commit()
    from app.services import approvals as approvals_svc

    approvals_svc.approve(build.db, owner, approval.ref, approval.confirmation_code)
    build.db.commit()
    from tests.conftest import run_jobs

    run_jobs()
    build.db.expire_all()
    assert playbook.get_by_ref(build.db, rule.ref).status == "active"
    assert playbook.get_by_ref(build.db, rule.ref).approved_by_owner_at is not None


def test_a_learned_rule_can_never_override_amits_own(build, owner):
    """Brief 12.2. Checked here rather than left to the caller, since this is the module most
    likely to argue for something clever."""
    build.db.add(PpcRule(scope="account", blocked_change_types=["bid_increase"], active=True))
    experiment = a_finished_experiment(build, change_type="bid_increase")
    rule = playbook.propose_from_experiment(build.db, experiment)
    build.db.commit()
    assert "blocked bid increase entirely" in (playbook.conflicts_with_owner_rules(build.db, rule) or "")
    with pytest.raises(playbook.PlaybookError, match="blocked"):
        playbook.activate(build.db, rule, actor=Actor.user(owner.id))


def test_a_rule_that_only_suppresses_never_conflicts(build, owner):
    build.db.add(PpcRule(scope="account", blocked_change_types=["bid_increase"], active=True))
    experiment = a_finished_experiment(build, conclusion="not_supported",
                                       change_type="bid_increase")
    rule = playbook.propose_from_experiment(build.db, experiment)
    build.db.commit()
    assert playbook.conflicts_with_owner_rules(build.db, rule) is None
    playbook.activate(build.db, rule, actor=Actor.user(owner.id))
    assert rule.status == "active"


def test_a_rule_changed_after_approval_is_not_turned_on(build, owner):
    experiment = a_finished_experiment(build)
    rule = playbook.propose_from_experiment(build.db, experiment)
    approval = playbook.request_approval(build.db, rule)
    build.db.commit()
    rule.conditions = {**rule.conditions, "min_clicks": 9999}
    build.db.commit()
    from app.services.approvals import EXECUTORS

    with pytest.raises(playbook.PlaybookError, match="has changed"):
        EXECUTORS["ppc_activate_playbook_rule"](build.db, approval.action_payload, approval)


# ---------------- what an active rule actually does ----------------
def active_rule(build, owner, *, action_kind="suppress", change_type="bid_increase",
                conditions=None, template=None):
    rule = PpcPlaybook(ad_product="sp", 
        title=f"test rule for {change_type}", statement="s", created_from="outcome_analysis",
        evidence_action_count=40, action_kind=action_kind,
        conditions=conditions if conditions is not None else {"change_type": change_type},
        action_template=template or {"reason": "the evidence says not to"},
        status="active", approved_by_owner_at=utcnow(), confidence=0.7)
    build.db.add(rule)
    build.db.commit()
    return rule


def a_candidate(**kw):
    base = dict(analyser="under_exposed", change_type="bid_increase", entity_type="target",
                entity_id="K1", reason="x", confidence=0.6, evidence={"clicks": 50})
    base.update(kw)
    return Candidate(**base)


def test_a_suppress_rule_removes_the_candidate_and_says_which_rule_did(build, owner):
    rule = active_rule(build, owner)
    kept, applied = playbook.apply_rules(build.db, [a_candidate(), a_candidate(
        change_type="bid_decrease", entity_id="K2")])
    assert [c.change_type for c in kept] == ["bid_decrease"]
    assert applied.rules_fired == {rule.ref: 1}
    assert applied.suppressed[0]["rule"] == rule.ref
    assert "the evidence says not to" in applied.suppressed[0]["why"]


def test_a_modify_rule_changes_confidence_and_leaves_a_note(build, owner):
    rule = active_rule(build, owner, action_kind="modify",
                       template={"confidence_multiplier": 1.2, "note": "backed by X-1"})
    candidate = a_candidate()
    kept, applied = playbook.apply_rules(build.db, [candidate])
    assert kept == [candidate]
    assert candidate.confidence == pytest.approx(0.72, abs=0.001)
    assert any("backed by X-1" in n for n in candidate.notes)
    assert candidate.evidence["playbook"][0]["rule"] == rule.ref


def test_a_rule_only_fires_where_its_conditions_hold(build, owner):
    active_rule(build, owner, conditions={"change_type": "bid_increase", "min_clicks": 100})
    thin = a_candidate(evidence={"clicks": 20})
    kept, applied = playbook.apply_rules(build.db, [thin])
    assert kept == [thin] and applied.rules_fired == {}


def test_a_rule_on_probation_still_applies(build, owner):
    """Probation means being watched, not switched off — otherwise nothing would ever be re-earned."""
    rule = active_rule(build, owner)
    rule.status = "on_probation"
    rule.probation_reason = "watching it"
    build.db.commit()
    kept, applied = playbook.apply_rules(build.db, [a_candidate()])
    assert kept == [] and applied.rules_fired == {rule.ref: 1}


def test_a_retired_rule_does_nothing(build, owner):
    rule = active_rule(build, owner)
    playbook.retire(build.db, rule, actor=Actor.user(owner.id), reason="stopped working")
    build.db.commit()
    candidate = a_candidate()
    kept, applied = playbook.apply_rules(build.db, [candidate])
    assert kept == [candidate] and applied.rules_fired == {}


def test_the_analysis_run_records_which_rules_fired(build, owner):
    from app.ppc import analysers, recommendations

    rule = active_rule(build, owner, change_type="pause_target")
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=60, orders=0, spend=30.0, sales=0.0)
    build.db.commit()
    result = analysers.run_all(build.db, build.profile, today=build.today, scene=build.scene())
    assert result["counts"]["suppressed_by_playbook"] >= 1
    run = recommendations.store_run(build.db, build.profile, result)
    build.db.commit()
    assert rule.ref in run.playbook_rules_applied
    assert any(entry.get("rule") == rule.ref for entry in run.suppressed_by_rule)
    build.db.refresh(rule)
    assert rule.times_applied >= 1, "the denominator of its success rate"


# ---------------- unlearning ----------------
def shaped_outcomes(build, rule, *, count, positive):
    """Recommendations this rule shaped, executed, measured and settled."""
    for n in range(count):
        recommendation = PpcRecommendation(ad_product="sp", 
            profile_id=build.profile.id, change_type="bid_increase", entity_type="target",
            amazon_entity_id=f"K{n}", reason="x", status="executed",
            evidence={"playbook": [{"rule": rule.ref, "title": rule.title, "action": "modify"}]})
        build.db.add(recommendation)
        build.db.flush()
        action = PpcAction(ad_product="sp", profile_id=build.profile.id, recommendation_id=recommendation.id,
                           change_type="bid_increase", entity_type="target",
                           amazon_entity_id=f"K{n}", before_value={}, after_value={},
                           executed_at=utcnow() - timedelta(days=40))
        build.db.add(action)
        build.db.flush()
        build.db.add(PpcActionOutcome(ad_product="sp", 
            action_id=action.id, window_days=14,
            verdict="positive" if n < positive else "negative",
            baseline_adjusted_effect={"settled": True, "headline": "x"}))
    build.db.commit()


def test_a_rule_whose_changes_stop_helping_goes_on_probation(build, owner):
    rule = active_rule(build, owner, action_kind="modify",
                       template={"confidence_multiplier": 1.2})
    shaped_outcomes(build, rule, count=12, positive=3)
    got = playbook.review(build.db, build.profile)
    build.db.refresh(rule)
    assert rule.status == "on_probation"
    assert float(rule.success_rate) == pytest.approx(0.25, abs=0.01)
    assert "only 3 helped" in rule.probation_reason
    assert got["moved"][0]["to"] == "on_probation"


def test_a_rule_that_stays_poor_on_probation_retires(build, owner):
    rule = active_rule(build, owner, action_kind="modify",
                       template={"confidence_multiplier": 1.2})
    shaped_outcomes(build, rule, count=12, positive=3)
    playbook.review(build.db, build.profile)
    build.db.commit()
    got = playbook.review(build.db, build.profile)
    build.db.refresh(rule)
    assert rule.status == "retired" and rule.retired_at is not None
    assert "Retired rather than left" in rule.probation_reason
    assert got["moved"][0]["to"] == "retired"


def test_a_rule_that_recovers_comes_off_probation(build, owner):
    rule = active_rule(build, owner, action_kind="modify",
                       template={"confidence_multiplier": 1.2})
    rule.status = "on_probation"
    rule.probation_reason = "was poor"
    build.db.commit()
    shaped_outcomes(build, rule, count=12, positive=10)
    playbook.review(build.db, build.profile)
    build.db.refresh(rule)
    assert rule.status == "active" and rule.probation_reason is None


def test_a_rule_is_not_judged_on_too_few_measurements(build, owner):
    rule = active_rule(build, owner, action_kind="modify",
                       template={"confidence_multiplier": 1.2})
    shaped_outcomes(build, rule, count=3, positive=0)
    playbook.review(build.db, build.profile)
    build.db.refresh(rule)
    assert rule.status == "active", "three outcomes would retire good rules and keep lucky ones"
    assert rule.last_reviewed_at is not None


# ---------------- what the owner sees ----------------
def test_the_playbook_view_is_honest_when_empty(build):
    text = views.playbook_summary(build.db)
    assert "playbook is empty" in text and "at the speed the data allows" in text


def test_the_playbook_view_shows_each_rule_and_its_record(build, owner):
    rule = active_rule(build, owner, action_kind="modify",
                       template={"confidence_multiplier": 1.2})
    rule.times_applied = 20
    rule.success_rate = 0.65
    build.db.commit()
    text = views.playbook_summary(build.db)
    assert rule.ref in text and "in force" in text
    assert "applied 20 times" in text and "65% of them helped" in text


def test_the_rule_detail_shows_the_evidence_and_what_it_does(build):
    experiment = a_finished_experiment(build)
    rule = playbook.propose_from_experiment(build.db, experiment)
    build.db.commit()
    text = views.playbook_detail(build.db, rule.ref)
    assert rule.ref in text and experiment.ref in text
    assert "Where it came from" in text and "from an experiment" in text
    assert "it can modify a recommendation when" in text
    assert "does nothing until you approve it" in text


def test_the_proposal_message_shows_how_to_adopt_or_refuse(build):
    experiment = a_finished_experiment(build)
    rule = playbook.propose_from_experiment(build.db, experiment)
    approval = playbook.request_approval(build.db, rule)
    build.db.commit()
    text = views.playbook_proposal(build.db, rule.ref, approval=approval)
    assert f"APPROVE {approval.ref} {approval.confirmation_code}" in text
    assert "won't propose it again" in text


def test_the_weekly_review_job_runs_end_to_end(build, owner, monkeypatch, sent, no_quiet_hours):
    from pydantic import SecretStr

    from app.config import get_settings
    from app.jobs import queue
    from tests.conftest import run_jobs

    owner.last_inbound_at = utcnow()
    s = get_settings()
    monkeypatch.setattr(s, "ads_lwa_client_id", "c", raising=False)
    monkeypatch.setattr(s, "ads_lwa_client_secret", SecretStr("s"), raising=False)
    monkeypatch.setattr(s, "ads_lwa_refresh_token", SecretStr("r"), raising=False)
    measured_actions(build, change_type="add_negative_exact", count=32, positive=26)
    queue.enqueue(build.db, "ppc_playbook_review", {})
    build.db.commit()
    assert run_jobs(types=["ppc_playbook_review"]) == 1
    run_jobs()
    rules = build.db.execute(select(PpcPlaybook)).scalars().all()
    assert rules and rules[0].status == "proposed"
    assert any("playbook" in m.get("text", "").lower() or rules[0].ref in m.get("text", "")
               for m in sent)
