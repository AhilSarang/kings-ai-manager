"""P4 M6: recommendations, the digest, batch and partial approval, execution and rollback.

The thing under test is the contract: what the owner was shown is what gets applied, nothing reaches
Amazon without his code, a failure stops the batch and says where it stopped, and anything applied
can be put back.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import (Approval, PpcAction, PpcAnalysisRun, PpcRecommendation, PpcRule, PpcTarget)
from app.ppc import apply as ppc_apply
from app.ppc import analysers, recommendations
from app.ppc.candidates import Candidate
from app.ppc.fake import FakeAdsClient
from app.services import settings_store
from app.services.common import Actor, utcnow
from tests.conftest import run_jobs
from tests.ppc.test_m5_analysers import ASIN, MARKETPLACE, SKU, Build, account, profile  # noqa: F401


@pytest.fixture
def build(db, profile, account):
    return Build(db, profile, account)


@pytest.fixture(autouse=True)
def ads_ready(monkeypatch, db, profile):
    """Credentials present and writes allowed, unless a test says otherwise."""
    from pydantic import SecretStr

    from app.config import get_settings
    from app.services.common import SYSTEM

    s = get_settings()
    monkeypatch.setattr(s, "ads_backend", "fake", raising=False)
    monkeypatch.setattr(s, "ads_lwa_client_id", "cid", raising=False)
    monkeypatch.setattr(s, "ads_lwa_client_secret", SecretStr("secret"), raising=False)
    monkeypatch.setattr(s, "ads_lwa_refresh_token", SecretStr("Atzr|x"), raising=False)
    settings_store.set_value(db, "ppc_write_mode", "live", SYSTEM)
    profile.write_mode = "live"
    db.commit()


def waste(build, *, target_id="K1", text="dead weight", clicks=60, spend=30.0):
    """An account with one obvious finding: dry clicks on a keyword."""
    listing = build.listing()
    build.economics()
    campaign = build.campaign(budget=40.0)
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, target_id=target_id, text=text, bid=1.00)
    build.metrics(entity_type="target", entity_id=target_id, clicks=clicks, orders=0, spend=spend,
                  sales=0.0)
    build.db.commit()
    return campaign, group


def analyse(build):
    result = analysers.run_all(build.db, build.profile, today=build.today)
    start, end = analysers.aggregates.window(build.profile, result["window_days"],
                                             today=build.today)
    run = recommendations.store_run(build.db, build.profile,
                                   {**result, "window_start": start, "window_end": end})
    build.db.commit()
    return run


def ok_207(collection="keywords", entity_id="K1", **fields):
    """Amazon's real write answer: 207 with per-item success and error lists."""
    inner = {"keywordId": entity_id, **fields}
    return {collection: {"success": [{"index": 0, "keywordId": entity_id,
                                      collection[:-1]: inner}], "error": []}}


def refused_207(collection="keywords", message="Bid is below the floor for this marketplace"):
    return {collection: {"success": [],
                         "error": [{"index": 0, "errors": [{"errorValue": {
                             "itemLevelError": {"code": "INVALID_BID", "message": message}}}]}]}}


# ---------------- storing a run ----------------
def test_a_run_becomes_a_numbered_batch_of_numbered_recommendations(build):
    waste(build)
    run = analyse(build)
    rows = recommendations.in_batch(build.db, run)
    assert run.ref.startswith("B-") and rows
    assert rows[0].ref.startswith("P-") and rows[0].status == "proposed"
    assert rows[0].change_type == "pause_target" and rows[0].amazon_entity_id == "K1"
    assert run.recommendations_made == len(rows) and run.candidates_considered >= len(rows)
    assert run.data_window_start is not None and run.finished_at is not None


