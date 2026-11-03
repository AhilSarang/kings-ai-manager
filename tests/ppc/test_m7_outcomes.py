"""P4 M7: measuring what a change actually did, and scoring the prediction that justified it.

The bias under test is toward "I don't know". A measurement that cannot distinguish a result from
luck, from an account-wide trend, or from a stockout has to say so — because every later milestone
treats these verdicts as ground truth.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (AmazonSalesDaily, PpcAction, PpcActionOutcome, PpcCalendarEvent,
                           PpcMetricsDaily, PpcPrediction, PpcRecommendation)
from app.ppc import metrics, outcomes, views
from app.services.common import utcnow
from tests.ppc.test_m5_analysers import MARKETPLACE, Build, account, profile  # noqa: F401


@pytest.fixture
def build(db, profile, account):
    return Build(db, profile, account)


def day_rows(build, entity_id, *, start, days, clicks, orders, spend, sales,
             entity_type="target", provisional=False):
    """One row per day, upserted — windows for different actions legitimately overlap.

    Every figure here is **per day**: `spend=10.0` over a 7-day window is 70.00 in total.

    The real loader upserts for the same reason, so a fixture that collided where production
    overwrites would be testing something the system never does.
    """
    from sqlalchemy.dialects.postgresql import insert

    rows = [{"amazon_profile_id": build.profile.amazon_profile_id, "ad_product": "sp",
             "marketplace_id": MARKETPLACE,
             "entity_type": entity_type, "amazon_entity_id": entity_id,
             "date": start + timedelta(days=n), "amazon_campaign_id": "C1",
             "amazon_ad_group_id": "G1", "impressions": clicks * 20, "clicks": clicks,
             "spend": spend, "sales": sales, "orders": orders, "units": orders,
             "is_provisional": provisional} for n in range(days)]
    keys = ("amazon_profile_id", "ad_product", "entity_type", "amazon_entity_id", "date")
    stmt = insert(PpcMetricsDaily).values(rows)
    build.db.execute(stmt.on_conflict_do_update(
        index_elements=list(keys), set_={c: stmt.excluded[c] for c in rows[0] if c not in keys}))
    build.db.flush()


def an_action(build, *, change_type="bid_decrease", days_ago=12, entity_id="K1",
              entity_type="target", asin=None, recommendation=None):
    row = PpcAction(ad_product="sp", profile_id=build.profile.id, change_type=change_type, entity_type=entity_type,
                    amazon_entity_id=entity_id, asin=asin,
                    recommendation_id=recommendation.id if recommendation else None,
                    before_value={"bid": 1.00}, after_value={"bid": 0.75},
                    executed_at=utcnow() - timedelta(days=days_ago))
    build.db.add(row)
    build.db.flush()
    return row


def surrounding(build, action, *, window=7, mine_before, mine_after, others_before, others_after):
    """Fill both windows for the changed entity and for one other entity as the baseline."""
    pre_start, _pre_end, post_start, _post_end = outcomes.windows_for(action, window)
    day_rows(build, action.amazon_entity_id, start=pre_start, days=window, **mine_before)
    day_rows(build, action.amazon_entity_id, start=post_start, days=window, **mine_after)
    day_rows(build, "OTHER1", start=pre_start, days=window, **others_before)
    day_rows(build, "OTHER1", start=post_start, days=window, **others_after)
    build.db.commit()


FLAT = dict(clicks=10, orders=1, spend=5.0, sales=25.0)


# ---------------- the windows ----------------
def test_the_day_of_the_change_belongs_to_neither_window(build):
    action = an_action(build, days_ago=12)
    pre_start, pre_end, post_start, post_end = outcomes.windows_for(action, 7)
    changed = action.executed_at.date()
    assert pre_end == changed - timedelta(days=1)
    assert post_start == changed + timedelta(days=1)
    assert (pre_end - pre_start).days == 6 and (post_end - post_start).days == 6
    assert changed not in (pre_start, pre_end, post_start, post_end)


def test_a_window_still_open_is_not_measured(build):
    action = an_action(build, days_ago=2)
    assert outcomes.evaluate(build.db, build.profile, action, 7) is None
    assert outcomes.needs_evaluating(build.db, build.profile) == []


def test_a_window_inside_the_provisional_zone_is_not_measured(build):
    """Yesterday's numbers are still moving, so a window ending yesterday cannot be read."""
    action = an_action(build, days_ago=8)
    assert outcomes.evaluate(build.db, build.profile, action, 7) is None


