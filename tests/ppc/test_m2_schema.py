"""P4 M2: the PPC tables, the settings, and the action registry.

The schema review found several defects in the first design; the ones that would have been
invisible until much later have a test here so they cannot come back.
"""

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.actions.registry import ACTIONS, get_spec
from app.actions.tiers import Tier
from app.db.models import (
    PpcAction,
    PpcActionOutcome,
    PpcAdGroup,
    PpcAlert,
    PpcAnalysisRun,
    PpcCampaign,
    PpcExperiment,
    PpcExperimentUnit,
    PpcMetricsDaily,
    PpcPlaybook,
    PpcProfile,
    PpcRecommendation,
    PpcSearchTerm,
    PpcSearchTermDecision,
    PpcTarget,
)
from app.services import settings_store

PROFILE = "3333333333"


@pytest.fixture
def profile(db):
    row = PpcProfile(amazon_profile_id=PROFILE, marketplace_id="ATVPDKIKX0DER", country_code="US",
                     currency="USD", timezone="America/Los_Angeles", account_type="seller",
                     status="active")
    db.add(row)
    db.commit()
    return row


def campaign(db, profile, amazon_id="C1"):
    row = PpcCampaign(ad_product="sp", profile_id=profile.id, amazon_campaign_id=amazon_id, name="Skyzz exact")
    db.add(row)
    db.flush()
    return row


def ad_group(db, profile, camp, amazon_id="AG1"):
    row = PpcAdGroup(ad_product="sp", profile_id=profile.id, campaign_id=camp.id, amazon_ad_group_id=amazon_id,
                     amazon_campaign_id=camp.amazon_campaign_id, name="mouth tape")
    db.add(row)
    db.flush()
    return row


# ---------- profiles ----------
def test_a_new_profile_starts_paused_and_read_only(db):
    row = PpcProfile(amazon_profile_id="999", marketplace_id="A2EUQ1WTGCTBG2", country_code="CA",
                     currency="CAD", timezone="America/Toronto")
    db.add(row)
    db.commit()
    assert row.status == "paused", "a profile the owner hasn't enabled must not be analysed or written to"
    assert row.write_mode == "read_only"


def test_write_mode_and_status_are_constrained(db, profile):
    profile.write_mode = "yolo"
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------- the id conventions the review flagged ----------
def test_amazon_ids_and_our_ids_never_share_a_column_name(db):
    """The first design had profile_id meaning a uuid on some tables and Amazon's string on others."""
    for model in (PpcMetricsDaily, PpcSearchTerm, PpcAlert):
        columns = {c.name for c in model.__table__.columns}
        if "amazon_profile_id" in columns:
            assert "profile_id" not in columns, f"{model.__tablename__} carries both spellings"
    # And where both exist they are different types, deliberately.
    assert PpcProfile.__table__.c.amazon_profile_id.type.python_type is str
    assert PpcRecommendation.__table__.c.profile_id.type.python_type is uuid.UUID


def test_experiment_units_join_metrics_on_the_amazon_id(db, profile):
    """The join key was uuid on one side and Amazon's string on the other: it returned nothing."""
    exp = PpcExperiment(ad_product="sp", profile_id=profile.id, hypothesis="phrase beats exact", change_type="bid_increase")
    db.add(exp)
    db.flush()
    db.add(PpcExperimentUnit(ad_product="sp", experiment_id=exp.id, entity_type="target", amazon_entity_id="T-77",
                             arm="treatment"))
    db.add(PpcMetricsDaily(ad_product="sp", amazon_profile_id=PROFILE, marketplace_id="ATVPDKIKX0DER",
                           entity_type="target", amazon_entity_id="T-77", date=date(2026, 9, 1),
                           clicks=10, spend=5, sales=40, orders=2, is_provisional=False))
    db.commit()
    joined = db.execute(
        select(PpcMetricsDaily.clicks)
        .join(PpcExperimentUnit,
              PpcExperimentUnit.amazon_entity_id == PpcMetricsDaily.amazon_entity_id)
        .where(PpcExperimentUnit.experiment_id == exp.id)).scalars().all()
    assert joined == [10], "the evaluation join has to actually find the metrics"