def test_every_stored_recommendation_carries_what_the_brief_requires(build):
    waste(build)
    run = analyse(build)
    [row] = recommendations.in_batch(build.db, run)
    assert row.reason and row.analyser == "wasted_spend"
    assert row.evidence["clicks"] == 60 and row.evidence["from"] and row.evidence["to"]
    assert row.evidence["analyser"] == "wasted_spend"
    effect = row.predicted_effect
    assert effect["monthly_effect_low"] <= effect["monthly_effect_estimate"] <= effect["monthly_effect_high"]
    assert "tacos_impact" in row.evidence, "7.2 asks for the TACOS impact, not just ACOS"
    assert row.confidence and row.risk in ("low", "medium", "high")
    assert row.spend_at_risk is not None


def test_the_daily_cap_is_honoured(build, owner):
    from app.services.common import SYSTEM

    settings_store.set_value(build.db, "ppc_daily_recommendation_cap", 2, SYSTEM)
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    for n in range(5):
        build.target(campaign, group, target_id=f"K{n}", text=f"dead {n}", bid=1.00)
        build.metrics(entity_type="target", entity_id=f"K{n}", clicks=60, orders=0,
                      spend=30.0 + n, sales=0.0)
    build.db.commit()
    run = analyse(build)
    assert len(recommendations.in_batch(build.db, run)) == 2
    assert run.candidates_considered >= 5, "the rest were considered, not unseen"


def test_a_protected_entity_is_never_proposed(build):
    build.db.add(PpcRule(scope="account", protected_entities=["dead weight"], active=True))
    waste(build)
    run = analyse(build)
    assert recommendations.in_batch(build.db, run) == []
    assert run.suppressed_by_rule and run.suppressed_by_rule[0]["entity"] == "dead weight"


def test_a_product_we_cannot_price_is_flagged_on_the_recommendation(build):
    listing = build.listing(cost=None)
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=60, orders=0, spend=30.0, sales=0.0)
    build.db.commit()
    run = analyse(build)
    rows = recommendations.in_batch(build.db, run)
    assert rows and rows[0].target_acos_unverified is True


# ---------------- the digest ----------------
def test_the_digest_groups_by_kind_and_says_how_to_approve(build):
    waste(build)
    build.term(text="free samples please", clicks=20, orders=0, spend=12.0, sales=0.0)
    build.db.commit()
    run = analyse(build)
    approval = recommendations.request_batch_approval(build.db, run)
    build.db.commit()
    text = recommendations.digest(build.db, run, approval=approval)
    assert run.ref in text and "changes to approve" in text
    assert "pause" in text and "negative keyword" in text
    assert f"APPROVE BATCH {run.ref} {approval.confirmation_code}" in text
    assert "REJECT P-" in text and "teaches me something" in text


def test_the_digest_says_what_it_held_back(build):
    listing = build.listing(stock=0)
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=8, spend=20.0, sales=200.0)
    build.metrics(entity_type="campaign", entity_id="C1", clicks=40, orders=8, spend=20.0,
                  sales=200.0, top_share=25.0)
    build.db.commit()
    run = analyse(build)
    assert "held back because the product can't take more spend" in recommendations.digest(
        build.db, run)


def test_details_shows_the_arithmetic_behind_one_recommendation(build):
    waste(build)
    run = analyse(build)
    [row] = recommendations.in_batch(build.db, run)
    text = recommendations.details(build.db, row.ref)
    assert row.ref in text and "clicks 60" in text and "orders 0" in text
    assert "Target ACOS" in text and "Expected:" in text and "between" in text
    assert "wasted_spend analyser" in text


def test_batch_details_lists_every_recommendation_with_its_ref(build):
    waste(build)
    build.term(text="free samples please", clicks=20, orders=0, spend=12.0, sales=0.0)
    build.db.commit()
    run = analyse(build)
    text = recommendations.batch_details(build.db, run.ref)
    for row in recommendations.in_batch(build.db, run):
        assert row.ref in text
    assert "P-14 DETAILS" in text or "DETAILS" in text


# ---------------- approving ----------------
def test_the_whole_batch_can_be_approved_with_one_code(build, owner):
    waste(build)
    run = analyse(build)
    approval = recommendations.request_batch_approval(build.db, run)
    build.db.commit()
    got = recommendations.decide(build.db, owner, batch_ref=run.ref,
                                 code=approval.confirmation_code)
    assert got["approved"] and not got["skipped"]
    assert all(r.status == "approved" for r in recommendations.in_batch(build.db, run))
    assert build.db.get(Approval, approval.id).status == "approved"