# ---------------- the baseline ----------------
def test_an_account_wide_fall_is_not_blamed_on_the_change(build):
    """Everything halved that fortnight; this target halved. That is not a result."""
    action = an_action(build, change_type="bid_decrease", days_ago=12)
    surrounding(build, action, window=7,
                mine_before=dict(clicks=20, orders=2, spend=20.0, sales=80.0),
                mine_after=dict(clicks=20, orders=1, spend=10.0, sales=40.0),
                others_before=dict(clicks=40, orders=4, spend=40.0, sales=160.0),
                others_after=dict(clicks=40, orders=2, spend=20.0, sales=80.0))
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    spend = next(e for e in got.baseline_adjusted_effect["effects"] if e["metric"] == "spend")
    assert spend["change"] == pytest.approx(-0.5, abs=0.01)
    assert spend["baseline_change"] == pytest.approx(-0.5, abs=0.01)
    assert spend["adjusted"] == pytest.approx(0.0, abs=0.01), "it did what everything else did"
    assert got.verdict == "neutral"


def test_a_cut_that_beats_the_trend_is_a_result(build):
    action = an_action(build, change_type="bid_decrease", days_ago=12)
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=2, spend=30.0, sales=60.0),
                mine_after=dict(clicks=25, orders=2, spend=12.0, sales=58.0),
                others_before=dict(clicks=40, orders=4, spend=40.0, sales=160.0),
                others_after=dict(clicks=40, orders=4, spend=40.0, sales=160.0))
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    assert got.verdict == "positive"
    assert "more than the rest of the account" in got.baseline_adjusted_effect["headline"]


def test_a_cut_that_cost_sales_is_reported_as_worse(build):
    action = an_action(build, change_type="bid_increase", days_ago=12)
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=30.0, sales=150.0),
                mine_after=dict(clicks=30, orders=2, spend=30.0, sales=75.0),
                others_before=dict(clicks=40, orders=4, spend=40.0, sales=160.0),
                others_after=dict(clicks=40, orders=4, spend=40.0, sales=160.0))
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    assert got.verdict == "negative"
    assert "opposite of the intent" in got.baseline_adjusted_effect["headline"]


def test_the_changed_entity_is_not_part_of_its_own_baseline(build):
    action = an_action(build, days_ago=12)
    surrounding(build, action, window=7, mine_before=FLAT, mine_after=FLAT,
                others_before=FLAT, others_after=FLAT)
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    baseline = got.pre_metrics["baseline"]
    assert baseline["clicks"] == 70, "only the other entity's clicks, not its own"


# ---------------- honest uncertainty ----------------
def test_too_little_traffic_is_inconclusive_and_says_which_number_was_short(build):
    action = an_action(build, days_ago=12)
    surrounding(build, action, window=7,
                mine_before=dict(clicks=1, orders=0, spend=1.0, sales=0.0),
                mine_after=dict(clicks=1, orders=0, spend=0.5, sales=0.0),
                others_before=FLAT, others_after=FLAT)
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    assert got.verdict == "inconclusive"
    assert "clicks" in got.baseline_adjusted_effect["reasons"][0]
    assert "least that could show anything" in got.baseline_adjusted_effect["reasons"][0]


def test_too_few_orders_cannot_judge_a_change_in_sales(build):
    action = an_action(build, change_type="bid_increase", days_ago=12)
    surrounding(build, action, window=7,
                mine_before=dict(clicks=20, orders=0, spend=20.0, sales=0.0),
                mine_after=dict(clicks=20, orders=0, spend=25.0, sales=20.0),
                others_before=FLAT, others_after=FLAT)
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    assert got.verdict == "inconclusive"
    assert "too few to judge" in " ".join(got.baseline_adjusted_effect["reasons"])


