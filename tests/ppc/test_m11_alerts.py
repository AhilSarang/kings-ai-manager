"""P4 M11: alerts, intraday budget pace, and the shape of the day.

Three things are being defended here, and they are the three that would actually go wrong.

**The budget-usage contract.** It is the only Ads endpoint in this phase that is lower-case and v1,
its 207 puts `success`/`error` at the top level rather than under a collection name, and it accepts
1 to 100 ids. Every one of those is a detail I would otherwise have got from memory, and this phase
has already shipped two mappings that were wrong in exactly that way with green tests behind them.

**That an alert is a state.** Raising the same thing twice is the failure that makes people stop
reading alerts, so the dedupe, the refresh, the mute and the self-closing are all tested directly.

**That dayparting refuses to be confident on thin data.** An hour is a twenty-fourth of the data, and
the easiest bug to write here is one that calls three good Tuesdays a pattern.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import PpcAlert, PpcHourlyMetric
from app.ppc import alerts, budget, dayparting, metrics as metrics_module, stream, views
from tests.ppc.test_m5_analysers import MARKETPLACE, Build, account, profile  # noqa: F401


@pytest.fixture
def build(db, profile, account):
    return Build(db, profile, account)


def _row(*args, ad_product="sp", **kwargs):
    """A Sponsored Products hourly row. `ad_product` is keyword-only and required on `stream.Row`,
    deliberately — Stream V2 is one dataset across all three ad products, so a row that does not say
    which one it is would be filed under whatever the default happened to be."""
    return stream.Row(*args, ad_product=ad_product, **kwargs)


class FakeAds:
    """Answers the budget-usage endpoint, and records exactly what it was asked."""

    def __init__(self, usage=None, errors=None):
        self.usage = usage or {}
        self.errors = errors or {}
        self.calls = []

    def post(self, path, *, json=None, params=None, profile_id=None, content_type=None,
             accept=None, operation=None):
        self.calls.append({"path": path, "json": json, "content_type": content_type,
                           "accept": accept, "operation": operation, "profile_id": profile_id})
        ids = (json or {}).get("campaignIds") or []
        success, failed = [], []
        for index, campaign_id in enumerate(ids):
            if campaign_id in self.errors:
                failed.append({"campaignId": campaign_id, "code": "NOT_FOUND",
                               "details": self.errors[campaign_id], "index": index})
            elif campaign_id in self.usage:
                used, cap, stamp = self.usage[campaign_id]
                success.append({"campaignId": campaign_id, "budget": cap,
                                "budgetUsagePercent": used, "index": index,
                                "usageUpdatedTimestamp": stamp})
        return {"success": success, "error": failed}


def _now(profile, hour):
    """A local datetime in the profile's own clock, at a given hour today."""
    from app.ppc import metrics

    today = metrics.today_for(profile)
    return datetime.combine(today, datetime.min.time(),
                            tzinfo=metrics.zone_for(profile)).replace(hour=hour)