def test_a_subset_can_be_approved_and_the_rest_close(build, owner):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    for n in range(3):
        build.target(campaign, group, target_id=f"K{n}", text=f"dead {n}", bid=1.00)
        build.metrics(entity_type="target", entity_id=f"K{n}", clicks=60, orders=0,
                      spend=30.0 + n, sales=0.0)
    build.db.commit()
    run = analyse(build)
    approval = recommendations.request_batch_approval(build.db, run)
    build.db.commit()
    rows = recommendations.in_batch(build.db, run)
    chosen = [rows[0].ref, rows[2].ref]
    got = recommendations.decide(build.db, owner, refs=chosen, code=approval.confirmation_code)
    assert set(got["approved"]) == set(chosen)
    statuses = {r.ref: r.status for r in recommendations.in_batch(build.db, run)}
    assert statuses[rows[0].ref] == "approved" and statuses[rows[2].ref] == "approved"
    assert statuses[rows[1].ref] == "expired", "not chosen is not the same as rejected"


def test_a_wrong_code_changes_nothing_at_all(build, owner):
    waste(build)
    run = analyse(build)
    approval = recommendations.request_batch_approval(build.db, run)
    build.db.commit()
    from app.services.approvals import ApprovalError

    with pytest.raises(ApprovalError):
        recommendations.decide(build.db, owner, batch_ref=run.ref, code="XXXX")
    build.db.rollback()
    assert all(r.status == "proposed" for r in recommendations.in_batch(build.db, run))
    assert build.db.get(Approval, approval.id).status == "pending"


def test_refs_from_two_batches_cannot_be_approved_together(build, owner):
    """Two digests are two decisions; one code must not reach across both."""
    waste(build)
    first = analyse(build)
    second = analyse(build)          # the same findings, stored again as a fresh batch
    one = recommendations.in_batch(build.db, first)[0]
    two = recommendations.in_batch(build.db, second)[0]
    assert one.batch_id != two.batch_id
    with pytest.raises(recommendations.PpcError, match="different batch"):
        recommendations.decide(build.db, owner, refs=[one.ref, two.ref], code="ABCD")


def test_only_an_owner_can_approve(build, db):
    from app.db.models import User

    waste(build)
    run = analyse(build)
    recommendations.request_batch_approval(build.db, run)
    manager = User(name="Manager", whatsapp_number="+15550000001", role="manager")
    db.add(manager)
    db.commit()
    with pytest.raises(recommendations.PpcError, match="Only an owner"):
        recommendations.decide(db, manager, batch_ref=run.ref, code="ABCD")


def test_a_rejection_keeps_the_reason(build, owner):
    waste(build)
    run = analyse(build)
    [row] = recommendations.in_batch(build.db, run)
    got = recommendations.reject(build.db, owner, row.ref, "too aggressive on a new product")
    assert got.status == "rejected" and "too aggressive" in got.rejected_reason
    assert got.rejected_at is not None


# ---------------- PIN conditions (7.3) ----------------
def test_a_big_budget_rise_needs_the_pin(build, owner):
    listing = build.listing()
    build.economics()
    campaign = build.campaign(budget=200.0)
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    for n in range(6):
        build.day_of_spend("C1", day=build.end - timedelta(days=n), spend=200.0, sales=1000.0,
                           clicks=100, orders=20)
    build.db.commit()
    run = analyse(build)
    rows = recommendations.in_batch(build.db, run)
    assert any(r.change_type == "budget_increase" for r in rows)
    approval = recommendations.request_batch_approval(build.db, run)
    assert approval.risk_level == "critical", "a +50/day rise is a PIN decision"
    assert "raises a daily budget" in approval.action_payload["pin_reason"]