def test_a_stockout_in_the_window_makes_it_unreadable(build):
    listing = build.listing()
    action = an_action(build, days_ago=12, asin=listing.asin)
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=30.0, sales=120.0),
                mine_after=dict(clicks=25, orders=0, spend=10.0, sales=0.0),
                others_before=FLAT, others_after=FLAT)
    # No units sold on any day of the post window.
    _pre_start, _pre_end, post_start, post_end = outcomes.windows_for(action, 7)
    build.db.execute(AmazonSalesDaily.__table__.delete().where(
        AmazonSalesDaily.date >= post_start))
    for n in range(7):
        build.db.add(AmazonSalesDaily(marketplace_id=MARKETPLACE, asin=listing.asin,
                                      sku=listing.sku, date=post_start + timedelta(days=n),
                                      units=0, ordered_product_sales=0.0, currency="USD"))
    build.db.commit()
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    assert got.verdict == "inconclusive" and got.contaminated is True
    assert got.external_factors["possible_stockout"] is True
    assert "about stock rather than the change" in got.baseline_adjusted_effect["headline"]


def test_a_second_change_to_the_same_thing_spoils_both(build):
    action = an_action(build, days_ago=12)
    an_action(build, days_ago=9)          # another change to K1, inside the window
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=30.0, sales=120.0),
                mine_after=dict(clicks=30, orders=4, spend=15.0, sales=160.0),
                others_before=FLAT, others_after=FLAT)
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    assert got.verdict == "inconclusive"
    assert got.external_factors["other_changes_to_the_same_entity"] == 1
    assert "neither can be told apart" in got.baseline_adjusted_effect["headline"]


def test_prime_day_is_not_a_normal_week(build):
    action = an_action(build, days_ago=12)
    _pre, _pe, post_start, post_end = outcomes.windows_for(action, 7)
    build.db.add(PpcCalendarEvent(kind="prime_day", name="Prime Day", start_date=post_start,
                                  end_date=post_start + timedelta(days=1)))
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=30.0, sales=120.0),
                mine_after=dict(clicks=60, orders=9, spend=30.0, sales=400.0),
                others_before=FLAT, others_after=FLAT)
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    assert got.verdict == "inconclusive"
    assert "Prime Day" in got.baseline_adjusted_effect["headline"]


def test_a_tiny_difference_is_called_no_change(build):
    action = an_action(build, change_type="bid_decrease", days_ago=12)
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=30.0, sales=120.0),
                mine_after=dict(clicks=30, orders=3, spend=29.0, sales=120.0),
                others_before=FLAT, others_after=FLAT)
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    assert got.verdict == "neutral" and "barely moved" in got.baseline_adjusted_effect["headline"]


# ---------------- settling ----------------
def test_an_early_reading_is_marked_as_early_and_re_read_later(build):
    action = an_action(build, days_ago=12)
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=30.0, sales=120.0),
                mine_after=dict(clicks=25, orders=3, spend=12.0, sales=120.0),
                others_before=FLAT, others_after=FLAT)
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    assert got.baseline_adjusted_effect["settled"] is False
    # Still unsettled, so it comes back round for another read.
    assert (action, 7) in [(a, w) for a, w in outcomes.needs_evaluating(build.db, build.profile)]

    later = metrics.today_for(build.profile) + timedelta(days=40)
    again = outcomes.evaluate(build.db, build.profile, action, 7, today=later)
    assert again.id == got.id, "the same row is updated, not a second one added"
    assert again.baseline_adjusted_effect["settled"] is True
    # The 7-day window is done with. The 14 and 28-day ones have since come due, which is the
    # point of measuring the same change three times.
    pending = {w for _a, w in outcomes.needs_evaluating(build.db, build.profile, today=later)}
    assert 7 not in pending and pending == {14, 28}


def test_each_window_is_measured_once(build):
    action = an_action(build, days_ago=40)
    pre_start = action.executed_at.date() - timedelta(days=30)
    day_rows(build, "K1", start=pre_start, days=70, clicks=10, orders=1, spend=5.0, sales=25.0)
    day_rows(build, "OTHER1", start=pre_start, days=70, clicks=10, orders=1, spend=5.0, sales=25.0)
    build.db.commit()
    outcomes.evaluate_due(build.db, build.profile)
    rows = build.db.execute(select(PpcActionOutcome)).scalars().all()
    assert sorted(r.window_days for r in rows) == [7, 14, 28]