def _stamp(profile, hour, minutes_ago=5):
    return (_now(profile, hour) - timedelta(minutes=minutes_ago)).astimezone(
        timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------- the endpoint contract ----------------
def test_the_budget_endpoint_is_called_the_way_the_spec_describes(db, build, profile):
    build.campaign(campaign_id="C1", name="Skyzz exact", budget=40.0)
    client = FakeAds(usage={"C1": (62.5, 40.0, _stamp(profile, 11))})

    pace = budget.poll(db, profile, client=client, at=_now(profile, 11))

    call = client.calls[0]
    assert call["path"] == "/sp/campaigns/budget/usage"
    # Lower case and v1. Every other Sponsored Products endpoint in this codebase is camelCase v3,
    # which is exactly why this one is asserted character by character.
    assert call["content_type"] == "application/vnd.spcampaignbudgetusage.v1+json"
    assert call["accept"] == "application/vnd.spcampaignbudgetusage.v1+json"
    assert call["json"] == {"campaignIds": ["C1"]}
    assert call["operation"] == "spCampaignsBudgetUsage"
    assert [r.used_pct for r in pace.readings] == [62.5]
    assert pace.readings[0].spent == 25.0


def test_the_success_and_error_lists_are_read_from_the_top_level(db, build, profile):
    """The campaign-management 207s nest under a collection name; this one does not.

    Reading it with `apply.read_multi_status` would find nothing and report every campaign as
    missing, which is a silent zero rather than a crash.
    """
    build.campaign(campaign_id="C1", budget=40.0)
    build.campaign(campaign_id="C2", name="Broad", budget=25.0)
    client = FakeAds(usage={"C1": (30.0, 40.0, _stamp(profile, 9))},
                     errors={"C2": "No such campaign"})

    pace = budget.poll(db, profile, client=client, at=_now(profile, 9))

    assert [r.amazon_campaign_id for r in pace.readings] == ["C1"]
    assert pace.errors[0]["amazon_campaign_id"] == "C2"
    assert pace.errors[0]["code"] == "NOT_FOUND"


def test_more_than_a_hundred_campaigns_go_out_in_batches(db, build, profile):
    """The spec caps `campaignIds` at 100. Sending 150 would be a 422 for the whole account."""
    usage = {}
    for n in range(150):
        build.campaign(campaign_id=f"C{n}", name=f"Campaign {n}", budget=10.0 + n)
        usage[f"C{n}"] = (10.0, 10.0 + n, _stamp(profile, 14))
    client = FakeAds(usage=usage)

    pace = budget.poll(db, profile, client=client, at=_now(profile, 14))

    assert [len(c["json"]["campaignIds"]) for c in client.calls] == [100, 50]
    assert len(pace.readings) == 150
    # Biggest budgets first, so a later batch failing costs us the least important readings.
    first = client.calls[0]["json"]["campaignIds"]
    assert first[0] == "C149"


def test_a_campaign_with_no_budget_is_never_asked_about(db, build, profile):
    build.campaign(campaign_id="C1", budget=40.0)
    build.campaign(campaign_id="C2", name="No budget", budget=None)
    build.campaign(campaign_id="C3", name="Paused", budget=30.0, state="paused")
    client = FakeAds(usage={"C1": (10.0, 40.0, _stamp(profile, 8))})

    budget.poll(db, profile, client=client, at=_now(profile, 8))

    assert client.calls[0]["json"]["campaignIds"] == ["C1"]


def test_an_account_with_no_live_campaigns_sends_no_request_at_all(db, build, profile):
    """An empty `campaignIds` is a 422. The right number of calls for nothing is zero."""
    client = FakeAds()

    pace = budget.poll(db, profile, client=client, at=_now(profile, 8))

    assert client.calls == []
    assert pace.readings == []
    assert "No live campaigns" in budget.summary(pace)


def test_a_stale_reading_is_not_treated_as_current(db, build, profile):
    build.campaign(campaign_id="C1", budget=40.0)
    client = FakeAds(usage={"C1": (98.0, 40.0, _stamp(profile, 11, minutes_ago=240))})

    pace = budget.poll(db, profile, client=client, at=_now(profile, 11))

    assert pace.readings[0].is_stale(pace.at) is True
    assert pace.usable == []
    # And so it cannot raise an alert either: a four-hour-old 98% might be yesterday's news.
    assert budget.exhausted_early(pace) == []


def test_a_reading_with_no_timestamp_counts_as_stale(db, build, profile):
    build.campaign(campaign_id="C1", budget=40.0)
    client = FakeAds(usage={"C1": (99.0, 40.0, None)})

    pace = budget.poll(db, profile, client=client, at=_now(profile, 10))

    assert pace.readings[0].is_stale(pace.at) is True


def test_yesterdays_reading_is_not_believed_five_minutes_into_a_new_day(db, build, profile):
    """The reading that would otherwise fire the worst possible false alarm.

    `budgetUsagePercent` is usage within Amazon's *current budget day*, which resets at local
    midnight. At 00:05 a snapshot taken at 23:50 is fifteen minutes old — fresh by any age test —
    and says 100% of a budget that has just been handed back in full.
    """
    build.campaign(campaign_id="C1", name="Skyzz exact", budget=40.0)
    last_night = (_now(profile, 23) - timedelta(minutes=10)).astimezone(timezone.utc)
    client = FakeAds(usage={"C1": (100.0, 40.0, last_night.isoformat().replace("+00:00", "Z"))})

    just_after_midnight = _now(profile, 0) + timedelta(days=1, minutes=5)
    pace = budget.poll(db, profile, client=client, at=just_after_midnight)

    assert pace.readings[0].age(pace.at) < budget.STALE_AFTER      # fresh by the clock
    assert pace.usable == []                                       # and useless anyway
    assert alerts.budget_findings(db, profile, pace) == []
    assert "too old to quote" in budget.summary(pace)


def test_one_failing_batch_does_not_lose_the_readings_already_in_hand(db, build, profile):
    """The descending-budget ordering only means anything if the early batches survive a late one."""
    from app.ppc.errors import AdsError

    usage = {}
    for n in range(150):
        build.campaign(campaign_id=f"C{n}", name=f"Campaign {n}", budget=10.0 + n)
        usage[f"C{n}"] = (30.0, 10.0 + n, _stamp(profile, 14))

    class FailsOnSecondBatch(FakeAds):
        def post(self, *args, **kwargs):
            if len(self.calls) >= 1:
                self.calls.append({"failed": True})
                raise AdsError("Amazon said no")
            return super().post(*args, **kwargs)

    client = FailsOnSecondBatch(usage=usage)
    pace = budget.poll(db, profile, client=client, at=_now(profile, 14))

    assert len(pace.readings) == 100           # the hundred biggest budgets, kept
    assert pace.errors[0]["code"] == "batch_failed"
    assert db.scalar(__import__("sqlalchemy").select(
        __import__("sqlalchemy").func.count()).select_from(PpcHourlyMetric)) == 100


def test_an_auth_failure_is_not_swallowed_as_a_batch_problem(db, build, profile):
    """Authorisation is account-wide. The job's own handler notices it once and stops asking."""
    from app.ppc.errors import AdsAuthError

    build.campaign(campaign_id="C1", budget=40.0)

    class Unauthorised(FakeAds):
        def post(self, *args, **kwargs):
            raise AdsAuthError("the refresh token was revoked")

    with pytest.raises(AdsAuthError):
        budget.poll(db, profile, client=Unauthorised(), at=_now(profile, 14))


def test_readings_are_stored_on_the_hour_they_describe(db, build, profile):
    build.campaign(campaign_id="C1", budget=40.0)
    client = FakeAds(usage={"C1": (41.0, 40.0, _stamp(profile, 13))})

    budget.poll(db, profile, client=client, at=_now(profile, 13))

    row = db.execute(select(PpcHourlyMetric)).scalars().one()
    assert (row.hour, float(row.budget_consumed_pct)) == (13, 41.0)
    assert row.weekday == row.date.weekday()


def test_a_second_reading_in_the_same_hour_replaces_the_first(db, build, profile):
    build.campaign(campaign_id="C1", budget=40.0)
    for used in (20.0, 35.0):
        budget.poll(db, profile, client=FakeAds(usage={"C1": (used, 40.0, _stamp(profile, 13))}),
                    at=_now(profile, 13))

    rows = db.execute(select(PpcHourlyMetric)).scalars().all()
    assert len(rows) == 1 and float(rows[0].budget_consumed_pct) == 35.0


def test_storing_a_budget_reading_does_not_wipe_stream_metrics(db, build, profile):
    """The two writers share a row. An upsert that set every column would zero the hourly spend."""
    build.campaign(campaign_id="C1", budget=40.0)
    today = _now(profile, 13).date()
    stream.load(db, [_row(profile.amazon_profile_id, "C1", today, 13, clicks=12, spend=9.40,
                                sales=31.0, orders=2, impressions=400)])

    budget.poll(db, profile, client=FakeAds(usage={"C1": (23.5, 40.0, _stamp(profile, 13))}),
                at=_now(profile, 13))

    row = db.execute(select(PpcHourlyMetric)).scalars().one()
    assert (row.clicks, float(row.spend), row.orders) == (12, 9.40, 2)
    assert float(row.budget_consumed_pct) == 23.5


# ---------------- the day's own clock ----------------
def test_the_day_elapsed_is_measured_in_the_marketplaces_clock(profile):
    assert budget.elapsed_fraction(profile, _now(profile, 0)) == 0.0
    assert budget.elapsed_fraction(profile, _now(profile, 12)) == pytest.approx(0.5, abs=0.02)
    assert budget.elapsed_fraction(profile, _now(profile, 23)) == pytest.approx(0.958, abs=0.02)


def test_the_elapsed_fraction_survives_a_short_day(db, profile):
    """A spring-forward day is 23 hours long. A fixed /86400 divisor misreports every campaign."""
    from zoneinfo import ZoneInfo

    profile.timezone = "America/Los_Angeles"
    db.flush()
    # 2026-03-08 is the US spring-forward day: 2am never happens, so by noon only eleven real hours
    # have passed out of the day's twenty-three. The clock says half past; the money says less.
    short_day = datetime(2026, 3, 8, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    fraction = budget.elapsed_fraction(profile, short_day)

    assert fraction == pytest.approx(11 / 23, abs=0.001)
    assert fraction < 0.5          # and not the 0.5 that plain wall-clock subtraction would give


# ---------------- early exhaustion ----------------
def test_exhausting_the_budget_at_noon_is_an_alert(db, build, profile):
    build.campaign(campaign_id="C1", name="Skyzz exact", budget=40.0)
    client = FakeAds(usage={"C1": (100.0, 40.0, _stamp(profile, 11))})

    pace = budget.poll(db, profile, client=client, at=_now(profile, 11))
    findings = alerts.budget_findings(db, profile, pace)

    assert [f.type for f in findings] == ["budget_exhausted_early"]
    assert "Skyzz exact" in findings[0].message
    assert findings[0].severity == "high"       # before 40% of the day has gone


def test_exhausting_the_budget_at_eleven_at_night_is_not(db, build, profile):
    """A campaign that spends its day's budget by the end of the day has done its job."""
    build.campaign(campaign_id="C1", budget=40.0)
    client = FakeAds(usage={"C1": (100.0, 40.0, _stamp(profile, 23))})

    pace = budget.poll(db, profile, client=client, at=_now(profile, 23))

    assert alerts.budget_findings(db, profile, pace) == []


def test_a_campaign_merely_pacing_fast_is_not_an_alert(db, build, profile):
    build.campaign(campaign_id="C1", budget=40.0)
    client = FakeAds(usage={"C1": (70.0, 40.0, _stamp(profile, 11))})

    pace = budget.poll(db, profile, client=client, at=_now(profile, 11))

    assert alerts.budget_findings(db, profile, pace) == []
    assert "70%" in budget.summary(pace) or "of today's budget is spent" in budget.summary(pace)


# ---------------- daily anomalies ----------------
def _baseline(build, campaign_id, *, day, spend, sales, clicks, weeks=4, orders=2):
    """The same weekday as `day`, several weeks running.

    Anchored on the day being measured, not on today. Anchoring it on today builds a baseline for
    the wrong weekday, which is the mistake the detector exists to prevent.
    """
    for n in range(1, weeks + 1):
        build.day_of_spend(campaign_id, day=day - timedelta(days=7 * n), spend=spend,
                           sales=sales, clicks=clicks, orders=orders)


def test_a_spend_spike_is_measured_against_the_same_weekday(db, build, profile):
    build.campaign(campaign_id="C1", name="Skyzz exact")
    yesterday = build.today - timedelta(days=1)
    _baseline(build, "C1", day=yesterday, spend=20.0, sales=80.0, clicks=40)
    build.day_of_spend("C1", day=yesterday, spend=60.0, sales=90.0, clicks=100, orders=3)

    findings = alerts.daily_anomalies(db, profile, yesterday)

    spikes = [f for f in findings if f.type == "spend_spike"]
    assert len(spikes) == 1
    assert spikes[0].details["change_pct"] == 200.0
    assert spikes[0].severity == "high"
    assert "above its usual" in spikes[0].message


def test_a_quiet_weekday_is_not_a_spike_against_a_busy_one(db, build, profile):
    """The bug this prevents: comparing Tuesday to a trailing average that includes Saturday."""
    build.campaign(campaign_id="C1")
    yesterday = build.today - timedelta(days=1)
    # Every other day of the fortnight is busy; the same weekday is not.
    for n in range(1, 15):
        day = build.today - timedelta(days=n)
        same_weekday = day.weekday() == yesterday.weekday()
        build.day_of_spend("C1", day=day, spend=30.0 if same_weekday else 5.0,
                           sales=120.0, clicks=60)

    findings = alerts.daily_anomalies(db, profile, yesterday)

    assert [f.type for f in findings] == []


def test_a_spike_needs_more_than_one_week_to_compare_against(db, build, profile):
    build.campaign(campaign_id="C1")
    yesterday = build.today - timedelta(days=1)
    build.day_of_spend("C1", day=yesterday - timedelta(days=7), spend=5.0, sales=20.0, clicks=10)
    build.day_of_spend("C1", day=yesterday, spend=50.0, sales=60.0, clicks=80)

    assert alerts.daily_anomalies(db, profile, yesterday) == []


def test_small_money_is_never_an_alert_however_large_the_percentage(db, build, profile):
    build.campaign(campaign_id="C1")
    yesterday = build.today - timedelta(days=1)
    _baseline(build, "C1", day=yesterday, spend=0.20, sales=2.0, clicks=2)
    build.day_of_spend("C1", day=yesterday, spend=1.80, sales=0.0, clicks=4)

    assert alerts.daily_anomalies(db, profile, yesterday) == []


def test_zero_sales_waits_for_the_day_to_stop_being_provisional(db, build, profile):
    """Yesterday's zero is usually yesterday's conversions not having arrived."""
    build.campaign(campaign_id="C1", name="Skyzz exact")
    yesterday = build.today - timedelta(days=1)
    build.day_of_spend("C1", day=yesterday, spend=45.0, sales=0.0, clicks=70)

    assert [f.type for f in alerts.daily_anomalies(db, profile, yesterday)] == []

    settled = build.today - timedelta(days=5)
    build.day_of_spend("C1", day=settled, spend=45.0, sales=0.0, clicks=70)
    found = [f for f in alerts.daily_anomalies(db, profile, settled)
             if f.type == "zero_sales_with_spend"]
    assert len(found) == 1
    assert "sold nothing" in found[0].message


def test_zero_sales_fires_the_way_the_job_actually_calls_it(db, build, profile):
    """The whole alert was dead in production and the tests did not notice.

    Every test above named a day explicitly. The job does not: `alerts.run` calls
    `daily_anomalies(session, profile)` with no day, which took the *newest* day we hold — always
    yesterday, always provisional, always skipped. So acceptance 13's zero-sale alert could never
    fire on the real schedule, however much money a campaign wasted.
    """
    build.campaign(campaign_id="C1", name="Skyzz exact")
    # Exactly the production shape: the morning load has filled in every day up to yesterday.
    for n in range(1, 10):
        build.day_of_spend("C1", day=build.today - timedelta(days=n), spend=45.0, sales=0.0,
                           clicks=70)

    found = alerts.daily_anomalies(db, profile)       # no day named — the job's own call

    waste = [f for f in found if f.type == "zero_sales_with_spend"]
    assert len(waste) == 1, "the newest settled day should still be checked"
    assert waste[0].details["day"] == (build.today - timedelta(days=3)).isoformat()
    assert waste[0].severity == "high"

    sweep = alerts.run(db, profile, intraday=False)
    assert "zero_sales_with_spend" in {f.type for f in sweep.findings}


def test_zero_sales_is_not_claimed_when_every_day_we_hold_is_provisional(db, build, profile):
    """A fresh account with two days of data has nothing settled to judge, and says nothing."""
    build.campaign(campaign_id="C1", name="Skyzz exact")
    for n in (1, 2):
        build.day_of_spend("C1", day=build.today - timedelta(days=n), spend=45.0, sales=0.0,
                           clicks=70)

    assert [f for f in alerts.daily_anomalies(db, profile)
            if f.type == "zero_sales_with_spend"] == []


def test_a_cpc_jump_needs_enough_clicks_on_both_sides(db, build, profile):
    build.campaign(campaign_id="C1", name="Skyzz exact")
    day = build.today - timedelta(days=4)
    for n in range(1, 5):
        build.day_of_spend("C1", day=day - timedelta(days=7 * n), spend=20.0, sales=90.0, clicks=40)
    build.day_of_spend("C1", day=day, spend=40.0, sales=150.0, clicks=40, orders=4)

    jumps = [f for f in alerts.daily_anomalies(db, profile, day) if f.type == "cpc_jump"]

    assert len(jumps) == 1
    assert jumps[0].details["cpc"] == 1.0 and jumps[0].details["baseline_cpc"] == 0.5


# ---------------- Amazon's own serving verdicts ----------------
def test_a_campaign_amazon_says_is_rejected_is_reported(db, build, profile):
    campaign = build.campaign(campaign_id="C1", name="Skyzz exact")
    campaign.serving_status = "REJECTED"
    db.flush()

    findings = alerts.serving_findings(db, profile)

    assert [f.type for f in findings] == ["ad_serving_problem"]
    assert findings[0].severity == "high"
    assert "rejected" in findings[0].message


def test_an_account_level_block_is_one_alert_not_sixty(db, build, profile):
    """Every campaign reports the same account status. Sixty copies would bury the one sentence."""
    for n in range(8):
        campaign = build.campaign(campaign_id=f"C{n}", name=f"Campaign {n}")
        campaign.serving_status = "ACCOUNT_OUT_OF_BUDGET"
    db.flush()

    findings = alerts.serving_findings(db, profile)

    assert [f.type for f in findings] == ["account_spend_cap"]
    assert findings[0].details["affected"] == 8
    assert "stopped across the account" in findings[0].message


def test_a_campaign_out_of_budget_is_left_to_the_budget_alert(db, build, profile):
    """CAMPAIGN_OUT_OF_BUDGET as a serving problem would be a vague duplicate of a precise alert."""
    campaign = build.campaign(campaign_id="C1")
    campaign.serving_status = "CAMPAIGN_OUT_OF_BUDGET"
    db.flush()

    assert alerts.serving_findings(db, profile) == []


def test_an_enabled_serving_status_is_not_news(db, build, profile):
    campaign = build.campaign(campaign_id="C1")
    campaign.serving_status = "CAMPAIGN_STATUS_ENABLED"
    db.flush()

    assert alerts.serving_findings(db, profile) == []


def test_a_serving_status_amazon_invented_later_is_reported_not_dropped(db, build, profile):
    """Amazon adds to this enum. Ignoring an unknown value would hide a real outage."""
    campaign = build.campaign(campaign_id="C1", name="Skyzz exact")
    campaign.serving_status = "SOME_STATUS_FROM_NEXT_YEAR"
    db.flush()

    findings = alerts.serving_findings(db, profile)

    assert len(findings) == 1
    assert findings[0].severity == "low"
    assert "some status from next year" in findings[0].message


def test_an_out_of_stock_product_ad_is_found(db, build, profile):
    campaign = build.campaign(campaign_id="C1")
    build.ad_group(campaign)
    ad = db.execute(select(__import__("app.db.models", fromlist=["PpcProductAd"]).PpcProductAd)
                    ).scalars().one()
    ad.serving_status = "AD_OUT_OF_STOCK"
    db.flush()

    findings = alerts.serving_findings(db, profile)

    assert [(f.entity_type, f.severity) for f in findings] == [("product_ad", "high")]
    assert "out of stock" in findings[0].message


# ---------------- an alert is a state, not an event ----------------
def test_the_same_condition_twice_is_one_alert_with_a_count(db, build, profile):
    campaign = build.campaign(campaign_id="C1", name="Skyzz exact")
    campaign.serving_status = "REJECTED"
    db.flush()

    first = alerts.run(db, profile, intraday=False)
    second = alerts.run(db, profile, intraday=False)

    rows = db.execute(select(PpcAlert)).scalars().all()
    assert len(rows) == 1
    assert rows[0].details["seen_count"] == 2
    assert first.raised[0].id == second.raised[0].id


def test_an_alert_closes_itself_when_the_condition_goes(db, build, profile):
    campaign = build.campaign(campaign_id="C1", name="Skyzz exact")
    campaign.serving_status = "REJECTED"
    db.flush()
    alerts.run(db, profile, intraday=False)

    campaign.serving_status = "CAMPAIGN_STATUS_ENABLED"
    db.flush()
    sweep = alerts.run(db, profile, intraday=False)

    row = db.execute(select(PpcAlert)).scalars().one()
    assert row.status == "resolved" and row.resolved_at is not None
    assert [r.id for r in sweep.resolved] == [row.id]


def test_a_check_that_failed_does_not_close_the_alerts_it_never_looked_at(db, build, profile,
                                                                         monkeypatch):
    """Silence about something is not evidence that it is fixed."""
    campaign = build.campaign(campaign_id="C1", name="Skyzz exact")
    campaign.serving_status = "REJECTED"
    db.flush()
    alerts.run(db, profile, intraday=False)

    def explode(*args, **kwargs):
        raise RuntimeError("Amazon fell over")

    monkeypatch.setattr(alerts, "serving_findings", explode)
    sweep = alerts.run(db, profile, intraday=False)

    assert sweep.errors and "Amazon fell over" in sweep.errors[0]
    assert db.execute(select(PpcAlert)).scalars().one().status == "open"


def test_a_muted_alert_stays_muted_even_as_it_worsens(db, build, profile):
    campaign = build.campaign(campaign_id="C1", name="Skyzz exact")
    campaign.serving_status = "REJECTED"
    db.flush()
    row = alerts.run(db, profile, intraday=False).raised[0]
    row.status = "muted"
    db.flush()

    alerts.run(db, profile, intraday=False)

    assert db.execute(select(PpcAlert)).scalars().one().status == "muted"


def test_an_evening_sweep_does_not_give_a_false_all_clear_on_a_dark_campaign(db, build, profile):
    """The worst possible resolve: `PPC ALERTS` saying nothing is wrong while the campaign is dark.

    `exhausted_early` returns nothing after three quarters of the day, which is right for raising —
    going dark at 11pm is not news. Reading that silence as "fixed" closed the 11am alert about a
    campaign that was still out of budget.
    """
    build.campaign(campaign_id="C1", name="Skyzz exact", budget=40.0)
    at_eleven = _now(profile, 11)
    client = FakeAds(usage={"C1": (100.0, 40.0, _stamp(profile, 11))})
    morning = alerts.run(db, profile, at=at_eleven, client=client)
    assert [f.type for f in morning.findings] == ["budget_exhausted_early"]

    # 8pm, same 100% reading: the campaign has been dark for nine hours.
    evening = alerts.run(db, profile, at=_now(profile, 20),
                         client=FakeAds(usage={"C1": (100.0, 40.0, _stamp(profile, 20))}))

    assert "budget_exhausted_early" not in evening.checked, "we did not look, so we cannot resolve"
    row = db.execute(select(PpcAlert).where(PpcAlert.type == "budget_exhausted_early")).scalars().one()
    assert row.status == "open"
    assert row.id not in [r.id for r in evening.resolved]
    assert "Skyzz exact" in views.alerts_summary(db)


def test_a_budget_alert_still_closes_when_the_budget_is_raised(db, build, profile):
    """The flip side: inside the window, a campaign no longer out of budget does get its all-clear."""
    build.campaign(campaign_id="C1", name="Skyzz exact", budget=40.0)
    alerts.run(db, profile, at=_now(profile, 11),
               client=FakeAds(usage={"C1": (100.0, 40.0, _stamp(profile, 11))}))

    sweep = alerts.run(db, profile, at=_now(profile, 13),
                       client=FakeAds(usage={"C1": (45.0, 90.0, _stamp(profile, 13))}))

    assert "budget_exhausted_early" in sweep.checked
    row = db.execute(select(PpcAlert).where(PpcAlert.type == "budget_exhausted_early")).scalars().one()
    assert row.status == "resolved"


def test_a_muted_alert_is_still_listed_as_open(db, build, profile):
    """Muting means "stop messaging me", not "pretend it is fixed" — and the reply says so."""
    campaign = build.campaign(campaign_id="C1", name="Skyzz exact")
    campaign.serving_status = "REJECTED"
    db.flush()
    row = alerts.run(db, profile, intraday=False).raised[0]

    row.status = "muted"
    db.flush()
    text = views.alerts_summary(db)

    assert "Skyzz exact" in text
    assert "muted" in text
    assert "Nothing wrong" not in text
    assert [r.id for r in alerts.open_alerts(db, profile)] == [row.id]


def test_one_campaign_spiking_produces_one_alert_not_two(db, build, profile):
    """The hourly and daily detectors can both see the same spike. The owner should see it once."""
    build.campaign(campaign_id="C1", name="Skyzz exact")
    yesterday = build.today - timedelta(days=1)
    _baseline(build, "C1", day=yesterday, spend=20.0, sales=80.0, clicks=40)
    build.day_of_spend("C1", day=yesterday, spend=60.0, sales=90.0, clicks=100, orders=3)
    for n in range(1, 5):
        day = build.today - timedelta(days=7 * n)
        stream.load(db, [_row(profile.amazon_profile_id, "C1", day, h, spend=1.0, clicks=4,
                                    sales=5.0, orders=1) for h in range(10)])
    stream.load(db, [_row(profile.amazon_profile_id, "C1", build.today, h, spend=4.0,
                                clicks=6, sales=2.0) for h in range(10)])

    sweep = alerts.run(db, profile, day=yesterday, intraday=True, at=_now(profile, 10),
                       client=FakeAds())

    spikes = [f for f in sweep.findings if f.type == "spend_spike"]
    assert len(spikes) == 1
    # And the one kept is the one about now.
    assert spikes[0].details["source"] == "marketing_stream"
    assert "so far today" in spikes[0].message


# ---------------- the shape of the day ----------------
def _hours(build, profile, *, campaign_id="C1", days=28, pattern):
    """`pattern` maps hour to (clicks, orders, spend, sales), for every day in the window."""
    from app.ppc import metrics

    end = build.today - timedelta(days=metrics.PROVISIONAL_DAYS)
    rows = []
    for n in range(days):
        day = end - timedelta(days=n)
        for hour, (clicks, orders, spend, sales) in pattern.items():
            rows.append(_row(profile.amazon_profile_id, campaign_id, day, hour,
                                   clicks=clicks, orders=orders, spend=spend, sales=sales,
                                   impressions=clicks * 20))
    stream.load(build.db, rows)


def test_without_hourly_data_dayparting_says_so_rather_than_guessing(db, build, profile):
    shaped = dayparting.shape(db, profile)

    assert shaped.has_data is False
    assert shaped.coverage == "none"
    assert "Marketing Stream" in shaped.reason
    assert dayparting.candidates(db, profile, shaped=shaped) == []


def test_a_wasteful_stretch_of_night_is_found_and_tested(db, build, profile):
    build.campaign(campaign_id="C1", name="Skyzz exact")
    pattern = {h: (4, 2, 2.00, 20.00) for h in range(8, 22)}         # the day converts well
    pattern.update({h: (6, 0, 3.00, 1.00) for h in (1, 2, 3, 4)})    # the small hours do not
    _hours(build, profile, pattern=pattern)

    shaped = dayparting.shape(db, profile)
    verdicts = dayparting.judge(shaped)

    assert shaped.coverage == "good"
    wasteful = sorted(v.bucket.hour for v in dayparting.wasteful(verdicts) if v.proven)
    assert wasteful == [1, 2, 3, 4]
    assert "1am–5am" in dayparting.summary(shaped, verdicts)


def test_an_hour_that_converts_better_is_never_called_proven_wasteful(db, build, profile):
    """ACOS is cpc / (cvr x order value), so a high-CPC hour can convert *well* and still cost more.

    The conversion-rate test is two-sided. Treating "significantly different" as proof of "wasteful"
    certified an hour as waste using evidence that it converts better than the rest of the day.
    """
    build.campaign(campaign_id="C1", name="Skyzz exact")
    # Hour 3: converts at 15% (better) but pays $3 a click, so its ACOS is twice the rest of the day.
    pattern = {h: (20, 2, 20.00, 100.00) for h in range(0, 24)}       # 10% CVR, $1.00 CPC
    pattern[3] = (20, 3, 60.00, 150.00)                               # 15% CVR, $3.00 CPC
    _hours(build, profile, pattern=pattern)

    shaped = dayparting.shape(db, profile)
    verdicts = dayparting.judge(shaped)
    three = next((v for v in verdicts if v.bucket.hour == 3), None)

    assert three is not None and three.kind == "wasteful"     # it really does cost more
    assert three.comparison.significant                       # and the CVR difference is real
    assert three.comparison.difference > 0                    # but it points the other way
    assert three.proven is False                              # so it is shape, not proof
    assert dayparting.candidates(db, profile, shaped=shaped) == []


def test_a_zero_order_hour_is_not_proven_on_an_interval_alone(db, build, profile):
    """An arm with no orders contributes no variance, so the Wald interval narrows for free.

    Thirty clicks and nothing to show for them came out "proven" on the interval while the pooled
    p-value was 0.068. Both gates have to agree now.
    """
    from app.ppc import statistics

    comparison = statistics.two_proportions(0, 30, 60, 600, metric="cvr")

    assert comparison.significant is True            # the interval says yes...
    assert comparison.p_value > dayparting.ALPHA     # ...and the test itself does not
    verdict = dayparting.Verdict(bucket=dayparting.Bucket(hour=3, weekday=None, clicks=30,
                                                          spend=15.0, days=28),
                                 kind="wasteful", comparison=comparison,
                                 rest_of_day_acos=0.2, gap=None)
    assert verdict.proven is False


def test_an_hour_is_judged_against_the_rest_of_the_day_not_the_whole_day(db, build, profile):
    """A campaign concentrated into a few hours would otherwise shrink its own difference.

    Dividing by an account ACOS that includes the hour under test means the biggest hour is measured
    largely against itself, and the worst hour in a concentrated campaign goes unreported.
    """
    from app.ppc import metrics

    build.campaign(campaign_id="C2", name="Evening only")
    end = build.today - timedelta(days=metrics.PROVISIONAL_DAYS)
    rows = []
    for n in range(28):
        day = end - timedelta(days=n)
        # Hour 20 is two thirds of the spend and converts at 3%; the rest convert at 5%.
        rows.append(_row(profile.amazon_profile_id, "C2", day, 20, clicks=72, orders=2,
                               spend=72.00, sales=180.00, impressions=1400))
        rows.extend(_row(profile.amazon_profile_id, "C2", day, h, clicks=15, orders=1,
                               spend=7.50, sales=26.00, impressions=300)
                    for h in (16, 17, 18, 19, 21))
    stream.load(db, rows)

    shaped = dayparting.shape(db, profile, campaign_id="C2")
    twenty = next(v for v in dayparting.judge(shaped) if v.bucket.hour == 20)

    # Against the whole day — which is mostly hour 20 — the gap would be far smaller than the gate.
    against_whole_day = (shaped.by_hour[20].acos - shaped.account_acos) / shaped.account_acos
    assert against_whole_day < dayparting.MATERIAL_GAP
    assert twenty.gap >= dayparting.MATERIAL_GAP
    assert twenty.kind == "wasteful" and twenty.proven


def test_budget_readings_do_not_count_as_days_of_hourly_data(db, build, profile):
    """`budget.record` writes to the same table with the metric columns left at zero.

    Counting those dates turned three days of real traffic into twenty-four days of "coverage", which
    cleared the `good` gate and recommended a bid schedule off three days.
    """
    from app.ppc import metrics

    build.campaign(campaign_id="C1", name="Skyzz exact", budget=40.0)
    end = build.today - timedelta(days=metrics.PROVISIONAL_DAYS)
    pattern = {h: (20, 6, 10.00, 60.00) for h in range(8, 22)}
    pattern.update({h: (30, 0, 15.00, 0.0) for h in (1, 2, 3)})
    _hours(build, profile, days=3, pattern=pattern)

    before = dayparting.shape(db, profile)
    assert before.days == 3 and before.coverage == "thin"

    # A month of two-hourly budget readings, exactly as the job writes them.
    for n in range(24):
        day = end - timedelta(days=n)
        for hour in range(0, 24, 2):
            budget.record(db, profile, budget.Pace(
                profile_id=profile.amazon_profile_id,
                at=datetime.combine(day, datetime.min.time(),
                                    tzinfo=metrics.zone_for(profile)).replace(hour=hour),
                day=day, elapsed=hour / 24, errors=[],
                readings=[budget.Reading("C1", "Skyzz exact", 40.0, 50.0, None)]))

    after = dayparting.shape(db, profile)

    assert after.days == 3, "budget readings are not days of traffic"
    assert after.coverage == "thin"
    assert dayparting.candidates(db, profile, shaped=after) == []
    assert stream.coverage(db, profile)["days"] == 3


def test_the_wasteful_hour_recommendation_is_worth_a_positive_number(db, build, profile):
    """`monthly_effect` is documented "positive is better", and the digest prints its magnitude.

    `spend - sales` values the forgone sales at a 100% margin, so it came out negative for every
    wasteful hour with an ACOS under 100% — a loss figure presented as a saving.
    """
    build.campaign(campaign_id="C1", name="Skyzz exact")
    pattern = {h: (20, 6, 10.00, 120.00) for h in range(8, 22)}       # rest of day at ~8% ACOS
    pattern.update({h: (20, 1, 10.00, 20.00) for h in (1, 2, 3)})     # small hours at 50%
    _hours(build, profile, pattern=pattern)

    found = dayparting.candidates(db, profile)

    assert found, "a 50% ACOS stretch against an 8% day should be flagged"
    assert found[0].monthly_effect > 0
    assert found[0].monthly_effect <= found[0].spend_at_risk
    assert found[0].evidence["overspend_vs_rest_of_day"] == found[0].monthly_effect


def test_the_summary_quotes_the_marketplaces_own_currency(db, build, profile):
    build.campaign(campaign_id="C1", name="Skyzz exact")
    profile.currency = "CAD"
    db.flush()
    pattern = {h: (20, 6, 10.00, 120.00) for h in range(8, 22)}
    pattern.update({h: (20, 0, 10.00, 0.0) for h in (1, 2, 3)})
    _hours(build, profile, pattern=pattern)

    shaped = dayparting.shape(db, profile)

    assert shaped.currency == "CAD"
    assert "C$" in dayparting.summary(shaped)
    assert "$" in dayparting.summary(shaped) and "C$" in dayparting.summary(shaped)


def test_a_dayparting_recommendation_says_it_cannot_be_applied(db, build, profile):
    """Sponsored Products has no hour-of-day setting, and a recommendation must not imply one."""
    from app.ppc import apply as ppc_apply

    build.campaign(campaign_id="C1", name="Skyzz exact")
    pattern = {h: (4, 2, 2.00, 20.00) for h in range(8, 22)}
    pattern.update({h: (6, 0, 3.00, 1.00) for h in (1, 2, 3, 4)})
    _hours(build, profile, pattern=pattern)

    found = dayparting.candidates(db, profile)

    assert [c.change_type for c in found][0] == "dayparting"
    assert "dayparting" in ppc_apply.ADVISORY
    assert "change for you to make" in found[0].reason
    assert found[0].evidence["hour_ranges"] == "1am–5am"


def test_two_good_weeks_are_not_enough_to_recommend_anything(db, build, profile):
    """The easiest bug here is calling a fortnight a pattern."""
    build.campaign(campaign_id="C1", name="Skyzz exact")
    pattern = {h: (10, 4, 5.00, 40.00) for h in range(8, 22)}
    pattern.update({h: (20, 0, 10.00, 0.00) for h in (1, 2, 3)})
    _hours(build, profile, days=10, pattern=pattern)

    shaped = dayparting.shape(db, profile)

    assert shaped.has_data is True
    assert shaped.coverage == "thin"
    assert dayparting.candidates(db, profile, shaped=shaped) == []
    assert "not enough to act on it" in dayparting.summary(shaped)


def test_an_hour_seen_on_too_few_days_is_not_judged(db, build, profile):
    """One terrible week is not a pattern, however much money it lost.

    Built inside a full day, deliberately. An earlier version of this test put the thin hour in a
    shape of its own, where `judge` skipped it because there was no rest-of-day to compare against —
    so the assertion passed without the days floor ever being consulted, and would have gone on
    passing with that floor deleted.
    """
    from app.ppc import metrics

    build.campaign(campaign_id="C1", name="Skyzz exact")
    # 23 hours with a full window behind them, and hour 3 present on only four days.
    end = build.today - timedelta(days=metrics.PROVISIONAL_DAYS)
    rows = []
    for n in range(28):
        day = end - timedelta(days=n)
        rows.extend(_row(profile.amazon_profile_id, "C1", day, h, clicks=20, orders=6,
                               spend=10.00, sales=60.00, impressions=400)
                    for h in range(24) if h != 3)
        if n < 4:
            rows.append(_row(profile.amazon_profile_id, "C1", day, 3, clicks=60, orders=0,
                                   spend=40.00, sales=0.0, impressions=1200))
    stream.load(db, rows)

    shaped = dayparting.shape(db, profile)

    assert shaped.by_hour[3].clicks == 240            # not short of clicks
    assert shaped.by_hour[3].spend == 160.00          # not short of money either
    assert shaped.by_hour[3].days == 4                # short of days, which is the gate that counts
    assert shaped.by_hour[3].substantial is False
    assert all(v.bucket.hour != 3 for v in dayparting.judge(shaped))
    # And the rest of the day is healthy, so nothing else gets flagged in its place.
    assert dayparting.candidates(db, profile, shaped=shaped) == []


def test_a_small_but_real_difference_is_not_worth_a_sentence(db, build, profile):
    """Significance is not the only gate: a proven 3% gap is still nobody's morning."""
    build.campaign(campaign_id="C1", name="Skyzz exact")
    pattern = {h: (40, 12, 20.00, 120.00) for h in range(0, 24)}
    pattern[3] = (40, 12, 20.60, 120.00)           # 3% worse ACOS, on identical conversion
    _hours(build, profile, pattern=pattern)

    shaped = dayparting.shape(db, profile)

    assert shaped.coverage == "good"
    assert dayparting.judge(shaped) == []


def test_a_partial_day_is_left_out_of_the_shape(db, build, profile):
    """Today is half finished, and a half-finished afternoon would drag every afternoon down."""
    build.campaign(campaign_id="C1")
    stream.load(db, [_row(profile.amazon_profile_id, "C1", build.today, 14,
                                clicks=500, spend=900.0, sales=0.0)])

    shaped = dayparting.shape(db, profile)

    assert shaped.has_data is False
    assert shaped.to_date == build.today - timedelta(days=3)


def test_hours_are_described_as_clock_ranges(db):
    assert dayparting.describe_hours([1, 2, 3, 4]) == "1am–5am"
    assert dayparting.describe_hours([22, 23, 0, 1]) == "10pm–2am"      # wraps past midnight
    assert dayparting.describe_hours([3]) == "3am"
    assert dayparting.describe_hours([1, 2, 14, 15]) == "1am–3am, 2pm–4pm"
    assert dayparting.describe_hours(list(range(24))) == "all day"
    assert dayparting.describe_hours([]) == ""


# ---------------- the ingestion seam ----------------
def test_hourly_rows_load_idempotently(db, build, profile):
    build.campaign(campaign_id="C1")
    row = _row(profile.amazon_profile_id, "C1", build.today, 9, clicks=10, spend=5.0,
                     sales=30.0, orders=2, impressions=200)

    stream.load(db, [row])
    stream.load(db, [row])

    stored = db.execute(select(PpcHourlyMetric)).scalars().all()
    assert len(stored) == 1 and stored[0].clicks == 10


def test_a_redelivered_hour_replaces_rather_than_doubles(db, build, profile):
    """Amazon invalidates clicks for up to 72 hours. The newer delivery is the better number."""
    build.campaign(campaign_id="C1")
    stream.load(db, [_row(profile.amazon_profile_id, "C1", build.today, 9, clicks=10,
                                spend=5.00)])
    result = stream.load(db, [_row(profile.amazon_profile_id, "C1", build.today, 9, clicks=7,
                                         spend=3.50)])

    stored = db.execute(select(PpcHourlyMetric)).scalars().one()
    assert (stored.clicks, float(stored.spend)) == (7, 3.50)
    assert result["restated"] == 1


def test_a_correction_in_the_same_batch_replaces_rather_than_doubles(db, build, profile):
    """Two records for one hour in one batch are an original and its correction, not two halves.

    Stream V2 is a single dataset, so there is no traffic-and-conversions split to re-join. Summing
    them would turn 10 clicks restated to 7 into 17, and the spike alert would fire on arithmetic.
    Postgres rejects a self-conflicting INSERT either way, so the choice has to be made here.
    """
    build.campaign(campaign_id="C1")
    result = stream.load(db, [
        _row(profile.amazon_profile_id, "C1", build.today, 9, impressions=1000, clicks=10,
                   spend=5.00, sales=30.0, orders=2),
        _row(profile.amazon_profile_id, "C1", build.today, 9, impressions=1000, clicks=7,
                   spend=3.50, sales=30.0, orders=2)])

    stored = db.execute(select(PpcHourlyMetric)).scalars().one()
    assert (stored.impressions, stored.clicks, float(stored.spend), float(stored.sales),
            stored.orders) == (1000, 7, 3.50, 30.0, 2)
    assert result["written"] == 1


def test_a_batch_spanning_two_profiles_does_not_cross_restatements(db, build, profile):
    """The table's identity includes the profile, and so must the restatement lookup."""
    build.campaign(campaign_id="C1")
    other = "9999999999"
    stream.load(db, [_row(other, "C1", build.today, 9, clicks=10, spend=5.00)])

    result = stream.load(db, [
        _row(profile.amazon_profile_id, "C1", build.today, 9, clicks=1, spend=100.00),
        _row(other, "C1", build.today, 9, clicks=7, spend=3.50)])

    # Only the row that genuinely had a previous figure counts as restated.
    assert result["restated"] == 1
    rows = {r.amazon_profile_id: r for r in db.execute(select(PpcHourlyMetric)).scalars().all()}
    assert float(rows[profile.amazon_profile_id].spend) == 100.00
    assert float(rows[other].spend) == 3.50


def test_collapsing_does_not_mutate_the_rows_it_was_given(db, build, profile):
    """A retry loop holding the same list would otherwise re-send figures that had grown."""
    from datetime import date as _date

    first = _row(profile.amazon_profile_id, "C1", _date(2026, 9, 1), 9, clicks=10, spend=5.0)
    second = _row(profile.amazon_profile_id, "C1", _date(2026, 9, 1), 9, clicks=7, spend=3.5)
    mine = [first, second]

    merged = stream.collapse(mine)

    assert (first.clicks, first.spend) == (10, 5.0)      # the caller's own object, untouched
    assert (merged[0].clicks, merged[0].spend) == (7, 3.5)
    assert merged[0] is not first and merged[0] is not second
    # And collapsing twice gives the same answer, which it would not if the inputs had grown.
    # `received_at` is a fresh func.now() each time and compares by identity, so it is left out.
    def figures(rows):
        return [{k: v for k, v in r.as_values().items() if k != "received_at"} for r in rows]

    assert figures(stream.collapse(mine)) == figures(merged)


def test_an_impossible_hour_is_refused_at_the_door(db):
    for hour in (-1, 24, 99, 9.5, "9", None):
        with pytest.raises((ValueError, TypeError), match="not an hour of the day"):
            _row("1", "C1", date(2026, 9, 1), hour)
    # A whole number arriving as a float is fine, and is stored as an integer hour.
    assert _row("1", "C1", date(2026, 9, 1), 9.0).hour == 9


def test_a_date_that_is_not_a_date_is_refused_before_the_batch_is_built(db):
    """Otherwise as_values() raises AttributeError on .weekday() with the batch half assembled."""
    with pytest.raises(ValueError, match="is not a date"):
        _row("1", "C1", "2026-09-01", 9)


def test_a_batch_larger_than_one_insert_still_lands(db, build, profile):
    """More rows than `INSERT_BATCH`, so the batching is actually exercised.

    The arithmetic, stated properly rather than waved at: `Row.as_values` has 13 columns, so one
    INSERT holds about 5,400 rows before Postgres' 65,535 bind-parameter ceiling. A hundred
    campaigns' worth of a month of hourly data is roughly 74,000 rows, which is fourteen times over
    — hence the batching. This test does not reach the ceiling itself (that would be a slow test
    about Postgres, not about this code); it checks that more rows than one batch holds all arrive,
    with nothing dropped at a boundary.
    """
    assert len(_row("1", "C1", date(2026, 9, 1), 9).as_values()) == 13, \
        "if the column count changes, the batch arithmetic above needs rechecking"
    build.campaign(campaign_id="C1")
    rows = []
    for n in range(40):
        day = build.today - timedelta(days=n)
        for campaign in ("C0", "C1", "C2"):
            rows.extend(_row(profile.amazon_profile_id, campaign, day, h, clicks=1,
                                   spend=0.10) for h in range(24))

    # 2,880 rows: nearly six batches, so boundaries are crossed repeatedly rather than once.
    assert len(rows) > 5 * metrics_module.INSERT_BATCH
    result = stream.load(db, rows)

    assert result["written"] == len(rows)
    assert db.scalar(select(__import__("sqlalchemy").func.count()).select_from(PpcHourlyMetric)) \
        == len(rows)


def test_the_transport_refuses_to_guess_rather_than_inventing_a_mapping(db):
    """D122: the V2 column names are unpublished, and a guess with tests behind it is the worst
    outcome available. This asserts the refusal is deliberate and explains itself."""
    with pytest.raises(stream.StreamNotConfigured, match="No Marketing Stream queue configured"):
        stream.consume(db)


def test_the_stream_is_dormant_without_a_subscription(db, profile):
    assert stream.configured() is False
    assert "needs an Amazon Marketing Stream subscription" in stream.status(db, profile)
    assert "Seventy days of history" in stream.status(db, profile)


def test_coverage_reports_what_is_actually_held(db, build, profile):
    build.campaign(campaign_id="C1")
    for n in range(5):
        day = build.today - timedelta(days=n)
        stream.load(db, [_row(profile.amazon_profile_id, "C1", day, 9, clicks=1)])

    found = stream.coverage(db, profile)

    assert found["days"] == 5
    assert found["last_date"] == build.today.isoformat()
    assert "5 days of hourly data" in stream.status(db, profile)


# ---------------- the views ----------------
def test_the_alerts_view_leads_with_the_worst_one(db, build, profile):
    high = build.campaign(campaign_id="C1", name="Skyzz exact")
    high.serving_status = "REJECTED"
    low = build.campaign(campaign_id="C2", name="Broad research")
    low.serving_status = "PENDING_REVIEW"
    db.flush()
    alerts.run(db, profile, intraday=False)

    text = views.alerts_summary(db)

    assert text.index("Skyzz exact") < text.index("Broad research")
    assert "🚨" in text and "2 open alert" in text


def test_the_alerts_view_says_so_when_nothing_is_wrong(db, build, profile):
    assert "Nothing wrong that I can see" in views.alerts_summary(db)


def test_the_budgets_view_reads_the_stored_readings_not_the_api(db, build, profile):
    build.campaign(campaign_id="C1", name="Skyzz exact", budget=40.0)
    budget.poll(db, profile, client=FakeAds(usage={"C1": (97.0, 40.0, _stamp(profile, 10))}),
                at=_now(profile, 10))

    text = views.budgets_now(db, now=_now(profile, 10))

    assert "Skyzz exact" in text and "97%" in text
    assert "🔴" in text


def test_a_campaign_missing_from_the_newest_poll_does_not_vanish(db, build, profile):
    """Amazon's 207 fails per campaign, so the latest poll need not contain every campaign.

    Filtering on one global `max(hour)` dropped the ones it missed — including, in the case that
    actually matters, a campaign sitting at 98% of its budget from an hour earlier.
    """
    build.campaign(campaign_id="C1", name="Big campaign", budget=100.0)
    build.campaign(campaign_id="C2", name="Second campaign", budget=50.0)
    budget.poll(db, profile, at=_now(profile, 10), client=FakeAds(usage={
        "C1": (40.0, 100.0, _stamp(profile, 10)), "C2": (98.0, 50.0, _stamp(profile, 10))}))
    # At noon Amazon refuses C2, so only C1 gets a reading for hour 12.
    budget.poll(db, profile, at=_now(profile, 12),
                client=FakeAds(usage={"C1": (60.0, 100.0, _stamp(profile, 12))},
                               errors={"C2": "Temporarily unavailable"}))

    text = views.budgets_now(db, now=_now(profile, 12))

    assert "Second campaign" in text and "98%" in text
    assert "as of 10am" in text          # and it is not presented as a current reading
    assert "Big campaign" in text and "60%" in text
    assert "out of budget" in text


def test_the_hours_view_explains_itself_before_there_is_any_data(db, build, profile):
    text = views.day_shape(db)

    assert "Marketing Stream" in text
    assert "Seventy days" in text


# ---------------- the sweep job ----------------
def test_an_interrupted_sweep_still_sends_the_message_on_retry(db, build, profile, monkeypatch):
    """`alerted_at` is the record of having told the owner; `seen_count` is not.

    Gating on `seen_count == 1` meant anything failing between the commit and the notify — the
    commit itself, a worker kill, an exception out of notify_owner — lost the message for good,
    because the retry found the row already at seen_count 2.
    """
    from app.jobs.handlers import ppc as ppc_jobs

    campaign = build.campaign(campaign_id="C1", name="Skyzz exact")
    campaign.serving_status = "REJECTED"
    db.commit()
    monkeypatch.setattr(ppc_jobs, "ads_configured", lambda: True)

    sent = []

    def explode(session, text, **kwargs):
        raise RuntimeError("WhatsApp fell over")

    monkeypatch.setattr("app.services.notify.notify_owner", explode)
    with pytest.raises(RuntimeError):
        ppc_jobs.ppc_alert_sweep(db, {}, None)

    row = db.execute(select(PpcAlert)).scalars().one()
    assert row.status == "open" and row.alerted_at is None
    assert (row.details or {}).get("seen_count") == 1

    def record(session, text, **kwargs):
        sent.append(text)
        return True

    monkeypatch.setattr("app.services.notify.notify_owner", record)
    ppc_jobs.ppc_alert_sweep(db, {}, None)

    assert sent and "Skyzz exact" in sent[0]
    assert db.execute(select(PpcAlert)).scalars().one().alerted_at is not None


def test_an_alert_already_sent_is_not_sent_again(db, build, profile, monkeypatch):
    from app.jobs.handlers import ppc as ppc_jobs

    campaign = build.campaign(campaign_id="C1", name="Skyzz exact")
    campaign.serving_status = "REJECTED"
    db.commit()
    monkeypatch.setattr(ppc_jobs, "ads_configured", lambda: True)
    sent = []
    monkeypatch.setattr("app.services.notify.notify_owner",
                        lambda session, text, **kw: sent.append(text) or True)

    ppc_jobs.ppc_alert_sweep(db, {}, None)
    ppc_jobs.ppc_alert_sweep(db, {}, None)

    assert len(sent) == 1


def test_a_muted_alert_is_never_messaged_however_new(db, build, profile, monkeypatch):
    from app.jobs.handlers import ppc as ppc_jobs

    campaign = build.campaign(campaign_id="C1", name="Skyzz exact")
    campaign.serving_status = "REJECTED"
    db.commit()
    monkeypatch.setattr(ppc_jobs, "ads_configured", lambda: True)
    # Raised and muted before the sweep ever gets to notify about it.
    alerts.run(db, profile, intraday=False).raised[0].status = "muted"
    db.commit()
    sent = []
    monkeypatch.setattr("app.services.notify.notify_owner",
                        lambda session, text, **kw: sent.append(text) or True)

    ppc_jobs.ppc_alert_sweep(db, {}, None)

    assert sent == []
    assert db.execute(select(PpcAlert)).scalars().one().status == "muted"


def test_budget_readings_do_not_shrink_the_intraday_baseline(db, build, profile):
    """Same contamination as the day count, with a worse consequence: a false spike alert.

    Budget-pace rows carry a date and no spend. Counting them as baseline days inflates the divisor,
    which shrinks the average, which makes a campaign spending exactly what it always spends look
    like a spike.
    """
    from app.ppc import metrics

    build.campaign(campaign_id="C1", name="Skyzz exact", budget=40.0)
    # Four same-weekday baselines of £20 through 10am, and today at £20 — no spike at all.
    for n in range(1, BASELINE_WEEKS_FOR_TEST + 1):
        day = build.today - timedelta(days=7 * n)
        stream.load(db, [_row(profile.amazon_profile_id, "C1", day, h, spend=2.0, clicks=4,
                                    sales=9.0, orders=1) for h in range(10)])
    stream.load(db, [_row(profile.amazon_profile_id, "C1", build.today, h, spend=2.0,
                                clicks=4, sales=9.0, orders=1) for h in range(10)])
    assert alerts.intraday_spike(db, profile, at=_now(profile, 10)) == []

    # Now a month of budget readings, which touch the same dates and hours with no spend.
    for n in range(28):
        day = build.today - timedelta(days=n)
        for hour in range(0, 11, 2):
            budget.record(db, profile, budget.Pace(
                profile_id=profile.amazon_profile_id,
                at=datetime.combine(day, datetime.min.time(),
                                    tzinfo=metrics.zone_for(profile)).replace(hour=hour),
                day=day, elapsed=hour / 24, errors=[],
                readings=[budget.Reading("C1", "Skyzz exact", 40.0, 50.0, None)]))

    assert alerts.intraday_spike(db, profile, at=_now(profile, 10)) == []


BASELINE_WEEKS_FOR_TEST = 4


# ---------------- the weekly report knows about both ----------------
def test_the_weekly_report_names_the_missing_subscription_as_a_blocker(db, build, profile):
    """A gap the owner could close belongs in "what's holding me back", not in silence."""
    from app.ppc import report

    build.campaign(campaign_id="C1", name="Skyzz exact")
    build.metrics(entity_type="campaign", entity_id="C1", clicks=400, orders=40, spend=300.0,
                  sales=1200.0)
    build.listing()
    text = report.weekly(db, profile)

    assert "Marketing Stream subscription" in text
    assert "which hours of the day sell" in text


def test_a_report_with_no_numbers_still_says_what_is_wrong(db, build, profile):
    """An account that is suspended has no numbers *because* it is suspended.

    The report used to stop at "that is a data gap" and leave the explanation out of the only
    message the owner reads on a Monday.
    """
    from app.ppc import report

    campaign = build.campaign(campaign_id="C1", name="Skyzz exact")
    campaign.serving_status = "ADVERTISER_PAYMENT_FAILURE"
    db.flush()
    alerts.run(db, profile, intraday=False)

    text = report.weekly(db, profile)

    assert "data gap" in text
    assert "Still open" in text and "a payment failed" in text


def test_the_weekly_report_carries_the_shape_of_the_day_and_what_is_open(db, build, profile):
    from app.ppc import report

    campaign = build.campaign(campaign_id="C1", name="Skyzz exact")
    campaign.serving_status = "REJECTED"
    db.flush()
    build.metrics(entity_type="campaign", entity_id="C1", clicks=400, orders=40, spend=300.0,
                  sales=1200.0)
    build.listing()
    pattern = {h: (20, 6, 10.00, 120.00) for h in range(8, 22)}
    pattern.update({h: (20, 0, 10.00, 0.0) for h in (1, 2, 3)})
    _hours(build, profile, pattern=pattern)
    alerts.run(db, profile, intraday=False)

    text = report.weekly(db, profile)

    assert "The shape of the day" in text
    assert "1am" in text
    assert "Still open" in text and "isn\'t serving" in text
    assert "Marketing Stream subscription" not in text      # there is hourly data now
    assert "`PPC ALERTS`" in text


def test_a_stale_reading_is_not_written_down_either(db, build, profile):
    """The view reads these rows back with no way to know when the reading was taken.

    Storing a reading that `exhausted_early` had just rejected as stale would make `PPC BUDGETS`
    assert the out-of-budget claim the alert declined to make.
    """
    build.campaign(campaign_id="C1", name="Skyzz exact", budget=40.0)
    client = FakeAds(usage={"C1": (100.0, 40.0, _stamp(profile, 11, minutes_ago=240))})

    budget.poll(db, profile, client=client, at=_now(profile, 11))

    assert db.execute(select(PpcHourlyMetric)).scalars().all() == []
    assert "out of budget" not in views.budgets_now(db, now=_now(profile, 11))


def test_a_failed_hourly_check_does_not_let_the_daily_one_close_its_alert(db, build, profile,
                                                                         monkeypatch):
    """Both detectors raise `spend_spike`, so one succeeding is not grounds to resolve the type.

    The hourly detector is about today; the daily one is about yesterday. Letting yesterday's
    silence close an alert raised an hour ago from today's data is the same false all-clear in a
    different coat.
    """
    build.campaign(campaign_id="C1", name="Skyzz exact")
    for n in range(1, 5):
        day = build.today - timedelta(days=7 * n)
        stream.load(db, [_row(profile.amazon_profile_id, "C1", day, h, spend=1.0, clicks=4,
                                    sales=5.0, orders=1) for h in range(10)])
    stream.load(db, [_row(profile.amazon_profile_id, "C1", build.today, h, spend=4.0,
                                clicks=6, sales=2.0) for h in range(10)])

    first = alerts.run(db, profile, at=_now(profile, 10), client=FakeAds())
    assert "spend_spike" in {f.type for f in first.findings}

    def explode(*args, **kwargs):
        raise RuntimeError("the hourly table is unavailable")

    monkeypatch.setattr(alerts, "intraday_spike", explode)
    second = alerts.run(db, profile, at=_now(profile, 11), client=FakeAds())

    assert "spend_spike" not in second.checked
    row = db.execute(select(PpcAlert).where(PpcAlert.type == "spend_spike")).scalars().one()
    assert row.status == "open"


def test_the_intraday_spike_compares_the_same_part_of_the_day(db, build, profile):
    """Today-so-far against the same slice of past days, not against their whole day.

    Marketing Stream delivers a finished day whole, so a baseline day in this table always has all
    24 hours. Against a full day, today's ten hours of spend look small and a real spike disappears;
    the `hour <= hour` filter on both sides is what makes the comparison meaningful. An earlier
    fixture gave the baseline days only ten hours each, which made the filter a no-op — it could
    have been deleted outright with the suite staying green.
    """
    build.campaign(campaign_id="C1", name="Skyzz exact")
    # Four past same-weekdays, each a complete 24-hour day at $1/hour: $10 by 10am, $24 by midnight.
    for n in range(1, 5):
        day = build.today - timedelta(days=7 * n)
        stream.load(db, [_row(profile.amazon_profile_id, "C1", day, h, spend=1.0, clicks=4,
                                    sales=6.0, orders=1) for h in range(24)])
    # Today: $2/hour through 10am, so $20 against a $10 baseline — a 100% spike.
    stream.load(db, [_row(profile.amazon_profile_id, "C1", build.today, h, spend=2.0,
                                clicks=5, sales=6.0, orders=1) for h in range(10)])

    found = alerts.intraday_spike(db, profile, at=_now(profile, 10))

    assert [f.type for f in found] == ["spend_spike"]
    assert found[0].details["spend"] == 20.0
    # $10, the baseline's spend *through 10am* — not $24, its spend for the whole day.
    assert found[0].details["baseline_spend"] == 10.0
    assert found[0].details["change_pct"] == 100.0
    # And against the full baseline day the same spend is below the threshold, which is exactly what
    # dropping the hour filter would compute.
    assert (20.0 - 24.0) / 24.0 * 100 < get_settings().ppc_anomaly_spend_spike_pct


def test_an_hour_with_too_little_money_in_it_is_not_judged(db, build, profile):
    """`Bucket.substantial`'s money floor, which had no test and is doing real work.

    An hour at 2 clicks and 20p a day reaches 56 clicks and £5.60 over the window. It has no sales,
    so it skips the material-gap check entirely, and 0 orders in 56 clicks against a healthy
    rest-of-day converts to a comfortably significant result — a "proven wasteful" verdict and an
    advisory recommendation to stop advertising at 3am to save £6 a month.
    """
    build.campaign(campaign_id="C1", name="Skyzz exact")
    pattern = {h: (20, 6, 10.00, 120.00) for h in range(0, 24)}
    pattern[3] = (2, 0, 0.20, 0.0)
    _hours(build, profile, pattern=pattern)

    shaped = dayparting.shape(db, profile)
    three = shaped.by_hour[3]

    assert (three.clicks, three.spend, three.days) == (56, 5.60, 28)
    assert three.days >= dayparting.MIN_DAYS          # clears the days floor
    assert three.clicks >= dayparting.MIN_CLICKS      # and the click floor
    assert three.spend < dayparting.MIN_SPEND         # and is stopped by the money floor
    assert three.substantial is False
    assert all(v.bucket.hour != 3 for v in dayparting.judge(shaped))
    assert dayparting.candidates(db, profile, shaped=shaped) == []


def test_an_hour_with_too_few_clicks_in_it_is_not_judged(db, build, profile):
    """The click floor, likewise. Plenty of money, almost no traffic — nothing to conclude from."""
    build.campaign(campaign_id="C1", name="Skyzz exact")
    pattern = {h: (20, 6, 10.00, 120.00) for h in range(0, 24)}
    pattern[3] = (1, 0, 2.00, 0.0)            # 28 clicks, £56 over the window
    _hours(build, profile, pattern=pattern)

    shaped = dayparting.shape(db, profile)
    three = shaped.by_hour[3]

    assert three.spend >= dayparting.MIN_SPEND        # the money is there
    assert three.clicks < dayparting.MIN_CLICKS       # the traffic is not
    assert three.substantial is False
    assert dayparting.candidates(db, profile, shaped=shaped) == []


def test_a_utc_moment_is_filed_under_the_marketplaces_own_hour(db, build, profile):
    """Everything downstream reads `pace.at.hour`, so it has to be the local hour."""
    build.campaign(campaign_id="C1", name="Skyzz exact", budget=40.0)
    eleven_local = _now(profile, 11)

    pace = budget.poll(db, profile, at=eleven_local.astimezone(timezone.utc),
                       client=FakeAds(usage={"C1": (30.0, 40.0, _stamp(profile, 11))}))

    assert pace.at.hour == 11
    assert pace.day == eleven_local.date()
    assert db.execute(select(PpcHourlyMetric)).scalars().one().hour == 11
    assert "11am" in budget.summary(pace)