def test_a_structural_change_needs_the_pin(build, owner):
    listing = build.listing()
    build.economics()
    first = build.campaign(campaign_id="C1", name="exact")
    second = build.campaign(campaign_id="C2", name="broad")
    g1 = build.ad_group(first, ad_group_id="G1", listing=listing)
    g2 = build.ad_group(second, ad_group_id="G2", listing=listing)
    build.target(first, g1, target_id="K1", text="nicotine pouches", bid=1.00)
    build.target(second, g2, target_id="K2", text="nicotine pouches", bid=1.20)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=6, spend=20.0, sales=100.0,
                  campaign_id="C1", ad_group_id="G1")
    build.metrics(entity_type="target", entity_id="K2", clicks=40, orders=2, spend=40.0, sales=50.0,
                  campaign_id="C2", ad_group_id="G2")
    build.db.commit()
    run = analyse(build)
    approval = recommendations.request_batch_approval(build.db, run)
    assert approval.risk_level == "critical"
    assert "laid out" in approval.action_payload["pin_reason"]


# ---------------- applying ----------------
def approved(build, owner):
    run = analyse(build)
    approval = recommendations.request_batch_approval(build.db, run)
    build.db.commit()
    recommendations.decide(build.db, owner, batch_ref=run.ref, code=approval.confirmation_code)
    build.db.commit()
    return run


def test_an_approved_change_is_sent_read_back_and_recorded(build, owner):
    waste(build)
    run = approved(build, owner)
    [row] = recommendations.in_batch(build.db, run)
    FakeAdsClient.expect(r"/sp/keywords$", ok_207(entity_id="K1", state="PAUSED"))
    FakeAdsClient.expect(r"/sp/keywords/list", {"keywords": [{"keywordId": "K1", "state": "PAUSED",
                                                              "bid": 1.00}]})
    action = ppc_apply.apply_one(build.db, row, actor=Actor.user(owner.id), client=FakeAdsClient())
    assert row.status == "executed"
    assert action.before_value["state"] == "ENABLED" and action.after_value["state"] == "PAUSED"
    assert action.verified_at is not None and action.failed_reason is None
    assert action.rollback_payload["undo"] == "put_back"
    sent = FakeAdsClient.last_body("/sp/keywords$")
    assert sent == {"keywords": [{"keywordId": "K1", "state": "PAUSED"}]}


def test_the_media_type_amazon_demands_is_used(build, owner):
    waste(build)
    run = approved(build, owner)
    [row] = recommendations.in_batch(build.db, run)
    FakeAdsClient.expect(r"/sp/keywords$", ok_207(entity_id="K1", state="PAUSED"))
    FakeAdsClient.expect(r"/sp/keywords/list", {"keywords": [{"keywordId": "K1", "state": "PAUSED"}]})
    ppc_apply.apply_one(build.db, row, actor=Actor.user(owner.id), client=FakeAdsClient())
    call = [c for c in FakeAdsClient.calls if c["path"] == "/sp/keywords"][0]
    assert call["content_type"] == "application/vnd.spKeyword.v3+json"
    assert call["method"] == "PUT"


def test_a_207_whose_every_item_failed_is_not_a_success(build, owner):
    """The status code says 207 either way; the body is what says whether it worked."""
    waste(build)
    run = approved(build, owner)
    [row] = recommendations.in_batch(build.db, run)
    FakeAdsClient.expect(r"/sp/keywords$", refused_207())
    with pytest.raises(ppc_apply.WriteFailed, match="below the floor"):
        ppc_apply.apply_one(build.db, row, actor=Actor.user(owner.id), client=FakeAdsClient())
    assert row.status == "failed"
    action = build.db.execute(select(PpcAction)).scalar_one()
    assert "below the floor" in action.failed_reason


def test_a_change_amazon_reports_differently_is_said_not_assumed(build, owner):
    waste(build)
    run = approved(build, owner)
    [row] = recommendations.in_batch(build.db, run)
    FakeAdsClient.expect(r"/sp/keywords$", ok_207(entity_id="K1", state="PAUSED"))
    # Amazon says it is still enabled when we asked for paused.
    FakeAdsClient.expect(r"/sp/keywords/list", {"keywords": [{"keywordId": "K1", "state": "ENABLED"}]})
    action = ppc_apply.apply_one(build.db, row, actor=Actor.user(owner.id), client=FakeAdsClient())
    assert row.status == "executed", "the write was accepted; the mismatch is reported, not raised"
    assert "different value than we asked for" in action.failed_reason


