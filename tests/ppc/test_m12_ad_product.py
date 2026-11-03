"""P4 M12: Sponsored Products data stays Sponsored Products data.

Everything built in M1-M11 reads Sponsored Products and said so nowhere. `campaign_type` existed,
was hardcoded in one place, and was read by nothing — so every query meant "all ad products" while
being written as though it meant one. Harmless with one ad product; wrong the moment there are three,
and wrong in ways that corrupt measurement rather than crash.

These tests pin the separation itself rather than any particular query, because the failure mode is
a *missing* filter and you cannot test for the absence of something you forgot. So they insert real
Sponsored Brands and Sponsored Display rows and then assert the Sponsored Products answers are
unchanged — which is the property that actually matters, and which no amount of reading the code
proves.
"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import (PPC_AD_PRODUCTS, PpcCampaign, PpcHourlyMetric, PpcMetricsDaily,
                           PpcSearchTermDecision)
from app.ppc import aggregates, alerts, analysers, dayparting, metrics, stream, structure
from tests.ppc.test_m5_analysers import MARKETPLACE, Build, account, profile  # noqa: F401


@pytest.fixture
def build(db, profile, account):
    return Build(db, profile, account)


def _campaign(build, *, ad_product, campaign_id, name, budget=40.0):
    row = PpcCampaign(profile_id=build.profile.id, ad_product=ad_product,
                      amazon_campaign_id=campaign_id, name=name, daily_budget=budget,
                      targeting_type="manual", state="enabled")
    build.db.add(row)
    build.db.flush()
    return row


def _day(build, *, ad_product, entity_id, day, spend, sales, clicks=40, orders=2,
         entity_type="campaign"):
    build.db.add(PpcMetricsDaily(
        amazon_profile_id=build.profile.amazon_profile_id, ad_product=ad_product,
        marketplace_id=MARKETPLACE, entity_type=entity_type, amazon_entity_id=entity_id, date=day,
        amazon_campaign_id=entity_id, clicks=clicks, spend=spend, sales=sales, orders=orders,
        impressions=clicks * 20, is_provisional=False))
    build.db.flush()


# ---------------- the schema can tell them apart ----------------
def test_the_same_amazon_id_in_two_ad_products_is_two_rows(db, build, profile):
    """Amazon's ids come from three independent services and nothing promises they never overlap.

    Before the uniqueness keys carried the ad product, an SB campaign sharing an id with an SP one
    would have overwritten it — and a day of one ad product's metrics would have silently replaced
    the other's.
    """
    _campaign(build, ad_product="sp", campaign_id="12345", name="Skyzz exact")
    _campaign(build, ad_product="sb", campaign_id="12345", name="Skyzz brand headline")

    rows = db.execute(select(PpcCampaign).where(PpcCampaign.amazon_campaign_id == "12345")
                      ).scalars().all()

    assert {r.ad_product: r.name for r in rows} == {"sp": "Skyzz exact", "sb": "Skyzz brand headline"}


def test_the_same_campaign_day_in_two_ad_products_is_two_rows(db, build, profile):
    day = build.today - timedelta(days=5)
    _day(build, ad_product="sp", entity_id="12345", day=day, spend=100.0, sales=400.0)
    _day(build, ad_product="sd", entity_id="12345", day=day, spend=50.0, sales=90.0)

    rows = db.execute(select(PpcMetricsDaily).where(
        PpcMetricsDaily.amazon_entity_id == "12345", PpcMetricsDaily.date == day)).scalars().all()

    assert sorted((r.ad_product, float(r.spend)) for r in rows) == [("sd", 50.0), ("sp", 100.0)]


def test_an_unlabelled_row_is_refused_rather_than_defaulted(db, build, profile):
    """No server default, deliberately. A default of 'sp' would mean the day someone wires up the
    Display loader and misses the column, every Display row is filed as Sponsored Products —
    silently, and permanently. Without one, that mistake is an error on the first row."""
    from sqlalchemy.exc import IntegrityError

    db.add(PpcCampaign(profile_id=profile.id, amazon_campaign_id="99999", name="Unlabelled"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_a_nonsense_ad_product_is_refused(db, build, profile):
    from sqlalchemy.exc import IntegrityError

    db.add(PpcCampaign(profile_id=profile.id, ad_product="xx", amazon_campaign_id="99999",
                       name="Nonsense"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_every_table_holding_account_data_carries_the_dimension(db):
    """A new Ppc table that holds per-ad-product data and forgets this column is the way the whole
    problem comes back, so the list is asserted rather than trusted to review."""
    from app.db.models import Base

    expected = {
        "ppc_campaigns", "ppc_ad_groups", "ppc_targets", "ppc_product_ads", "ppc_negatives",
        "ppc_metrics_daily", "ppc_metrics_restatements", "ppc_search_terms",
        "ppc_search_term_decisions", "ppc_placements", "ppc_purchased_products",
        "ppc_hourly_metrics", "ppc_recommendations", "ppc_actions", "ppc_action_outcomes",
        "ppc_predictions", "ppc_experiments", "ppc_experiment_units", "ppc_playbook", "ppc_alerts",
    }
    carrying = {name for name, table in Base.metadata.tables.items()
                if name.startswith("ppc_") and "ad_product" in table.c
                and not table.c.ad_product.nullable}

    assert carrying == expected


# ---------------- Sponsored Products answers do not move ----------------
def test_the_structure_sync_does_not_archive_another_ad_products_account(db, build, profile):
    """The worst of them. `_archive_missing` archives anything absent from the sync, and `seen` is
    built from a Sponsored Products read — so without the scope, the first sync after Brands and
    Display arrived would have archived the owner's entire SB and SD account in one pass."""
    sp = _campaign(build, ad_product="sp", campaign_id="SP1", name="Skyzz exact")
    sb = _campaign(build, ad_product="sb", campaign_id="SB1", name="Skyzz headline")
    sd = _campaign(build, ad_product="sd", campaign_id="SD1", name="Skyzz retargeting")
    for row in (sp, sb, sd):
        row.last_synced_at = build.scene_time if hasattr(build, "scene_time") else None
    db.flush()
    previous = __import__("app.services.common", fromlist=["utcnow"]).utcnow()

    # A complete Sponsored Products sync that saw only SP1.
    gone = structure._archive_missing(db, profile, {"campaigns_seen": {"SP1"}}, previous)

    assert gone == 0, "nothing has missed twice yet"
    # Now a second sync, still seeing only SP1: SP1 survives, and so must SB1 and SD1.
    later = previous + timedelta(hours=7)
    sp.last_synced_at = later
    db.flush()
    structure._archive_missing(db, profile, {"campaigns_seen": {"SP1"}}, later)

    assert db.get(PpcCampaign, sb.id).archived_at is None, "Sponsored Brands was archived"
    assert db.get(PpcCampaign, sd.id).archived_at is None, "Sponsored Display was archived"