# ---------- one row per entity per day ----------
def test_a_day_of_metrics_cannot_be_duplicated(db, profile):
    for _ in range(2):
        db.add(PpcMetricsDaily(ad_product="sp", amazon_profile_id=PROFILE, marketplace_id="ATVPDKIKX0DER",
                               entity_type="campaign", amazon_entity_id="C1", date=date(2026, 9, 1)))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_provisional_days_are_marked_by_default(db, profile):
    row = PpcMetricsDaily(ad_product="sp", amazon_profile_id=PROFILE, marketplace_id="ATVPDKIKX0DER",
                          entity_type="campaign", amazon_entity_id="C9", date=date(2026, 9, 2))
    db.add(row)
    db.commit()
    assert row.is_provisional is True, "nothing may be concluded from a row until it stops restating"


# ---------- the harvest decision lives once, not once per day ----------
def test_a_harvest_decision_is_one_row_per_term(db, profile):
    camp = campaign(db, profile)
    group = ad_group(db, profile, camp)
    for day in range(3):
        db.add(PpcSearchTerm(ad_product="sp", amazon_profile_id=PROFILE, marketplace_id="ATVPDKIKX0DER",
                             amazon_campaign_id=camp.amazon_campaign_id,
                             amazon_ad_group_id=group.amazon_ad_group_id,
                             search_term="mouth tape for sleeping", date=date(2026, 9, 1 + day),
                             clicks=4, orders=1))
    db.add(PpcSearchTermDecision(ad_product="sp", amazon_profile_id=PROFILE,
                                 amazon_ad_group_id=group.amazon_ad_group_id,
                                 search_term="mouth tape for sleeping", harvest_status="harvested"))
    db.commit()
    assert "harvest_status" not in {c.name for c in PpcSearchTerm.__table__.columns}, \
        "a decision about a term is not a fact about one day of it"
    decisions = db.execute(select(PpcSearchTermDecision)).scalars().all()
    assert len(decisions) == 1 and decisions[0].harvest_status == "harvested"

    db.add(PpcSearchTermDecision(ad_product="sp", amazon_profile_id=PROFILE,
                                 amazon_ad_group_id=group.amazon_ad_group_id,
                                 search_term="mouth tape for sleeping", harvest_status="negated"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------- targets ----------
def test_one_amazon_target_id_cannot_land_three_times(db, profile):
    camp = campaign(db, profile)
    group = ad_group(db, profile, camp)
    common = dict(profile_id=profile.id, campaign_id=camp.id, ad_group_id=group.id,
                  amazon_target_id="T-1", amazon_campaign_id="C1", amazon_ad_group_id="AG1",
                  target_kind="target")
    db.add(PpcTarget(ad_product="sp", **common, target_type="product", match_type="asin", expression="B0SKYZZ001"))
    db.commit()
    db.add(PpcTarget(ad_product="sp", **common, target_type="category", match_type="category", expression="Health"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_keyword_kind_must_be_a_keyword_type(db, profile):
    camp = campaign(db, profile)
    group = ad_group(db, profile, camp)
    db.add(PpcTarget(ad_product="sp", profile_id=profile.id, campaign_id=camp.id, ad_group_id=group.id,
                     amazon_target_id="T-2", amazon_campaign_id="C1", amazon_ad_group_id="AG1",
                     target_kind="keyword", target_type="product", match_type="asin"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------- refs ----------
def test_every_ref_sequence_hands_out_readable_refs(db, profile):
    run = PpcAnalysisRun(profile_id=profile.id)
    exp = PpcExperiment(ad_product="sp", profile_id=profile.id, hypothesis="h", change_type="bid_decrease")
    db.add_all([run, exp])
    db.flush()
    rec = PpcRecommendation(ad_product="sp", batch_id=run.id, profile_id=profile.id, change_type="bid_decrease",
                            entity_type="target", amazon_entity_id="T-1")
    rule = PpcPlaybook(ad_product="sp", title="t", statement="s", created_from="experiment",
                       created_from_experiment_id=exp.id)
    db.add_all([rec, rule])
    db.commit()
    assert run.ref.startswith("B-") and rec.ref.startswith("P-")
    assert exp.ref.startswith("X-") and rule.ref.startswith("R-")


def test_a_recommendation_belongs_to_exactly_one_batch(db, profile):
    """B-7 has to resolve to one set of rows, or ROLLBACK BATCH B-7 means nothing."""
    assert "batch_ref" not in {c.name for c in PpcRecommendation.__table__.columns}
    fk = next(iter(PpcRecommendation.__table__.c.batch_id.foreign_keys))
    assert fk.column.table.name == "ppc_analysis_runs"


# ---------- the playbook's evidence floor ----------
def test_a_rule_cannot_be_invented(db):
    db.add(PpcPlaybook(ad_product="sp", title="hunch", statement="I reckon", created_from="outcome_analysis"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_thirty_actions_is_the_floor_even_when_the_count_is_missing(db):
    """A CHECK passes on NULL, so the original constraint let an unevidenced rule straight through."""
    db.add(PpcPlaybook(ad_product="sp", title="too thin", statement="s", created_from="outcome_analysis",
                       evidence_action_count=29))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    db.add(PpcPlaybook(ad_product="sp", title="earned it", statement="s", created_from="outcome_analysis",
                       evidence_action_count=30))
    db.commit()
    assert db.execute(select(PpcPlaybook)).scalars().first().status == "proposed"


def test_a_rule_can_suppress_as_well_as_propose(db, profile):
    exp = PpcExperiment(ad_product="sp", profile_id=profile.id, hypothesis="h", change_type="pause_target")
    db.add(exp)
    db.flush()
    rule = PpcPlaybook(ad_product="sp", title="never pause branded", statement="s", created_from="experiment",
                       created_from_experiment_id=exp.id, action_kind="suppress")
    db.add(rule)
    db.commit()
    assert rule.action_kind == "suppress"


# ---------- outcomes ----------
def test_an_action_is_measured_once_per_window(db, profile):
    action = PpcAction(ad_product="sp", profile_id=profile.id, change_type="bid_decrease", entity_type="target",
                       amazon_entity_id="T-1")
    db.add(action)
    db.flush()
    for _ in range(2):
        db.add(PpcActionOutcome(ad_product="sp", action_id=action.id, window_days=7))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------- settings and registry ----------
def test_the_settings_are_seeded_with_the_brief_defaults(db):
    assert settings_store.get(db, "ppc_target_acos_safety_margin_pct") == 70
    assert settings_store.get(db, "ppc_max_bid_change_pct") == 25
    assert settings_store.get(db, "ppc_min_clicks_for_pause") == 30
    assert settings_store.get(db, "ppc_experiment_max_spend_at_risk_usd") == 300
    assert settings_store.get(db, "ppc_max_experiments_running") == 3
    assert settings_store.get(db, "ppc_paused") is False


def test_writes_to_amazon_ads_need_approval_and_amits_rules_have_no_agent_path():
    assert get_spec("ppc_apply_change").tier == Tier.EXTERNAL
    assert get_spec("ppc_apply_change").min_risk == "high"
    assert get_spec("ppc_start_experiment").tier == Tier.EXTERNAL
    assert get_spec("ppc_set_write_mode").min_risk == "critical"
    # Safety actions are never gated behind an approval: stopping something must always be possible.
    for safe in ("ppc_stop_experiment", "ppc_rollback_action"):
        assert get_spec(safe).tier <= Tier.INTERNAL
    assert get_spec("ppc_set_rule").tier == Tier.PROHIBITED


def test_the_tables_all_exist_after_migration(db):
    names = db.execute(text("select table_name from information_schema.tables "
                            "where table_schema='public' and table_name like 'ppc_%'")).scalars().all()
    assert len(names) == 28, sorted(names)
    for expected in ("ppc_metrics_restatements", "ppc_search_term_decisions", "ppc_alerts",
                     "ppc_calendar_events", "ppc_purchased_products"):
        assert expected in names, f"{expected} is needed by the brief's own acceptance criteria"


def test_calendar_events_can_mark_a_contaminated_window(db):
    from app.db.models import PpcCalendarEvent

    db.add(PpcCalendarEvent(kind="prime_day", name="Prime Day 2026",
                            start_date=date(2026, 7, 14), end_date=date(2026, 7, 15)))
    db.commit()
    hit = db.execute(select(PpcCalendarEvent).where(
        PpcCalendarEvent.start_date <= date(2026, 7, 15),
        PpcCalendarEvent.end_date >= date(2026, 7, 14))).scalars().all()
    assert len(hit) == 1, "9.6 forbids conclusions across these, so they must be findable by date"