def test_nothing_is_sent_while_ppc_is_read_only(build, owner, db):
    from app.services.common import SYSTEM

    waste(build)
    run = approved(build, owner)
    settings_store.set_value(db, "ppc_write_mode", "read_only", SYSTEM)
    build.profile.write_mode = "read_only"
    db.commit()
    [row] = recommendations.in_batch(build.db, run)
    with pytest.raises(ppc_apply.WriteRefused, match="read-only"):
        ppc_apply.apply_one(build.db, row, actor=Actor.user(owner.id), client=FakeAdsClient())
    assert FakeAdsClient.calls == []


def test_a_structural_recommendation_is_not_something_i_can_do_for_you(build, owner):
    row = PpcRecommendation(ad_product="sp", profile_id=build.profile.id, change_type="campaign_structure",
                            entity_type="campaign", amazon_entity_id="C1", reason="x",
                            status="approved")
    build.db.add(row)
    build.db.commit()
    with pytest.raises(ppc_apply.WriteRefused, match="Seller Central"):
        ppc_apply.apply_one(build.db, row, actor=Actor.user(owner.id), client=FakeAdsClient())


def test_a_batch_stops_at_the_first_failure_and_says_where(build, owner):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    for n in range(3):
        build.target(campaign, group, target_id=f"K{n}", text=f"dead {n}", bid=1.00)
        build.metrics(entity_type="target", entity_id=f"K{n}", clicks=60, orders=0,
                      spend=50.0 - n, sales=0.0)
    build.db.commit()
    run = approved(build, owner)
    rows = recommendations.in_batch(build.db, run, ("approved",))
    FakeAdsClient.expect(r"/sp/keywords$", ok_207(entity_id=rows[0].amazon_entity_id,
                                                  state="PAUSED"))
    FakeAdsClient.expect(r"/sp/keywords/list",
                         {"keywords": [{"keywordId": rows[0].amazon_entity_id, "state": "PAUSED"}]})
    FakeAdsClient.expect(r"/sp/keywords$", refused_207(message="Amazon is having a moment"))
    got = ppc_apply.apply_batch(build.db, run, actor=Actor.user(owner.id), client=FakeAdsClient())
    assert got["applied"] == [rows[0].ref]
    assert got["failed"]["ref"] == rows[1].ref and "having a moment" in got["failed"]["why"]
    assert got["not_attempted"] == [rows[2].ref], "nothing after the failure was tried"


# ---------------- rollback ----------------
def test_a_change_can_be_put_back(build, owner):
    waste(build)
    run = approved(build, owner)
    [row] = recommendations.in_batch(build.db, run)
    FakeAdsClient.expect(r"/sp/keywords$", ok_207(entity_id="K1", state="PAUSED"))
    FakeAdsClient.expect(r"/sp/keywords/list", {"keywords": [{"keywordId": "K1", "state": "PAUSED"}]})
    ppc_apply.apply_one(build.db, row, actor=Actor.user(owner.id), client=FakeAdsClient())
    FakeAdsClient.expect(r"/sp/keywords$", ok_207(entity_id="K1", state="ENABLED"))
    action = ppc_apply.rollback(build.db, row, actor=Actor.user(owner.id), client=FakeAdsClient())
    assert row.status == "rolled_back" and action.rolled_back_at is not None
    sent = FakeAdsClient.last_body("/sp/keywords$")
    assert sent == {"keywords": [{"keywordId": "K1", "state": "ENABLED"}]}, \
        "put back exactly what was there, from the payload stored before the write"