def test_a_rolled_back_change_is_not_measured(build):
    action = an_action(build, days_ago=12)
    action.rolled_back_at = utcnow()
    build.db.commit()
    assert outcomes.needs_evaluating(build.db, build.profile) == []


# ---------------- calibration ----------------
def _recommendation(build, *, estimate, low, high, confidence=0.75, change_type="bid_decrease"):
    row = PpcRecommendation(ad_product="sp", 
        profile_id=build.profile.id, change_type=change_type, entity_type="target",
        amazon_entity_id="K1", reason="x", status="executed", confidence=confidence,
        predicted_effect={"monthly_effect_estimate": estimate, "monthly_effect_low": low,
                          "monthly_effect_high": high})
    build.db.add(row)
    build.db.flush()
    return row


def test_a_prediction_is_scored_once_the_window_has_settled(build):
    recommendation = _recommendation(build, estimate=60.0, low=30.0, high=90.0)
    action = an_action(build, days_ago=50, recommendation=recommendation)
    # 10.00 a day before, 8.00 a day after: 70.00 against 56.00 over the window.
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=10.0, sales=40.0),
                mine_after=dict(clicks=25, orders=3, spend=8.0, sales=40.0),
                others_before=FLAT, others_after=FLAT)
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    assert got.baseline_adjusted_effect["settled"] is True
    scored = build.db.execute(select(PpcPrediction)).scalar_one()
    assert scored.change_type == "bid_decrease" and scored.metric == "spend"
    # Spend fell 14.00 across the week, which is 60.00 a month — spot on the estimate.
    assert float(scored.actual["monthly_effect"]) == pytest.approx(60.0, abs=1.0)
    assert scored.within_interval is True and abs(float(scored.error)) < 1.0
    assert float(scored.stated_confidence) == 0.75


def test_a_prediction_outside_its_own_range_is_recorded_as_such(build):
    recommendation = _recommendation(build, estimate=200.0, low=150.0, high=250.0)
    action = an_action(build, days_ago=50, recommendation=recommendation)
    # A fall of 0.20 a day is 1.40 across the week, about 6.00 a month — nowhere near 200.
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=10.0, sales=40.0),
                mine_after=dict(clicks=28, orders=3, spend=9.8, sales=40.0),
                others_before=FLAT, others_after=FLAT)
    outcomes.evaluate(build.db, build.profile, action, 7)
    scored = build.db.execute(select(PpcPrediction)).scalar_one()
    assert scored.within_interval is False
    assert float(scored.error) < 0, "it claimed far more than it delivered"


def test_an_unsettled_window_is_not_scored(build):
    recommendation = _recommendation(build, estimate=60.0, low=30.0, high=90.0)
    action = an_action(build, days_ago=12, recommendation=recommendation)
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=30.0, sales=120.0),
                mine_after=dict(clicks=25, orders=3, spend=16.0, sales=120.0),
                others_before=FLAT, others_after=FLAT)
    outcomes.evaluate(build.db, build.profile, action, 7)
    assert build.db.execute(select(PpcPrediction)).scalars().all() == [], \
        "a calibration built on numbers Amazon is still revising measures the revision"


def test_calibration_reports_by_change_type_and_says_when_it_is_too_early(build):
    for n in range(3):
        recommendation = _recommendation(build, estimate=60.0, low=30.0, high=90.0)
        action = an_action(build, days_ago=50 + n * 20, recommendation=recommendation,
                          entity_id=f"K{n}")
        surrounding(build, action, window=7,
                    mine_before=dict(clicks=30, orders=3, spend=10.0, sales=40.0),
                    mine_after=dict(clicks=25, orders=3, spend=8.0, sales=40.0),
                    others_before=FLAT, others_after=FLAT)
        outcomes.evaluate(build.db, build.profile, action, 7)
    got = outcomes.calibration(build.db)
    assert got["scored"] == 3 and got["enough_to_judge"] is False
    row = got["by_change_type"]["bid_decrease"]
    assert row["scored"] == 3 and row["inside_range_pct"] == 100.0
    assert row["right_direction_pct"] == 100.0 and row["stated_confidence"] == 0.75


