"""P4 M8: the experiment engine — statistics, design, assignment, guardrails, verdicts.

Two things are under test. The statistics, against values worked by hand, because "significant" has
to mean something. And the engine's willingness to refuse: an experiment that cannot answer its own
question should not be run, and one that ran across Prime Day should not be believed.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (Approval, PpcAction, PpcCalendarEvent, PpcExperiment, PpcExperimentUnit,
                           PpcMetricsDaily, PpcTarget)
from app.ppc import experiments, metrics, statistics, views
from app.ppc.fake import FakeAdsClient
from app.services import settings_store
from app.services.common import SYSTEM, Actor, utcnow
from tests.ppc.test_m5_analysers import MARKETPLACE, Build, account, profile  # noqa: F401
from tests.ppc.test_m7_outcomes import day_rows


@pytest.fixture
def build(db, profile, account):
    return Build(db, profile, account)


@pytest.fixture(autouse=True)
def live(monkeypatch, db, profile):
    from pydantic import SecretStr

    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "ads_backend", "fake", raising=False)
    monkeypatch.setattr(s, "ads_lwa_client_id", "cid", raising=False)
    monkeypatch.setattr(s, "ads_lwa_client_secret", SecretStr("secret"), raising=False)
    monkeypatch.setattr(s, "ads_lwa_refresh_token", SecretStr("Atzr|x"), raising=False)
    settings_store.set_value(db, "ppc_write_mode", "live", SYSTEM)
    profile.write_mode = "live"
    db.commit()


# ---------------- the statistics, against hand-worked values ----------------
def test_the_normal_distribution_matches_the_tables():
    assert statistics.normal_cdf(0) == pytest.approx(0.5)
    assert statistics.normal_cdf(1.96) == pytest.approx(0.9750, abs=0.0001)
    assert statistics.normal_cdf(-1.96) == pytest.approx(0.0250, abs=0.0001)
    assert statistics.normal_quantile(0.975) == pytest.approx(1.9600, abs=0.0001)
    assert statistics.normal_quantile(0.80) == pytest.approx(0.8416, abs=0.0001)


def test_students_t_matches_the_tables():
    assert statistics.t_cdf(2.228, 10) == pytest.approx(0.975, abs=0.0005)
    assert statistics.t_quantile(0.975, 10) == pytest.approx(2.228, abs=0.001)
    assert statistics.t_quantile(0.975, 30) == pytest.approx(2.042, abs=0.002)
    # With many degrees of freedom the t converges on the normal.
    assert statistics.t_quantile(0.975, 10000) == pytest.approx(1.960, abs=0.002)


def test_a_two_proportion_test_against_arithmetic_done_by_hand():
    """0.14 against 0.10 at n=1000: pooled 0.12, SE 0.014533, z 2.752, two-sided p 0.0059."""
    got = statistics.two_proportions(140, 1000, 100, 1000)
    assert got.treatment == pytest.approx(0.14) and got.control == pytest.approx(0.10)
    assert got.difference == pytest.approx(0.04)
    assert got.p_value == pytest.approx(0.0059, abs=0.0005)
    assert got.ci_low == pytest.approx(0.0116, abs=0.001)
    assert got.ci_high == pytest.approx(0.0684, abs=0.001)
    assert got.significant is True


def test_a_difference_that_could_be_luck_is_not_significant():
    got = statistics.two_proportions(52, 500, 48, 500)
    assert got.p_value > 0.05 and got.significant is False
    assert got.ci_low < 0 < got.ci_high, "the interval includes no effect"


def test_sample_size_and_detectable_effect_agree_with_each_other():
    needed = statistics.required_per_arm(0.10, 0.20)
    assert needed == pytest.approx(3841, rel=0.02)
    # At that many clicks, a 20% change should be right at the edge of detectable.
    assert statistics.detectable_effect(0.10, needed) == pytest.approx(0.20, abs=0.02)
    assert statistics.power_two_proportions(0.10, 0.12, needed) == pytest.approx(0.80, abs=0.03)


def test_too_little_traffic_can_only_see_an_implausible_effect():
    """50 clicks can only detect a change so large it would not need an experiment."""
    got = statistics.detectable_effect(0.10, 50)
    assert got is not None and got > experiments.WORTHWHILE_EFFECT
    # Which is exactly why the design refuses: see the refusal test below.
    assert statistics.detectable_effect(0.10, 5) is None, "five clicks can see nothing at all"


def test_welch_handles_arms_with_different_spread():
    tight = [0.30, 0.31, 0.29, 0.30, 0.31, 0.30]
    wide = [0.10, 0.90, 0.40, 0.05, 0.70, 0.35]
    got = statistics.mean_difference(tight, wide, metric="acos")
    assert got.test == "welch t" and got.n_treatment == 6
    assert got.ci_low < got.difference < got.ci_high
    assert got.p_value > 0.05, "wildly spread data should not look like a result"


# ---------------- designing ----------------
def a_campaign_of_targets(build, *, count=16, clicks_each=40, orders_each=4, bid=1.00):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    for n in range(count):
        build.target(campaign, group, target_id=f"K{n}", text=f"term {n}", bid=bid)
        build.metrics(entity_type="target", entity_id=f"K{n}", clicks=clicks_each,
                      orders=orders_each, spend=clicks_each * 0.5, sales=orders_each * 20.0)
    build.db.commit()
    return campaign


def test_an_experiment_splits_the_targets_into_two_comparable_arms(build):
    a_campaign_of_targets(build, count=16, clicks_each=200, orders_each=20)
    candidates = experiments.candidates_for(build.db, build.profile, change_type="bid_increase",
                                            today=build.today)
    assert len(candidates) == 16
    got = experiments.design(build.db, build.profile, hypothesis="Raising bids 25% on these will "
                             "win more top-of-search without wrecking ACOS",
                             change_type="bid_increase", candidates=candidates, today=build.today)
    treatment = experiments.units(build.db, got, arm="treatment")
    control = experiments.units(build.db, got, arm="control")
    assert len(treatment) == len(control) == 8
    assert got.ref.startswith("X-") and got.status == "designed"
    assert got.has_true_control is True
    assert got.design["assignment"] == "stratified randomisation by click volume"


def test_the_arms_are_balanced_on_traffic_not_merely_random(build):
    """A coin flip over a few dozen targets routinely puts most of the traffic in one arm."""
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    # Wildly uneven volumes: four big targets and twelve small ones.
    for n in range(16):
        clicks = 400 if n < 4 else 40
        build.target(campaign, group, target_id=f"K{n}", text=f"term {n}", bid=1.00)
        build.metrics(entity_type="target", entity_id=f"K{n}", clicks=clicks, orders=clicks // 10,
                      spend=clicks * 0.5, sales=clicks * 2.0)
    build.db.commit()
    candidates = experiments.candidates_for(build.db, build.profile, change_type="bid_increase",
                                            today=build.today)
    got = experiments.design(build.db, build.profile, hypothesis="h", change_type="bid_increase",
                             candidates=candidates, today=build.today)
    by_arm = {}
    for unit in experiments.units(build.db, got):
        by_arm.setdefault(unit.arm, []).append((unit.pre_period_metrics or {}).get("clicks", 0))
    treated, control = sum(by_arm["treatment"]), sum(by_arm["control"])
    bigger, smaller = max(treated, control), min(treated, control)
    assert bigger / smaller < 1.5, f"arms are lopsided: {treated} vs {control} clicks"
    assert len({u.stratum for u in experiments.units(build.db, got)}) > 1


def test_an_experiment_that_cannot_answer_its_question_is_refused(build):
    """Three weeks of spend ending in "inconclusive" is worse than not starting."""
    a_campaign_of_targets(build, count=16, clicks_each=25, orders_each=1)
    candidates = experiments.candidates_for(build.db, build.profile, change_type="bid_increase",
                                            today=build.today)
    with pytest.raises(experiments.ExperimentError) as raised:
        experiments.design(build.db, build.profile, hypothesis="h", change_type="bid_increase",
                           candidates=candidates, today=build.today)
    message = str(raised.value)
    assert "could only detect" in message or "isn't enough traffic" in message
    assert "%" in message or "clicks per arm" in message, "it says what it would have needed"


def test_too_few_targets_to_split_is_refused_with_the_number(build):
    a_campaign_of_targets(build, count=5, clicks_each=200, orders_each=20)
    candidates = experiments.candidates_for(build.db, build.profile, change_type="bid_increase",
                                            today=build.today)
    with pytest.raises(experiments.ExperimentError, match="only 5 targets"):
        experiments.design(build.db, build.profile, hypothesis="h", change_type="bid_increase",
                           candidates=candidates, today=build.today)


def test_the_running_limit_is_honoured(build):
    settings_store.set_value(build.db, "ppc_max_experiments_running", 1, SYSTEM)
    a_campaign_of_targets(build, count=16, clicks_each=200, orders_each=20)
    candidates = experiments.candidates_for(build.db, build.profile, change_type="bid_increase",
                                            today=build.today)
    first = experiments.design(build.db, build.profile, hypothesis="h",
                               change_type="bid_increase", candidates=candidates, today=build.today)
    first.status = "running"
    first.started_at = utcnow()
    build.db.commit()
    with pytest.raises(experiments.ExperimentError, match="already running"):
        experiments.design(build.db, build.profile, hypothesis="h2", change_type="bid_increase",
                           candidates=candidates, today=build.today)


def test_the_design_records_what_it_could_detect_and_what_it_risks(build):
    a_campaign_of_targets(build, count=16, clicks_each=300, orders_each=30)
    candidates = experiments.candidates_for(build.db, build.profile, change_type="bid_increase",
                                            today=build.today)
    got = experiments.design(build.db, build.profile, hypothesis="h", change_type="bid_increase",
                             candidates=candidates, today=build.today)
    power = got.design["power"]
    assert power["baseline_cvr"] == pytest.approx(0.10, abs=0.01)
    assert power["expected_clicks_per_arm"] > 0
    assert 0 < power["minimum_detectable_relative_effect"] <= experiments.WORTHWHILE_EFFECT
    assert float(got.spend_at_risk) > 0
    assert float(got.spend_at_risk) <= 300, "the setting caps what can be put at risk"
    assert got.guardrails["abort_if_spend_exceeds"] == float(got.spend_at_risk)


def test_the_assignment_can_be_re_derived_from_the_recorded_seed(build):
    a_campaign_of_targets(build, count=16, clicks_each=200, orders_each=20)
    candidates = experiments.candidates_for(build.db, build.profile, change_type="bid_increase",
                                            today=build.today)
    got = experiments.design(build.db, build.profile, hypothesis="h", change_type="bid_increase",
                             candidates=candidates, today=build.today)
    seed = got.design["seed"]
    again = experiments.assign(experiments.stratify(candidates), seed=seed)
    stored = {u.amazon_entity_id: u.arm for u in experiments.units(build.db, got)}
    assert {c.amazon_entity_id: arm for c, arm, _s in again} == stored


# ---------------- starting ----------------
def ok_207(entity_id, **fields):
    return {"keywords": {"success": [{"index": 0, "keywordId": entity_id,
                                      "keyword": {"keywordId": entity_id, **fields}}],
                         "error": []}}


def refused_207(message="Amazon said no"):
    return {"keywords": {"success": [], "error": [{"index": 0, "errors": [
        {"errorValue": {"itemLevelError": {"code": "X", "message": message}}}]}]}}


def designed(build, **kw):
    a_campaign_of_targets(build, count=16, clicks_each=200, orders_each=20)
    candidates = experiments.candidates_for(build.db, build.profile, change_type="bid_increase",
                                            today=build.today)
    return experiments.design(build.db, build.profile, hypothesis="h", change_type="bid_increase",
                              candidates=candidates, today=build.today, **kw)


def test_starting_needs_an_approval_that_shows_the_money(build, owner):
    got = designed(build)
    approval = experiments.request_approval(build.db, got)
    build.db.commit()
    assert got.status == "pending_approval" and approval.risk_level == "high"
    assert "at risk over" in approval.summary
    assert approval.action_payload["spend_at_risk"] == float(got.spend_at_risk)
    assert len(approval.action_payload["treatment"]) == 8


def test_only_the_treatment_arm_is_changed(build, owner):
    got = designed(build)
    treated = [u.amazon_entity_id for u in experiments.units(build.db, got, arm="treatment")]
    for entity_id in treated:
        FakeAdsClient.expect(r"/sp/keywords$", ok_207(entity_id, bid=1.25))
    result = experiments.start(build.db, got, actor=Actor.user(owner.id), client=FakeAdsClient())
    assert set(result["treated"]) == set(treated) and got.status == "running"
    sent = [c["json"]["keywords"][0]["keywordId"] for c in FakeAdsClient.calls_to("/sp/keywords$")]
    assert set(sent) == set(treated), "the control arm is never touched"
    assert all(c["json"]["keywords"][0]["bid"] == 1.25
               for c in FakeAdsClient.calls_to("/sp/keywords$"))


def test_a_start_that_fails_part_way_puts_everything_back(build, owner):
    """A half-treated arm is not an experiment."""
    got = designed(build)
    treated = [u.amazon_entity_id for u in experiments.units(build.db, got, arm="treatment")]
    FakeAdsClient.expect(r"/sp/keywords$", ok_207(treated[0], bid=1.25))
    FakeAdsClient.expect(r"/sp/keywords$", refused_207("bid above the ceiling"))
    FakeAdsClient.expect(r"/sp/keywords$", ok_207(treated[0], bid=1.00))   # the undo
    with pytest.raises(experiments.ExperimentError, match="put back"):
        experiments.start(build.db, got, actor=Actor.user(owner.id), client=FakeAdsClient())
    assert got.status == "abandoned" and got.stopped_reason
    undone = build.db.execute(select(PpcAction).where(
        PpcAction.experiment_id == got.id, PpcAction.rolled_back_at.is_not(None))).scalars().all()
    assert len(undone) == 1, "the one that went through was put back"


def test_nothing_starts_while_ppc_is_read_only(build, owner, db):
    got = designed(build)
    settings_store.set_value(db, "ppc_write_mode", "read_only", SYSTEM)
    build.profile.write_mode = "read_only"
    db.commit()
    with pytest.raises(experiments.ExperimentError, match="read-only"):
        experiments.start(build.db, got, actor=Actor.user(owner.id), client=FakeAdsClient())
    assert FakeAdsClient.calls == []


def test_an_experiment_whose_arms_changed_after_approval_is_not_started(build, owner):
    from app.ppc import experiments as exp

    got = designed(build)
    approval = exp.request_approval(build.db, got)
    build.db.commit()
    # Somebody moves a unit between arms after the approval was created.
    unit = exp.units(build.db, got, arm="control")[0]
    unit.arm = "treatment"
    build.db.commit()
    from app.services.approvals import EXECUTORS

    with pytest.raises(exp.ExperimentError, match="arms have changed"):
        EXECUTORS["ppc_start_experiment"](build.db, approval.action_payload, approval)


# ---------------- guardrails ----------------
def running_experiment(build, owner, *, days_ago=10):
    got = designed(build)
    for unit in experiments.units(build.db, got, arm="treatment"):
        FakeAdsClient.expect(r"/sp/keywords$", ok_207(unit.amazon_entity_id, bid=1.25))
    experiments.start(build.db, got, actor=Actor.user(owner.id), client=FakeAdsClient())
    got.started_at = utcnow() - timedelta(days=days_ago)
    got.ends_at = got.started_at + timedelta(days=got.planned_days)
    build.db.commit()
    FakeAdsClient.reset()
    return got


def fill_arms(build, got, *, treatment, control, days=6):
    start = got.started_at.date()
    for unit in experiments.units(build.db, got, arm="treatment"):
        day_rows(build, unit.amazon_entity_id, start=start, days=days, **treatment)
    for unit in experiments.units(build.db, got, arm="control"):
        day_rows(build, unit.amazon_entity_id, start=start, days=days, **control)
    build.db.commit()


def test_spending_past_the_agreed_limit_stops_it(build, owner):
    got = running_experiment(build, owner)
    got.guardrails = {**got.guardrails, "abort_if_spend_exceeds": 20.0}
    build.db.commit()
    fill_arms(build, got, treatment=dict(clicks=20, orders=2, spend=10.0, sales=40.0),
              control=dict(clicks=20, orders=2, spend=2.0, sales=40.0))
    for unit in experiments.units(build.db, got, arm="treatment"):
        FakeAdsClient.expect(r"/sp/keywords$", ok_207(unit.amazon_entity_id, bid=1.00))
    watched = experiments.monitor(build.db, got, today=build.today)
    assert "at or past the" in (watched["stopped"] or "")
    assert got.status == "stopped_early" and got.stopped_reason
    assert FakeAdsClient.calls_to("/sp/keywords$"), "the treated bids were put back"


def test_a_treated_arm_doing_much_worse_stops_it(build, owner):
    got = running_experiment(build, owner)
    fill_arms(build, got, treatment=dict(clicks=20, orders=1, spend=2.0, sales=10.0),
              control=dict(clicks=20, orders=2, spend=2.0, sales=40.0))
    for unit in experiments.units(build.db, got, arm="treatment"):
        FakeAdsClient.expect(r"/sp/keywords$", ok_207(unit.amazon_entity_id, bid=1.00))
    watched = experiments.monitor(build.db, got, today=build.today)
    assert "worse by more than" in (watched["stopped"] or "")
    assert got.status == "stopped_early"


def test_a_healthy_experiment_is_left_alone(build, owner):
    got = running_experiment(build, owner)
    fill_arms(build, got, treatment=dict(clicks=20, orders=3, spend=2.0, sales=60.0),
              control=dict(clicks=20, orders=2, spend=2.0, sales=40.0))
    watched = experiments.monitor(build.db, got, today=build.today)
    assert watched["stopped"] is None and got.status == "running"


def test_a_unit_something_else_happened_to_is_dropped(build, owner):
    got = running_experiment(build, owner)
    fill_arms(build, got, treatment=dict(clicks=20, orders=2, spend=2.0, sales=40.0),
              control=dict(clicks=20, orders=2, spend=2.0, sales=40.0))
    unit = experiments.units(build.db, got, arm="treatment")[0]
    build.db.add(PpcAction(ad_product="sp", profile_id=build.profile.id, change_type="bid_decrease",
                           entity_type="target", amazon_entity_id=unit.amazon_entity_id,
                           before_value={}, after_value={},
                           executed_at=got.started_at + timedelta(days=2)))
    build.db.commit()
    watched = experiments.monitor(build.db, got, today=build.today)
    assert any(x["unit"] == unit.amazon_entity_id for x in watched["excluded"])
    build.db.refresh(unit)
    assert unit.excluded_at is not None and "outside the experiment" in unit.excluded_reason


def test_stopping_by_hand_needs_no_approval_and_puts_the_bids_back(build, owner):
    got = running_experiment(build, owner)
    for unit in experiments.units(build.db, got, arm="treatment"):
        FakeAdsClient.expect(r"/sp/keywords$", ok_207(unit.amazon_entity_id, bid=1.00))
    experiments.stop(build.db, got, reason="you asked me to stop it",
                     actor=Actor.user(owner.id), client=FakeAdsClient())
    assert got.status == "stopped_early"
    assert len(FakeAdsClient.calls_to("/sp/keywords$")) == 8


# ---------------- evaluating ----------------
def test_a_real_improvement_is_reported_with_its_interval(build, owner):
    got = running_experiment(build, owner, days_ago=30)
    # 14% against 10%, with real traffic behind it.
    fill_arms(build, got, treatment=dict(clicks=250, orders=35, spend=50.0, sales=700.0),
              control=dict(clicks=250, orders=25, spend=50.0, sales=500.0), days=10)
    experiments.evaluate(build.db, got, today=build.today)
    assert got.conclusion == "supported"
    comparison = got.result["comparison"]
    assert comparison["significant"] is True and comparison["p_value"] < 0.05
    assert comparison["confidence_interval"][0] > 0
    assert "excludes no effect" in got.learning_note
    assert float(got.achieved_power) > 0.8


def test_no_difference_with_enough_traffic_is_a_real_answer(build, owner):
    got = running_experiment(build, owner, days_ago=30)
    fill_arms(build, got, treatment=dict(clicks=400, orders=40, spend=50.0, sales=800.0),
              control=dict(clicks=400, orders=40, spend=50.0, sales=800.0), days=10)
    experiments.evaluate(build.db, got, today=build.today)
    assert got.conclusion == "not_supported"
    assert "did not work" in got.learning_note
    assert "chance of finding a 25% improvement" in got.learning_note
    # Power at the observed effect is alpha when nothing moved, which is why it is not the test.
    power = got.result["power"]
    assert power["at_the_observed_effect"] == pytest.approx(0.05, abs=0.01)
    assert power["for_a_meaningful_effect"] > 0.8


def test_no_difference_without_enough_traffic_says_we_could_not_tell(build, owner):
    got = running_experiment(build, owner, days_ago=30)
    fill_arms(build, got, treatment=dict(clicks=4, orders=1, spend=2.0, sales=20.0),
              control=dict(clicks=4, orders=1, spend=2.0, sales=20.0), days=3)
    experiments.evaluate(build.db, got, today=build.today)
    assert got.conclusion == "inconclusive"
    assert "couldn't tell" in got.learning_note or "nothing here to conclude" in got.learning_note


def test_an_experiment_across_prime_day_cannot_be_believed(build, owner):
    got = running_experiment(build, owner, days_ago=30)
    build.db.add(PpcCalendarEvent(kind="prime_day", name="Prime Day",
                                  start_date=got.started_at.date() + timedelta(days=2),
                                  end_date=got.started_at.date() + timedelta(days=3)))
    fill_arms(build, got, treatment=dict(clicks=250, orders=35, spend=50.0, sales=700.0),
              control=dict(clicks=250, orders=25, spend=50.0, sales=500.0), days=10)
    experiments.evaluate(build.db, got, today=build.today)
    assert got.conclusion == "inconclusive"
    assert "Prime Day" in got.learning_note


def test_too_many_dropped_units_makes_the_rest_unreadable(build, owner):
    got = running_experiment(build, owner, days_ago=30)
    fill_arms(build, got, treatment=dict(clicks=250, orders=35, spend=50.0, sales=700.0),
              control=dict(clicks=250, orders=25, spend=50.0, sales=500.0), days=10)
    for unit in experiments.units(build.db, got)[:6]:
        unit.excluded_reason = "something else happened"
        unit.excluded_at = utcnow()
    build.db.commit()
    experiments.evaluate(build.db, got, today=build.today)
    assert got.conclusion == "inconclusive" and "too many to read" in got.learning_note


# ---------------- what the owner sees ----------------
def test_the_experiments_view_explains_itself_when_there_are_none(build):
    text = views.experiments_summary(build.db)
    assert "No experiments yet" in text and "half are left exactly as they are" in text


def test_the_detail_view_shows_what_it_can_and_cannot_detect(build, owner):
    got = designed(build)
    text = views.experiment_detail(build.db, got.ref)
    assert got.ref in text and "What it can see" in text
    assert "smallest change it could detect" in text
    assert "Limits:" in text and "stops if spend passes" in text


def test_the_detail_view_shows_the_result_and_marks_an_early_read(build, owner):
    got = running_experiment(build, owner, days_ago=10)
    fill_arms(build, got, treatment=dict(clicks=250, orders=35, spend=50.0, sales=700.0),
              control=dict(clicks=250, orders=25, spend=50.0, sales=500.0), days=6)
    experiments.evaluate(build.db, got, today=build.today)
    build.db.commit()
    text = views.experiment_detail(build.db, got.ref)
    assert "Power achieved" in text and "early read" in text
    assert "interval" in text and "p " in text