def test_a_display_campaigns_spend_does_not_land_in_a_sponsored_products_rollup(db, build, profile):
    """Display sales include view-through attribution — credited to an impression nobody clicked.

    Adding those to Sponsored Products click-attributed sales and dividing by the combined spend
    produces an ACOS describing no real thing, which is why the separation matters more here than
    tidiness.
    """
    start, end = aggregates.window(profile, 30, today=build.today)
    day = end - timedelta(days=1)
    _day(build, ad_product="sp", entity_id="C1", day=day, spend=100.0, sales=400.0, clicks=200,
         orders=20)
    _day(build, ad_product="sd", entity_id="C1", day=day, spend=900.0, sales=150.0, clicks=50,
         orders=1)

    rolled = aggregates.by_entity(db, profile, "campaign", ad_product="sp", days=30,
                                  today=build.today)

    assert float(rolled["C1"].spend) == 100.0, "Display spend reached the SP rollup"
    assert float(rolled["C1"].sales) == 400.0, "Display sales reached the SP rollup"
    # And asking for Display gets Display, rather than nothing or everything.
    display = aggregates.by_entity(db, profile, "campaign", ad_product="sd", days=30,
                                   today=build.today)
    assert float(display["C1"].spend) == 900.0


def test_a_display_campaign_does_not_produce_a_sponsored_products_alert(db, build, profile):
    """The thresholds here are Sponsored Products thresholds, and Display's are not the same."""
    yesterday = build.today - timedelta(days=1)
    for n in range(1, 5):
        _day(build, ad_product="sd", entity_id="SD1", day=yesterday - timedelta(days=7 * n),
             spend=5.0, sales=40.0)
    _campaign(build, ad_product="sd", campaign_id="SD1", name="Skyzz retargeting")
    _day(build, ad_product="sd", entity_id="SD1", day=yesterday, spend=90.0, sales=10.0)

    assert alerts.daily_anomalies(db, profile, yesterday) == []


def test_display_hours_do_not_shape_the_sponsored_products_day(db, build, profile):
    """Dayparting reads one ad product's hours. Display's bid mechanics and its view-through sales
    make its hourly shape a different question, not more data about the same one."""
    end = build.today - timedelta(days=metrics.PROVISIONAL_DAYS)
    rows = []
    for n in range(28):
        at = end - timedelta(days=n)
        rows.extend(stream.Row(profile.amazon_profile_id, "C1", at, h, ad_product="sp",
                               clicks=20, orders=6, spend=10.0, sales=120.0, impressions=400)
                    for h in range(8, 22))
        # Display, spending heavily overnight and selling nothing — a shape the SP day never had.
        rows.extend(stream.Row(profile.amazon_profile_id, "D1", at, h, ad_product="sd",
                               clicks=40, orders=0, spend=60.0, sales=0.0, impressions=2000)
                    for h in (1, 2, 3))
    stream.load(db, rows)

    shaped = dayparting.shape(db, profile)

    assert [b.hour for b in shaped.by_hour if b.spend] == list(range(8, 22))
    assert shaped.by_hour[2].spend == 0.0, "a Display hour shaped the Sponsored Products day"
    assert dayparting.candidates(db, profile, shaped=shaped) == []