# ---------------- what the owner sees ----------------
def test_the_outcomes_view_explains_itself_before_anything_is_measured(build):
    text = views.outcomes_summary(build.db)
    assert "Nothing measured yet" in text and "7, 14 and 28 days" in text


def test_the_outcomes_view_marks_an_early_read_as_early(build):
    action = an_action(build, days_ago=12)
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=30.0, sales=120.0),
                mine_after=dict(clicks=25, orders=3, spend=12.0, sales=120.0),
                others_before=FLAT, others_after=FLAT)
    outcomes.evaluate(build.db, build.profile, action, 7)
    build.db.commit()
    text = views.outcomes_summary(build.db)
    assert "early read" in text and "Amazon still revising" in text
    assert "rest of the account taken out" in text


def test_the_calibration_view_says_when_there_is_nothing_to_report(build):
    assert "No predictions scored yet" in views.calibration_summary(build.db)


def test_the_job_measures_everything_that_is_ready(build, monkeypatch):
    from pydantic import SecretStr

    from app.config import get_settings
    from app.jobs import queue
    from tests.conftest import run_jobs

    s = get_settings()
    monkeypatch.setattr(s, "ads_lwa_client_id", "c", raising=False)
    monkeypatch.setattr(s, "ads_lwa_client_secret", SecretStr("s"), raising=False)
    monkeypatch.setattr(s, "ads_lwa_refresh_token", SecretStr("r"), raising=False)
    action = an_action(build, days_ago=12)
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=30.0, sales=120.0),
                mine_after=dict(clicks=25, orders=3, spend=12.0, sales=120.0),
                others_before=FLAT, others_after=FLAT)
    queue.enqueue(build.db, "ppc_measure_outcomes", {})
    build.db.commit()
    assert run_jobs(types=["ppc_measure_outcomes"]) == 1
    assert build.db.execute(select(PpcActionOutcome)).scalars().all()


def test_a_price_change_inside_the_window_makes_acos_unreadable(build):
    """ACOS is spend over sales, so moving the price moves ACOS without touching the advertising."""
    listing = build.listing()
    action = an_action(build, change_type="placement_adjustment", days_ago=12, asin=listing.asin,
                       entity_type="campaign", entity_id="C1")
    _pre_start, _pre_end, post_start, post_end = outcomes.windows_for(action, 7)
    build.db.execute(AmazonSalesDaily.__table__.delete().where(
        AmazonSalesDaily.date >= post_start))
    for n in range(7):
        # Five units a day, but the price steps up halfway through the window.
        price = 19.99 if n < 4 else 24.99
        build.db.add(AmazonSalesDaily(marketplace_id=MARKETPLACE, asin=listing.asin,
                                      sku=listing.sku, date=post_start + timedelta(days=n),
                                      units=5, ordered_product_sales=price * 5, currency="USD"))
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=30.0, sales=120.0),
                mine_after=dict(clicks=30, orders=3, spend=20.0, sales=150.0),
                others_before=FLAT, others_after=FLAT)
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    assert got.contaminated is True and got.verdict == "inconclusive"
    assert got.external_factors["price_moved"]["low"] == 19.99
    assert "selling price moved" in got.baseline_adjusted_effect["headline"]


def test_a_listing_edited_mid_window_is_recorded(build):
    from app.db.models import AmazonListingVersion

    listing = build.listing()
    action = an_action(build, days_ago=12, asin=listing.asin)
    _pre, _pe, post_start, _post_end = outcomes.windows_for(action, 7)
    build.db.add(AmazonListingVersion(
        listing_id=listing.id, version_number=2, source="amazon_sync", attributes={},
        created_at=utcnow() - timedelta(days=(metrics.today_for(build.profile) - post_start).days)))
    surrounding(build, action, window=7,
                mine_before=dict(clicks=30, orders=3, spend=30.0, sales=120.0),
                mine_after=dict(clicks=30, orders=3, spend=20.0, sales=120.0),
                others_before=FLAT, others_after=FLAT)
    got = outcomes.evaluate(build.db, build.profile, action, 7)
    assert got.external_factors.get("listing_changed", 0) >= 1