def test_undoing_something_created_archives_it_rather_than_putting_a_value_back(build, owner):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, text="pouches", match="broad")
    build.term(text="nicotine pouches mint", clicks=20, orders=4, spend=10.0, sales=80.0)
    build.db.commit()
    run = approved(build, owner)
    harvest = next(r for r in recommendations.in_batch(build.db, run)
                   if r.change_type == "harvest_search_term")
    FakeAdsClient.expect(r"/sp/keywords$", ok_207(entity_id="NEWKW1"))
    action = ppc_apply.apply_one(build.db, harvest, actor=Actor.user(owner.id),
                                 client=FakeAdsClient(), verify=False)
    assert action.amazon_entity_id == "NEWKW1"
    assert action.rollback_payload["undo"] == "archive_created"
    FakeAdsClient.expect(r"/sp/keywords$", ok_207(entity_id="NEWKW1", state="ARCHIVED"))
    ppc_apply.rollback(build.db, harvest, actor=Actor.user(owner.id), client=FakeAdsClient())
    assert FakeAdsClient.last_body("/sp/keywords$") == {
        "keywords": [{"keywordId": "NEWKW1", "state": "ARCHIVED"}]}


def test_a_whole_batch_can_be_put_back_newest_first(build, owner):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    for n in range(2):
        build.target(campaign, group, target_id=f"K{n}", text=f"dead {n}", bid=1.00)
        build.metrics(entity_type="target", entity_id=f"K{n}", clicks=60, orders=0,
                      spend=50.0 - n, sales=0.0)
    build.db.commit()
    run = approved(build, owner)
    rows = recommendations.in_batch(build.db, run, ("approved",))
    for row in rows:
        FakeAdsClient.expect(r"/sp/keywords$", ok_207(entity_id=row.amazon_entity_id,
                                                      state="PAUSED"))
        FakeAdsClient.expect(r"/sp/keywords/list",
                             {"keywords": [{"keywordId": row.amazon_entity_id, "state": "PAUSED"}]})
    ppc_apply.apply_batch(build.db, run, actor=Actor.user(owner.id), client=FakeAdsClient())
    for row in rows:
        FakeAdsClient.expect(r"/sp/keywords$", ok_207(entity_id=row.amazon_entity_id,
                                                      state="ENABLED"))
    got = ppc_apply.rollback_batch(build.db, run, actor=Actor.user(owner.id), client=FakeAdsClient())
    assert set(got["rolled_back"]) == {r.ref for r in rows} and not got["problems"]


# ---------------- end to end, through the queue ----------------
def test_approving_a_digest_applies_it_through_the_job_queue(build, owner, sent, no_quiet_hours):
    owner.last_inbound_at = utcnow()
    waste(build)
    build.db.commit()
    from app.jobs import queue

    queue.enqueue(build.db, "ppc_analyse", {})
    build.db.commit()
    run_jobs(types=["ppc_analyse"])
    run_jobs()
    run = build.db.execute(select(PpcAnalysisRun)).scalars().first()
    assert run is not None and run.digest_sent_at is not None
    approval = build.db.get(Approval, run.approval_id)
    assert any(run.ref in m.get("text", "") for m in sent), "the digest reached the owner"

    [row] = recommendations.in_batch(build.db, run)
    FakeAdsClient.expect(r"/sp/keywords$", ok_207(entity_id=row.amazon_entity_id, state="PAUSED"))
    FakeAdsClient.expect(r"/sp/keywords/list",
                         {"keywords": [{"keywordId": row.amazon_entity_id, "state": "PAUSED"}]})
    recommendations.decide(build.db, owner, batch_ref=run.ref, code=approval.confirmation_code)
    build.db.commit()
    run_jobs(limit=50)
    build.db.expire_all()
    row = recommendations.get_by_ref(build.db, row.ref)
    assert row.status == "executed"
    assert any("applied" in m.get("text", "") for m in sent)


def test_a_recommendation_nobody_answers_expires(build, owner):
    waste(build)
    run = analyse(build)
    [row] = recommendations.in_batch(build.db, run)
    row.expires_at = utcnow() - timedelta(minutes=1)
    build.db.commit()
    assert [r.ref for r in recommendations.expire_due(build.db)] == [row.ref]
    assert recommendations.get_by_ref(build.db, row.ref).status == "expired"
