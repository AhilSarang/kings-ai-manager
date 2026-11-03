"""P4 M3: the jobs that keep the data current, and how they behave when Amazon says no."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Job, PpcMetricsDaily, PpcProfile
from app.jobs import queue
from app.jobs.errors import PermanentError, RetryableError
from app.jobs.handlers import load_handlers
from app.jobs.handlers.ppc import ppc_paused
from app.ppc import profiles, reports
from app.ppc.errors import AdsAuthError, AdsError, AdsReportInFlight
from app.ppc.fake import FakeAdsClient
from tests.conftest import run_jobs


def texts(sent) -> str:
    """Outbound messages as one blob; a template send carries no text of its own."""
    return "\n---\n".join(m.get("text", f"[{m['kind']}]") for m in sent)

TODAY = date(2026, 9, 25)


@pytest.fixture
def window_open(db, owner):
    """WhatsApp only allows free text inside 24 hours of the owner's last message."""
    from app.services.common import utcnow

    owner.last_inbound_at = utcnow()
    db.commit()
    return owner


@pytest.fixture
def profile(db):
    row = PpcProfile(amazon_profile_id="4444444444", marketplace_id="ATVPDKIKX0DER",
                     country_code="US", currency="USD", timezone="America/Los_Angeles",
                     status="active")
    db.add(row)
    db.commit()
    return row


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    """The fake client, and credentials present — the jobs do nothing at all without them."""
    from pydantic import SecretStr

    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "ads_backend", "fake", raising=False)
    monkeypatch.setattr(s, "ads_lwa_client_id", "test-ads-client", raising=False)
    monkeypatch.setattr(s, "ads_lwa_client_secret", SecretStr("test-secret"), raising=False)
    monkeypatch.setattr(s, "ads_lwa_refresh_token", SecretStr("Atzr|test"), raising=False)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Report jobs space themselves out and poll on a delay; in tests they run straight away."""
    from app.jobs.handlers import ppc as ppc_jobs

    monkeypatch.setattr(ppc_jobs, "SPACING_SECONDS", 0)
    monkeypatch.setattr(ppc_jobs, "POLL_SECONDS", 0)
    monkeypatch.setattr(ppc_jobs, "MAX_POLL_SECONDS", 0)


def load_reports(db, **payload) -> int:
    """Plan the load, then run every report job it queues to completion."""
    queue.enqueue(db, "ppc_load_reports", payload)
    db.commit()
    run_jobs(types=["ppc_load_reports"])
    return run_jobs(types=["ppc_report"], limit=200)


def queue_report(rows, report_id="RPT-1"):
    url = f"https://ads-reports.test/{report_id}.json.gz"
    FakeAdsClient.expect(r"/reporting/reports$", {"reportId": report_id, "status": "PENDING"})
    FakeAdsClient.expect(rf"/reporting/reports/{report_id}",
                         {"reportId": report_id, "status": "COMPLETED", "url": url})
    FakeAdsClient.downloads[url] = rows


def test_every_ppc_job_has_a_handler():
    handlers = load_handlers()
    for job_type in ("ppc_sync_profiles", "ppc_sync_structure", "ppc_load_reports", "ppc_backfill",
                     "ppc_backfill_chunk"):
        assert job_type in handlers


def test_the_daily_load_runs_end_to_end_and_stores_what_it_read(db, profile):
    from app.ppc import metrics

    day = metrics.today_for(profile) - timedelta(days=5)
    queue_report([{"date": day.isoformat(), "campaignId": "C1", "campaignName": "Skyzz",
                   "impressions": 100, "clicks": 5, "cost": 2.5, "sales7d": 20.0, "purchases7d": 1,
                   "unitsSoldClicks7d": 1}])
    load_reports(db, only=["campaigns"])
    row = db.execute(select(PpcMetricsDaily)).scalar_one()
    assert row.amazon_entity_id == "C1" and float(row.spend) == 2.5


def test_the_daily_plan_covers_every_report_for_every_enabled_marketplace(db, profile):
    from app.db.models import PpcProfile as P

    db.add(P(amazon_profile_id="5555555555", marketplace_id="A2EUQ1WTGCTBG2", country_code="CA",
             currency="CAD", timezone="America/Toronto", status="active"))
    db.commit()
    queue.enqueue(db, "ppc_load_reports", {})
    db.commit()
    run_jobs(types=["ppc_load_reports"])
    jobs = db.execute(select(Job).where(Job.type == "ppc_report")).scalars().all()
    planned = {(j.payload["profile_id"], j.payload["report"]) for j in jobs}
    assert len({p for p, _r in planned}) == 2, "both marketplaces, not just the alphabetically first"
    assert {r for _p, r in planned} == set(reports.DAILY_ORDER)


def test_one_report_is_asked_for_and_collected_by_separate_short_jobs(db, profile):
    """A three-hour report must not hold a worker: the waiting happens in the queue."""
    from app.ppc import metrics

    day = metrics.today_for(profile) - timedelta(days=4)
    url = "https://ads-reports.test/SLOW.json.gz"
    FakeAdsClient.expect(r"/reporting/reports$", {"reportId": "SLOW", "status": "PENDING"})
    FakeAdsClient.expect(r"/reporting/reports/SLOW", {"reportId": "SLOW", "status": "PENDING"})
    FakeAdsClient.expect(r"/reporting/reports/SLOW", {"reportId": "SLOW", "status": "COMPLETED",
                                                     "url": url})
    FakeAdsClient.downloads[url] = [
        {"date": day.isoformat(), "campaignId": "C1", "impressions": 10, "clicks": 1, "cost": 1.0,
         "sales7d": 5.0, "purchases7d": 1, "unitsSoldClicks7d": 1}]
    queue.enqueue(db, "ppc_load_reports", {"only": ["campaigns"]})
    db.commit()
    run_jobs(types=["ppc_load_reports"])
    assert run_jobs(types=["ppc_report"], limit=1) == 1      # asks Amazon, stores nothing yet
    assert db.execute(select(PpcMetricsDaily)).scalars().all() == []
    assert run_jobs(types=["ppc_report"], limit=1) == 1      # still building
    assert db.execute(select(PpcMetricsDaily)).scalars().all() == []
    assert run_jobs(types=["ppc_report"], limit=1) == 1      # ready, and stored
    assert db.execute(select(PpcMetricsDaily)).scalar_one().amazon_entity_id == "C1"


def test_one_bad_report_does_not_throw_away_the_ones_that_worked(db, profile):
    """Seven reports in one transaction meant the last failure discarded the first six."""
    from app.ppc import metrics

    day = metrics.today_for(profile) - timedelta(days=5)
    queue_report([{"date": day.isoformat(), "campaignId": "C1", "impressions": 10, "clicks": 1,
                   "cost": 1.0, "sales7d": 5.0, "purchases7d": 1, "unitsSoldClicks7d": 1}],
                 report_id="RPT-OK")
    FakeAdsClient.expect(r"/reporting/reports$", AdsError("date range too long"))
    load_reports(db, only=["campaigns", "targets"])
    assert db.execute(select(PpcMetricsDaily)).scalar_one().amazon_entity_id == "C1", \
        "the campaign report is kept even though the targeting report failed"


def test_nothing_runs_while_ppc_is_paused(db, profile, owner):
    from app.services import settings_store
    from app.services.common import Actor

    settings_store.set_value(db, "ppc_paused", True, Actor.user(owner.id))
    db.commit()
    assert ppc_paused(db) is True
    queue.enqueue(db, "ppc_load_reports", {})
    db.commit()
    run_jobs(types=["ppc_load_reports"])
    assert db.execute(select(PpcMetricsDaily)).scalars().all() == []
    assert FakeAdsClient.calls == [], "a paused agent doesn't talk to Amazon at all"


def test_a_job_with_no_profile_yet_does_nothing_rather_than_failing(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ads_profile_id_us", "", raising=False)
    queue.enqueue(db, "ppc_sync_structure", {})
    db.commit()
    run_jobs(types=["ppc_sync_structure"])
    job = db.execute(select(Job).where(Job.type == "ppc_sync_structure")).scalar_one()
    assert job.status == "done", "waiting for the owner to connect Ads is not a failure"


def test_lost_access_is_reported_once_and_stops_the_collection(db, profile, sent, no_quiet_hours, window_open):
    FakeAdsClient.expect(r"/sp/campaigns/list", AdsAuthError("consent was withdrawn"))
    FakeAdsClient.expect(r"/portfolios/list", {"portfolios": []})
    queue.enqueue(db, "ppc_sync_structure", {})
    db.commit()
    run_jobs(types=["ppc_sync_structure"])
    run_jobs()
    db.expire_all()
    assert profiles.get(db, "4444444444").status == "auth_error"
    assert "Amazon Ads access stopped working" in texts(sent)


def test_one_forbidden_call_does_not_look_like_a_broken_connection(db, profile, sent, no_quiet_hours, window_open):
    FakeAdsClient.expect(r"/portfolios/list", AdsAuthError("no portfolio scope", missing_scope=True))
    queue.enqueue(db, "ppc_sync_structure", {})
    db.commit()
    run_jobs(types=["ppc_sync_structure"])
    run_jobs()
    db.expire_all()
    assert profiles.get(db, "4444444444").status == "active", "one call being off-limits is not an outage"
    assert "won't let this app do one thing" in texts(sent)


def test_a_report_already_generating_is_retried_later_not_failed(db, profile):
    FakeAdsClient.expect(r"/reporting/reports$", AdsReportInFlight("duplicate of a processing request"))
    queue.enqueue(db, "ppc_load_reports", {"only": ["campaigns"]})
    db.commit()
    run_jobs(types=["ppc_load_reports"])
    run_jobs(types=["ppc_report"], limit=1)
    job = db.execute(select(Job).where(Job.type == "ppc_report")).scalars().first()
    assert job.status == "pending" and job.attempts == 1, "waiting for Amazon's own report to finish"


def test_a_report_amazon_refuses_outright_stops_rather_than_retrying_for_ever(db, profile):
    FakeAdsClient.expect(r"/reporting/reports$", AdsError("date range too long"))
    queue.enqueue(db, "ppc_load_reports", {"only": ["campaigns"]})
    db.commit()
    run_jobs(types=["ppc_load_reports"])
    run_jobs(types=["ppc_report"], limit=1)
    job = db.execute(select(Job).where(Job.type == "ppc_report")).scalars().first()
    assert job.status == "failed" and "date range too long" in (job.last_error or "")


def test_a_new_marketplace_is_reported_but_left_switched_off(db, sent, no_quiet_hours, window_open):
    FakeAdsClient.expect(r"/v2/profiles", [
        {"profileId": 888, "countryCode": "CA", "currencyCode": "CAD",
         "accountInfo": {"marketplaceStringId": "A2EUQ1WTGCTBG2", "type": "seller"}}])
    queue.enqueue(db, "ppc_sync_profiles", {})
    db.commit()
    run_jobs(types=["ppc_sync_profiles"])
    run_jobs()
    db.expire_all()
    assert profiles.get(db, "888").status == "paused"
    assert "aren't switched on yet" in texts(sent)


def test_a_periodic_sync_can_run_again_once_it_has_finished(db, profile):
    """A constant idempotency key meant the six-hourly structure sync ran once in the lifetime of
    the database: nothing clears the key and nothing prunes finished jobs."""
    import app.jobs.periodic as periodic

    periodic.ppc_structure_sync()
    db.expire_all()
    first = db.execute(select(Job).where(Job.type == "ppc_sync_structure")).scalar_one()
    queue.complete(db, first)
    db.commit()
    periodic.ppc_structure_sync()
    db.expire_all()
    assert len(db.execute(select(Job).where(Job.type == "ppc_sync_structure")).scalars().all()) == 2


def test_a_second_sync_is_not_stacked_up_while_the_first_is_still_waiting(db, profile):
    import app.jobs.periodic as periodic

    periodic.ppc_structure_sync()
    periodic.ppc_structure_sync()
    db.expire_all()
    assert len(db.execute(select(Job).where(Job.type == "ppc_sync_structure")).scalars().all()) == 1


def test_the_backfill_queues_its_chunks_through_the_job_queue(db, profile):
    queue.enqueue(db, "ppc_backfill", {"days": 365})
    db.commit()
    run_jobs(types=["ppc_backfill"])
    chunks = db.execute(select(Job).where(Job.type == "ppc_backfill_chunk")).scalars().all()
    assert len(chunks) >= 20, "95 days across seven reports, in 31-day requests"
    assert all(job.payload["report"] in reports.SPECS for job in chunks)


def test_the_schedule_covers_discovery_structure_and_both_re_pulls():
    import app.jobs.periodic  # noqa: F401
    from app.jobs.scheduler import PERIODIC
    from app.ppc import metrics

    assert PERIODIC["ppc_structure_sync"].trigger == "interval"
    assert PERIODIC["ppc_daily_load"].args["hour"] == 7
    deep = PERIODIC["ppc_deep_load"]
    assert deep.args["day_of_week"] == "mon" and deep.args["hour"] < PERIODIC["ppc_daily_load"].args["hour"], \
        "the weekly deep load claims the day's key first, so no report type is asked for twice"
    assert metrics.DEEP_RESTATEMENT_DAYS > metrics.RESTATEMENT_DAYS


def test_the_daily_and_weekly_loads_cannot_both_run_in_one_day(db, profile):
    """Amazon recommends one or two report requests per type per day, so they share a key."""
    import app.jobs.periodic as periodic

    periodic.ppc_deep_load()
    periodic.ppc_daily_load()
    db.expire_all()
    jobs = db.execute(select(Job).where(Job.type == "ppc_load_reports")).scalars().all()
    assert len(jobs) == 1 and jobs[0].payload.get("days") == 42


def test_a_hundred_thousand_row_sync_does_not_need_a_single_statement(db, profile):
    """The whole point of the batching: no bulk write may depend on how big the account is."""
    from app.ppc import metrics

    profile_id = profile.amazon_profile_id
    rows = [metrics.MetricRow(entity_type="target", amazon_entity_id=f"T{n}",
                              date=metrics.today_for(profile) - timedelta(days=5),
                              impressions=10, clicks=1, spend=0.2, sales=1.0, orders=0, units=0)
            for n in range(4000)]
    stats = metrics.load_daily(db, profile, rows, ad_product="sp", report_id="RPT-BIG")
    assert stats.stored == 4000
    assert db.execute(select(PpcMetricsDaily).where(
        PpcMetricsDaily.amazon_profile_id == profile_id)).scalars().all()


def test_an_expired_token_is_refreshed_once_rather_than_stopping_collection(db, profile):
    """A 401 can just be a token that expired mid-flight; it used to stop every sync."""
    from app.ppc.errors import AdsAuthError

    FakeAdsClient.expect(r"/portfolios/list", AdsAuthError("token expired"))
    queue.enqueue(db, "ppc_sync_structure", {})
    db.commit()
    run_jobs(types=["ppc_sync_structure"])
    db.expire_all()
    assert profiles.get(db, "4444444444").status == "auth_error"
    # ...and the next discovery run puts it back, without the owner sending anything.
    FakeAdsClient.reset()
    FakeAdsClient.expect(r"/v2/profiles", [
        {"profileId": 4444444444, "countryCode": "US",
         "accountInfo": {"marketplaceStringId": "ATVPDKIKX0DER"}}])
    queue.enqueue(db, "ppc_sync_profiles", {})
    db.commit()
    run_jobs(types=["ppc_sync_profiles"])
    db.expire_all()
    assert profiles.get(db, "4444444444").status == "active"


def test_nothing_happens_before_the_ads_application_exists(db, monkeypatch, sent, window_open):
    """Deploying Phase 4 before the owner has registered with Amazon Ads must be silent."""
    from app.config import get_settings
    from app.jobs.handlers import ppc as ppc_jobs
    from pydantic import SecretStr

    s = get_settings()
    monkeypatch.setattr(s, "ads_lwa_client_id", "", raising=False)
    monkeypatch.setattr(s, "ads_lwa_client_secret", SecretStr(""), raising=False)
    monkeypatch.setattr(s, "ads_lwa_refresh_token", SecretStr(""), raising=False)
    assert ppc_jobs.ads_configured() is False
    for job_type in ("ppc_sync_profiles", "ppc_sync_structure", "ppc_load_reports", "ppc_backfill"):
        queue.enqueue(db, job_type, {})
    db.commit()
    run_jobs(limit=50)
    assert FakeAdsClient.calls == [], "no Amazon call, so nothing to report as broken"
    assert "Amazon Ads" not in texts(sent), "no alarming message about something not yet connected"
    assert db.execute(select(Job).where(Job.status == "failed")).scalars().all() == []