def test_search_term_decisions_do_not_collide_across_ad_products(db, build, profile):
    """Display has no search terms; Brands does. Two ad products' decisions on the same ad group id
    and term were one row before the key carried the dimension."""
    for product in ("sp", "sb"):
        db.add(PpcSearchTermDecision(
            amazon_profile_id=profile.amazon_profile_id, ad_product=product,
            amazon_campaign_id="C1", amazon_ad_group_id="G1", search_term="nicotine pouches",
            harvest_status="new" if product == "sp" else "negated"))
    db.flush()

    rows = db.execute(select(PpcSearchTermDecision)).scalars().all()

    assert {r.ad_product: r.harvest_status for r in rows} == {"sp": "new", "sb": "negated"}


# ---------------- candidates and rules carry it ----------------
def test_every_candidate_is_stamped_with_its_ad_product(db, build, profile):
    """Stamped once in `run_all` rather than by each analyser, so a tenth analyser cannot forget."""
    build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign)
    build.target(campaign, group)
    build.metrics(entity_type="target", entity_id="K1", clicks=60, orders=0, spend=30.0, sales=0.0)

    result = analysers.run_all(db, profile, today=build.today)

    produced = result["candidates"] + (result["held"] or []) + (result["in_cooldown"] or [])
    assert produced, "expected at least one candidate to check"
    assert {c.ad_product for c in produced} == {"sp"}


def test_a_recommendation_with_no_ad_product_is_refused(db, build, profile):
    """An unstamped candidate means something produced one outside `run_all`. Guessing 'sp' there is
    how a Display recommendation would be sent to a Sponsored Products endpoint."""
    from app.ppc import recommendations
    from app.ppc.candidates import Candidate

    orphan = Candidate(analyser="handwritten", change_type="bid_decrease", entity_type="target",
                       entity_id="K1", reason="no ad product on this one")
    assert orphan.ad_product == ""

    with pytest.raises(ValueError, match="no ad product"):
        recommendations.store_run(db, profile, {"candidates": [orphan], "per_analyser": {}})


def test_an_experiment_cannot_span_two_ad_products(db, build, profile):
    """Brief 8.2: the unit of assignment is "never mixed within one experiment".

    Two ad products in one experiment is the clearest form of mixing there is — they run in
    different auctions, and Display's sales include view-through attribution, so a single effect
    size would describe neither arm.
    """
    from app.ppc import experiments

    build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign)
    units = []
    for n in range(14):
        target = build.target(campaign, group, target_id=f"K{n}", text=f"term {n}")
        units.append(experiments.Candidate(
            entity_type="target", amazon_entity_id=f"K{n}", entity_id=target.id,
            clicks=400, orders=30, spend=200.0, sales=900.0, bid=1.0, label=f"term {n}",
            ad_product="sd" if n == 0 else "sp"))

    with pytest.raises(experiments.ExperimentError, match="across ad products"):
        experiments.design(db, profile, hypothesis="Lower bids convert as well",
                           change_type="bid_decrease", candidates=units, today=build.today)


def test_an_experiment_records_the_ad_product_it_tested(db, build, profile):
    """So its conclusion can be scoped when it becomes a playbook rule."""
    from app.db.models import PpcExperiment
    from app.ppc import experiments

    build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign)
    units = []
    for n in range(14):
        target = build.target(campaign, group, target_id=f"K{n}", text=f"term {n}")
        units.append(experiments.Candidate(
            entity_type="target", amazon_entity_id=f"K{n}", entity_id=target.id,
            clicks=400, orders=30, spend=200.0, sales=900.0, bid=1.0, label=f"term {n}"))

    experiments.design(db, profile, hypothesis="Lower bids convert as well",
                       change_type="bid_decrease", candidates=units, today=build.today)

    rows = db.execute(select(PpcExperiment)).scalars().all()
    assert rows and {r.ad_product for r in rows} == {"sp"}
    units_stored = db.execute(select(__import__("app.db.models",
                                                fromlist=["PpcExperimentUnit"]).PpcExperimentUnit)
                              ).scalars().all()
    assert units_stored and {u.ad_product for u in units_stored} == {"sp"}


def test_the_ad_product_names_read_like_english(db):
    from app.db.models import PPC_AD_PRODUCT_NAMES

    assert set(PPC_AD_PRODUCT_NAMES) == set(PPC_AD_PRODUCTS)
    assert PPC_AD_PRODUCT_NAMES["sd"] == "Sponsored Display"
