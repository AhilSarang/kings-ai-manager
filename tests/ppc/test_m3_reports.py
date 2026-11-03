"""P4 M3: the report requests, the daily load, restatements, provisional days and the backfill.

Amazon's v3 column names are full of traps — orders are `purchases7d`, units are `unitsSoldClicks7d`,
cost is `cost` and not `spend` — and a column that doesn't exist for a report type fails the whole
request rather than coming back empty. So the column lists are tested as carefully as the loading.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (PpcMetricsDaily, PpcMetricsRestatement, PpcPlacement, PpcProfile,
                           PpcPurchasedProduct, PpcSearchTerm, PpcSearchTermDecision)
from app.ppc import backfill, metrics, reports
from app.ppc.errors import AdsError
from app.ppc.fake import FakeAdsClient

PROFILE = "2222222222"
TODAY = date(2026, 9, 25)


@pytest.fixture
def profile(db):
    row = PpcProfile(amazon_profile_id=PROFILE, marketplace_id="ATVPDKIKX0DER", country_code="US",
                     currency="USD", timezone="America/Los_Angeles", status="active",
                     ads_account_id="ACC-9")
    db.add(row)
    db.commit()
    return row


def client():
    return FakeAdsClient()


def queue_report(rows, report_id="RPT-1", generated_at="2026-09-25T08:00:00Z"):
    url = f"https://ads-reports.test/{report_id}.json.gz"
    FakeAdsClient.expect(r"/reporting/reports$", {"reportId": report_id, "status": "PENDING"})
    FakeAdsClient.expect(rf"/reporting/reports/{report_id}",
                         {"reportId": report_id, "status": "COMPLETED", "url": url,
                          "generatedAt": generated_at})
    FakeAdsClient.downloads[url] = rows


def campaign_row(day, *, campaign="C1", cost=10.0, sales=40.0, purchases=2, clicks=20, units=2):
    return {"date": day.isoformat(), "campaignId": campaign, "campaignName": "Skyzz exact",
            "impressions": 500, "clicks": clicks, "cost": cost, "sales7d": sales,
            "purchases7d": purchases, "unitsSoldClicks7d": units,
            "attributedSalesSameSku7d": sales, "purchasesSameSku7d": purchases,
            "topOfSearchImpressionShare": 12.5}


# ---------------- the request Amazon is actually sent ----------------
def test_the_request_is_shaped_the_way_v3_wants_it(profile):
    body = reports.request_body(reports.SPECS["campaigns"], date(2026, 9, 1), date(2026, 9, 14))
    config = body["configuration"]
    assert body["startDate"] == "2026-09-01" and body["endDate"] == "2026-09-14"
    assert config["reportTypeId"] == "spCampaigns" and config["adProduct"] == "SPONSORED_PRODUCTS"
    assert config["groupBy"] == ["campaign"] and config["timeUnit"] == "DAILY"
    assert config["format"] == "GZIP_JSON", "the only format Sponsored Products offers"
    assert "date" in config["columns"], "a DAILY report must ask for the date column"


def test_the_group_by_values_are_the_unguessable_ones_amazon_documents():
    assert reports.SPECS["advertised_products"].group_by == ["advertiser"]
    assert reports.SPECS["purchased_products"].group_by == ["asin"]
    assert reports.SPECS["targets"].group_by == ["targeting"]
    assert reports.SPECS["placements"].group_by == ["campaignPlacement"]
    assert reports.SPECS["ad_groups"].group_by == ["adGroup"]


def test_no_report_asks_for_a_column_that_does_not_exist_in_v3():
    forbidden = {"orders", "spend", "unitsSold", "impressionShare", "salesSameSku7d",
                 "searchTermImpressionShare", "acosClicks7d", "roasClicks7d"}
    for spec in reports.SPECS.values():
        clash = forbidden.intersection(spec.columns)
        assert not clash, f"{spec.key} asks Amazon for {clash}, which v3 has no such column for"


def test_the_purchased_product_report_never_asks_for_traffic_it_does_not_have():
    columns = set(reports.SPECS["purchased_products"].columns)
    assert not columns & {"impressions", "clicks", "cost"}, \
        "spPurchasedProduct carries no traffic or cost columns; asking fails the whole report"


def test_impression_share_is_only_asked_of_the_one_report_that_has_it():
    for key, spec in reports.SPECS.items():
        if "topOfSearchImpressionShare" in spec.columns:
            assert key == "campaigns", f"{key} is not documented to carry impression share"


def test_a_long_window_becomes_several_requests_of_at_most_31_days():
    pieces = reports.chunks(date(2026, 8, 1), date(2026, 9, 25))
    assert len(pieces) == 2
    assert all((end - start).days + 1 <= reports.MAX_RANGE_DAYS for start, end in pieces)
    assert pieces[0][0] == date(2026, 8, 1) and pieces[-1][1] == date(2026, 9, 25)
    assert pieces[1][0] == pieces[0][1] + timedelta(days=1), "no day is skipped or asked for twice"


def test_a_window_older_than_amazons_memory_is_trimmed_rather_than_refused():
    spec = reports.SPECS["campaigns"]
    start, end = reports.clamp(spec, date(2025, 1, 1), TODAY, TODAY)
    assert (end - start).days + 1 == reports.RETENTION_DAYS - 1
    assert end == TODAY - timedelta(days=1), "today is still accruing"
    assert reports.clamp(spec, date(2025, 1, 1), date(2025, 6, 1), TODAY) is None


def test_search_terms_have_a_shorter_memory_than_everything_else():
    assert reports.SPECS["search_terms"].retention_days == 65
    assert reports.SPECS["campaigns"].retention_days == 95
    start, _end = reports.clamp(reports.SPECS["search_terms"], date(2025, 1, 1), TODAY, TODAY)
    assert (TODAY - start).days == 64, "a shared 95-day lookback would fail on search terms"


def test_a_column_amazon_has_moved_is_dropped_once_and_the_report_retried(profile):
    FakeAdsClient.expect(r"/reporting/reports$",
                         AdsError("createAsyncReport: Unsupported column topOfSearchImpressionShare"))
    queue_report([campaign_row(date(2026, 9, 20))], report_id="RPT-2")
    rows, report = reports.fetch(client(), reports.SPECS["campaigns"], profile,
                                 date(2026, 9, 20), date(2026, 9, 20))
    assert len(rows) == 1 and report["reportId"] == "RPT-2"
    second = FakeAdsClient.calls_to(r"/reporting/reports$")[1]
    assert "topOfSearchImpressionShare" not in second["json"]["configuration"]["columns"]


def test_a_column_we_actually_need_is_not_quietly_dropped(profile):
    FakeAdsClient.expect(r"/reporting/reports$", AdsError("createAsyncReport: bad column date"))
    with pytest.raises(AdsError, match="bad column date"):
        reports.fetch(client(), reports.SPECS["campaigns"], profile, TODAY, TODAY)


# ---------------- loading ----------------
def test_a_campaign_report_lands_in_metrics_with_amazons_column_names_translated(db, profile):
    day = date(2026, 9, 15)
    queue_report([campaign_row(day)])
    result = reports.load(db, profile, reports.SPECS["campaigns"], day, day, client=client(),
                          today=TODAY)
    row = db.execute(select(PpcMetricsDaily)).scalar_one()
    assert result["stored"] == 1
    assert row.entity_type == "campaign" and row.amazon_entity_id == "C1"
    assert float(row.spend) == 10.0, "spend comes from `cost`"
    assert float(row.sales) == 40.0 and row.orders == 2, "orders come from `purchases7d`"
    assert row.units == 2 and row.clicks == 20 and row.impressions == 500
    assert float(row.top_of_search_impression_share) == 12.5
    assert row.currency == "USD" and row.marketplace_id == "ATVPDKIKX0DER"


def test_every_row_records_where_the_number_came_from(db, profile):
    day = date(2026, 9, 15)
    queue_report([campaign_row(day)])
    reports.load(db, profile, reports.SPECS["campaigns"], day, day, client=client(), today=TODAY)
    row = db.execute(select(PpcMetricsDaily)).scalar_one()
    assert row.report_id == "RPT-1" and row.report_start_date == day and row.report_end_date == day
    assert row.attribution["window_days"] == 7
    assert row.attribution["columns"]["orders"] == "purchases7d"
    assert row.attribution["generated_at"] == "2026-09-25T08:00:00Z", \
        "Amazon sends no attribution metadata, so this is the only record of which pull it was"


def test_the_last_three_days_are_marked_provisional_and_older_ones_are_not(db, profile):
    days = [TODAY - timedelta(days=n) for n in (1, 2, 3, 4)]
    queue_report([campaign_row(d) for d in days])
    reports.load(db, profile, reports.SPECS["campaigns"], days[-1], days[0], client=client(),
                 today=TODAY)
    flags = {r.date: r.is_provisional for r in db.execute(select(PpcMetricsDaily)).scalars()}
    assert flags[TODAY - timedelta(days=1)] is True
    assert flags[TODAY - timedelta(days=2)] is True
    assert flags[TODAY - timedelta(days=3)] is False
    assert flags[TODAY - timedelta(days=4)] is False


def test_a_provisional_day_becomes_final_on_a_later_load_without_anyone_clearing_a_flag(db, profile):
    day = TODAY - timedelta(days=2)
    queue_report([campaign_row(day)])
    reports.load(db, profile, reports.SPECS["campaigns"], day, day, client=client(), today=TODAY)
    assert db.execute(select(PpcMetricsDaily)).scalar_one().is_provisional is True
    queue_report([campaign_row(day)], report_id="RPT-9")
    reports.load(db, profile, reports.SPECS["campaigns"], day, day, client=client(),
                 today=TODAY + timedelta(days=2))
    assert db.execute(select(PpcMetricsDaily)).scalar_one().is_provisional is False


def test_a_restated_day_is_overwritten_and_the_change_is_recorded(db, profile):
    day = date(2026, 9, 10)
    queue_report([campaign_row(day, cost=10.0, sales=40.0, purchases=2)])
    reports.load(db, profile, reports.SPECS["campaigns"], day, day, client=client(), today=TODAY)
    queue_report([campaign_row(day, cost=10.0, sales=65.0, purchases=3)], report_id="RPT-2")
    result = reports.load(db, profile, reports.SPECS["campaigns"], day, day, client=client(),
                          today=TODAY)
    row = db.execute(select(PpcMetricsDaily)).scalar_one()
    assert float(row.sales) == 65.0 and row.orders == 3, "the re-pull overwrites"
    change = db.execute(select(PpcMetricsRestatement)).scalar_one()
    assert result["restated"] == 1
    assert float(change.old_sales) == 40.0 and float(change.new_sales) == 65.0
    assert change.old_orders == 2 and change.new_orders == 3
    assert change.old_report_id == "RPT-1" and change.new_report_id == "RPT-2", \
        "so a number that moved can be traced to the two pulls that disagreed"


def test_a_re_pull_that_changes_nothing_records_nothing(db, profile):
    day = date(2026, 9, 10)
    for report_id in ("RPT-1", "RPT-2"):
        queue_report([campaign_row(day)], report_id=report_id)
        reports.load(db, profile, reports.SPECS["campaigns"], day, day, client=client(), today=TODAY)
    assert db.execute(select(PpcMetricsRestatement)).scalars().all() == []
    assert len(db.execute(select(PpcMetricsDaily)).scalars().all()) == 1, "one row per entity per day"


def test_a_rounding_difference_is_not_treated_as_a_restatement(db, profile):
    day = date(2026, 9, 10)
    queue_report([campaign_row(day, cost=10.00)])
    reports.load(db, profile, reports.SPECS["campaigns"], day, day, client=client(), today=TODAY)
    queue_report([campaign_row(day, cost=10.004)], report_id="RPT-2")
    reports.load(db, profile, reports.SPECS["campaigns"], day, day, client=client(), today=TODAY)
    assert db.execute(select(PpcMetricsRestatement)).scalars().all() == []


# ---------------- the tables with their own shape ----------------
def test_search_terms_are_stored_with_one_decision_per_term(db, profile):
    day = date(2026, 9, 15)
    rows = [{"date": day.isoformat(), "campaignId": "C1", "adGroupId": "G1", "keywordId": "K1",
             "keyword": "nicotine pouches", "searchTerm": "nicotine pouches mint", "matchType": "BROAD",
             "impressions": 100, "clicks": 9, "cost": 4.5, "sales7d": 30.0, "purchases7d": 2,
             "unitsSoldClicks7d": 2}]
    queue_report(rows)
    reports.load(db, profile, reports.SPECS["search_terms"], day, day, client=client(), today=TODAY)
    term = db.execute(select(PpcSearchTerm)).scalar_one()
    decision = db.execute(select(PpcSearchTermDecision)).scalar_one()
    assert term.search_term == "nicotine pouches mint" and term.clicks == 9
    assert float(term.spend) == 4.5 and term.orders == 2
    assert decision.harvest_status == "new" and decision.amazon_ad_group_id == "G1"


def test_a_harvest_decision_survives_the_next_days_load(db, profile):
    day = date(2026, 9, 15)
    row = {"date": day.isoformat(), "campaignId": "C1", "adGroupId": "G1", "keywordId": "K1",
           "searchTerm": "mint pouches", "impressions": 10, "clicks": 1, "cost": 0.5,
           "sales7d": 0.0, "purchases7d": 0, "unitsSoldClicks7d": 0}
    queue_report([row])
    reports.load(db, profile, reports.SPECS["search_terms"], day, day, client=client(), today=TODAY)
    decision = db.execute(select(PpcSearchTermDecision)).scalar_one()
    decision.harvest_status = "harvested"
    db.commit()
    queue_report([dict(row, date=(day + timedelta(days=1)).isoformat())], report_id="RPT-2")
    reports.load(db, profile, reports.SPECS["search_terms"], day, day + timedelta(days=1),
                 client=client(), today=TODAY)
    assert db.execute(select(PpcSearchTermDecision)).scalar_one().harvest_status == "harvested", \
        "a term already harvested must not be proposed again every morning"


def test_a_search_term_with_no_target_id_is_not_stored_twice(db, profile):
    day = date(2026, 9, 15)
    row = {"date": day.isoformat(), "campaignId": "C1", "adGroupId": "G1", "searchTerm": "pouches",
           "impressions": 10, "clicks": 1, "cost": 0.5, "sales7d": 0.0, "purchases7d": 0,
           "unitsSoldClicks7d": 0}
    for report_id in ("RPT-1", "RPT-2"):
        queue_report([row], report_id=report_id)
        reports.load(db, profile, reports.SPECS["search_terms"], day, day, client=client(), today=TODAY)
    stored = db.execute(select(PpcSearchTerm)).scalars().all()
    assert len(stored) == 1, "a null key in Postgres never conflicts, so the id is stored empty"
    assert stored[0].amazon_target_id == ""


def test_placement_labels_become_the_words_the_analysers_use(db, profile):
    day = date(2026, 9, 15)
    rows = [{"date": day.isoformat(), "campaignId": "C1",
             "placementClassification": "Top of Search on-Amazon", "impressions": 300, "clicks": 12,
             "cost": 8.0, "sales7d": 50.0, "purchases7d": 3, "unitsSoldClicks7d": 3},
            {"date": day.isoformat(), "campaignId": "C1",
             "placementClassification": "Detail Page on-Amazon", "impressions": 100, "clicks": 2,
             "cost": 1.0, "sales7d": 0.0, "purchases7d": 0, "unitsSoldClicks7d": 0}]
    queue_report(rows)
    reports.load(db, profile, reports.SPECS["placements"], day, day, client=client(), today=TODAY)
    stored = {r.placement: r for r in db.execute(select(PpcPlacement)).scalars()}
    assert set(stored) == {"top_of_search", "product_pages"}
    assert float(stored["top_of_search"].spend) == 8.0


def test_an_unfamiliar_placement_label_is_kept_rather_than_dropped(db, profile):
    assert reports.placement_key("Some New Placement") == "some_new_placement"
    assert reports.placement_key(None) == "unknown"


def test_what_an_ad_sold_that_was_not_the_advertised_asin_is_kept(db, profile):
    day = date(2026, 9, 15)
    queue_report([{"date": day.isoformat(), "campaignId": "C1", "adGroupId": "G1",
                   "advertisedAsin": "B0ADVERTISED", "purchasedAsin": "B0OTHER", "sales7d": 22.0,
                   "purchases7d": 1, "unitsSoldClicks7d": 1}])
    reports.load(db, profile, reports.SPECS["purchased_products"], day, day, client=client(),
                 today=TODAY)
    row = db.execute(select(PpcPurchasedProduct)).scalar_one()
    assert row.purchased_asin == "B0OTHER" and row.advertised_asin == "B0ADVERTISED"
    assert float(row.sales) == 22.0


def test_an_advertised_product_row_without_an_ad_id_is_keyed_by_asin(db, profile):
    day = date(2026, 9, 15)
    queue_report([{"date": day.isoformat(), "campaignId": "C1", "adGroupId": "G1",
                   "advertisedAsin": "B0GMW34NZC", "advertisedSku": "H3-NKLT-J0KI", "impressions": 40,
                   "clicks": 3, "cost": 1.5, "sales7d": 0.0, "purchases7d": 0, "unitsSoldClicks7d": 0}])
    reports.load(db, profile, reports.SPECS["advertised_products"], day, day, client=client(),
                 today=TODAY)
    row = db.execute(select(PpcMetricsDaily)).scalar_one()
    assert row.entity_type == "asin" and row.amazon_entity_id == "B0GMW34NZC"


# ---------------- the two clocks ----------------
def test_dates_are_the_marketplaces_own(db, profile):
    profile.timezone = "America/Los_Angeles"
    assert metrics.today_for(profile) is not None
    profile.timezone = "Not/AZone"
    assert metrics.today_for(profile) is not None, "an unreadable zone must not stop a load"


def test_the_re_pull_window_is_the_fortnight_ending_yesterday(profile):
    start, end = metrics.window(profile, days=14, today=TODAY)
    assert end == TODAY - timedelta(days=1) and (end - start).days + 1 == 14


def test_conclusions_wait_longer_than_recommendations_do(db, profile):
    """Traffic settles in days; conversions restate for weeks. Both gates are tested."""
    recent, old = TODAY - timedelta(days=10), TODAY - timedelta(days=40)
    queue_report([campaign_row(recent), campaign_row(old)])
    reports.load(db, profile, reports.SPECS["campaigns"], old, recent, client=client(), today=TODAY)
    final = metrics.daily(db, profile, entity_type="campaign", today=TODAY)
    settled = metrics.daily(db, profile, entity_type="campaign", settled=True, today=TODAY)
    assert {r.date for r in final} == {recent, old}, "both days are final for recommendations"
    assert {r.date for r in settled} == {old}, \
        "a ten-day-old day can still restate, so no experiment may conclude from it"
    assert metrics.is_settled(old, TODAY) and not metrics.is_settled(recent, TODAY)


# ---------------- the whole daily load ----------------
def test_the_daily_load_reads_every_report_and_overwrites_the_fortnight(db, profile):
    for index, key in enumerate(reports.DAILY_ORDER):
        rows = [campaign_row(TODAY - timedelta(days=5))] if key == "campaigns" else []
        queue_report(rows, report_id=f"RPT-{index}")
    stats = reports.load_recent(db, profile, client=client(), today=TODAY)
    assert len(stats["reports"]) == len(reports.DAILY_ORDER)
    assert stats["start"] == (TODAY - timedelta(days=14)).isoformat()
    assert stats["end"] == (TODAY - timedelta(days=1)).isoformat()
    assert stats["stored"] == 1
    requested = [c["json"]["configuration"]["reportTypeId"]
                 for c in FakeAdsClient.calls_to(r"/reporting/reports$")]
    assert requested.count("spCampaigns") == 3, "campaign, ad group and placement groupings"
    assert set(requested) == {"spCampaigns", "spTargeting", "spSearchTerm", "spAdvertisedProduct",
                              "spPurchasedProduct"}


# ---------------- the backfill ----------------
def test_the_backfill_plan_says_plainly_that_a_year_is_not_available(profile):
    planned = backfill.plan(profile, days=365, today=TODAY)
    assert planned["requested_days"] == 365
    assert planned["available_days"] == reports.RETENTION_DAYS - 1
    assert "95 days" in planned["note"] and "65 for search terms" in planned["note"]
    assert planned["coverage"]["search_terms"]["days"] == 64
    assert planned["coverage"]["campaigns"]["days"] == 94


def test_the_backfill_starts_at_the_oldest_day_amazon_still_has(profile):
    chunks = backfill.plan(profile, days=365, today=TODAY)["chunks"]
    assert chunks[0].start == TODAY - timedelta(days=reports.RETENTION_DAYS - 1)
    assert chunks == sorted(chunks, key=lambda c: (c.start, c.report)), \
        "oldest first, so an interrupted backfill leaves contiguous history"
    assert all((c.end - c.start).days + 1 <= reports.MAX_RANGE_DAYS for c in chunks)


def test_the_backfill_is_spread_out_and_never_queued_twice(db, profile):
    result = backfill.enqueue(db, profile, days=365, today=TODAY)
    again = backfill.enqueue(db, profile, days=365, today=TODAY)
    from app.db.models import Job

    jobs = db.execute(select(Job).where(Job.type == "ppc_backfill_chunk")).scalars().all()
    assert result["queued"] == result["requests"] and again["queued"] == 0
    assert len(jobs) == result["requests"]
    starts = sorted(j.run_after for j in jobs)
    assert starts[-1] > starts[0], "Amazon asks for one or two reports per type per day"


def test_a_backfill_chunk_loads_exactly_like_the_daily_job(db, profile):
    # Relative to the profile's real today, because run_chunk reads the calendar (D98).
    day = metrics.today_for(profile) - timedelta(days=40)
    queue_report([campaign_row(day)])
    result = backfill.run_chunk(db, {"profile_id": str(profile.id), "report": "campaigns",
                                     "start": day.isoformat(), "end": day.isoformat()},
                                client=client())
    assert result["stored"] == 1
    assert db.execute(select(PpcMetricsDaily)).scalar_one().is_provisional is False


def test_coverage_reports_how_much_history_there_actually_is(db, profile):
    day = metrics.today_for(profile) - timedelta(days=30)
    queue_report([campaign_row(day), campaign_row(day + timedelta(days=1))])
    reports.load(db, profile, reports.SPECS["campaigns"], day, day + timedelta(days=1),
                 client=client(), today=metrics.today_for(profile))
    got = backfill.coverage(db, profile)
    assert got["metrics"]["days"] == 2 and got["metrics"]["rows"] == 2
    assert got["search_terms"]["rows"] == 0


# ---------------- two rows for one key ----------------
def test_one_asin_advertised_in_two_campaigns_does_not_lose_the_whole_load(db, profile):
    """Postgres refuses to update a row twice in one statement, so the rows are merged first.

    This is not hypothetical: spAdvertisedProduct groups by advertiser, so an ASIN promoted in two
    campaigns arrives twice for the same day, and when Amazon omits `adId` the ASIN is our entity id.
    Before the merge this raised a CardinalityViolation and lost every report in the run.
    """
    day = date(2026, 9, 15)
    row = {"date": day.isoformat(), "advertisedAsin": "B0GMW34NZC", "advertisedSku": "SKU-1",
           "impressions": 40, "clicks": 3, "cost": 1.5, "sales7d": 10.0, "purchases7d": 1,
           "unitsSoldClicks7d": 1}
    queue_report([dict(row, campaignId="C1", adGroupId="G1"),
                  dict(row, campaignId="C2", adGroupId="G2", clicks=2, cost=1.0, sales7d=5.0)])
    result = reports.load(db, profile, reports.SPECS["advertised_products"], day, day,
                          client=client(), today=TODAY)
    stored = db.execute(select(PpcMetricsDaily)).scalar_one()
    assert result["stored"] == 1
    assert stored.clicks == 5 and float(stored.spend) == 2.5 and float(stored.sales) == 15.0, \
        "an ASIN-level row is the sum of the campaigns advertising it"
    assert stored.amazon_campaign_id is None, "the row is no longer about one campaign"


def test_a_merged_row_is_compared_as_a_whole_when_amazon_restates_it(db, profile):
    day = date(2026, 9, 10)
    row = {"date": day.isoformat(), "advertisedAsin": "B0X", "impressions": 10, "clicks": 1,
           "cost": 1.0, "sales7d": 5.0, "purchases7d": 1, "unitsSoldClicks7d": 1}
    queue_report([dict(row, campaignId="C1"), dict(row, campaignId="C2")])
    reports.load(db, profile, reports.SPECS["advertised_products"], day, day, client=client(),
                 today=TODAY)
    assert float(db.execute(select(PpcMetricsDaily)).scalar_one().sales) == 10.0
    queue_report([dict(row, campaignId="C1", sales7d=9.0), dict(row, campaignId="C2")],
                 report_id="RPT-2")
    reports.load(db, profile, reports.SPECS["advertised_products"], day, day, client=client(),
                 today=TODAY)
    change = db.execute(select(PpcMetricsRestatement)).scalar_one()
    assert float(change.old_sales) == 10.0 and float(change.new_sales) == 14.0, \
        "the restatement is measured against the total actually stored, not against one half of it"


def test_merging_never_adds_percentages_together(db, profile):
    merged = metrics.collapse(
        [{"k": 1, "impressions": 10, "top_of_search_impression_share": 30.0},
         {"k": 1, "impressions": 10, "top_of_search_impression_share": 40.0}], ("k",))
    assert len(merged) == 1 and merged[0]["impressions"] == 20
    assert merged[0]["top_of_search_impression_share"] == 40.0, "80% top-of-search would be nonsense"


def test_two_placement_rows_for_one_campaign_day_are_merged_not_fatal(db, profile):
    day = date(2026, 9, 15)
    row = {"date": day.isoformat(), "campaignId": "C1",
           "placementClassification": "Top of Search on-Amazon", "impressions": 100, "clicks": 4,
           "cost": 2.0, "sales7d": 8.0, "purchases7d": 1, "unitsSoldClicks7d": 1}
    queue_report([row, dict(row, clicks=1, cost=1.0)])
    reports.load(db, profile, reports.SPECS["placements"], day, day, client=client(), today=TODAY)
    stored = db.execute(select(PpcPlacement)).scalar_one()
    assert stored.clicks == 5 and float(stored.spend) == 3.0


# ---------------- the defects an adversarial review reproduced ----------------
def test_an_auto_campaigns_match_type_does_not_kill_the_load(db, profile):
    """Amazon reports TARGETING_EXPRESSION_PREDEFINED here — 31 characters into a 20-character column.

    Unhandled, the INSERT aborted the transaction, which discarded every report loaded in the same
    run, and did it again on every retry because the load re-reads the same fortnight. An account
    with an auto campaign would never have loaded anything.
    """
    day = date(2026, 9, 15)
    rows = [{"date": day.isoformat(), "campaignId": "C1", "adGroupId": "G1", "keywordId": "K1",
             "searchTerm": "mint pouches", "matchType": "TARGETING_EXPRESSION_PREDEFINED",
             "impressions": 10, "clicks": 1, "cost": 0.5, "sales7d": 0.0, "purchases7d": 0,
             "unitsSoldClicks7d": 0}]
    queue_report(rows)
    result = reports.load(db, profile, reports.SPECS["search_terms"], day, day, client=client(),
                          today=TODAY)
    stored = db.execute(select(PpcSearchTerm)).scalar_one()
    assert result["stored"] == 1 and stored.match_type == "auto"


@pytest.mark.parametrize("raw,expected", [
    ("EXACT", "exact"), ("BROAD", "broad"), ("TARGETING_EXPRESSION", "target"),
    ("TARGETING_EXPRESSION_PREDEFINED", "auto"), ("-", None), (None, None),
    ("SOMETHING_AMAZON_INVENTED_LATER_THAT_IS_VERY_LONG", "something_amazon_inv"),
])
def test_every_match_type_amazon_can_send_fits_the_column(raw, expected):
    got = reports.match_type(raw)
    assert got == expected
    assert got is None or len(got) <= 20


def test_a_real_accounts_fortnight_of_targets_is_stored(db, profile):
    """One INSERT per report died at about 2,600 rows: Postgres binds at most 65,535 parameters.

    A fortnight of 200 keywords is 2,800 rows, so the daily load failed on any real account and
    stored nothing at all.
    """
    days = [TODAY - timedelta(days=n) for n in range(1, 15)]
    rows = [{"date": d.isoformat(), "campaignId": "C1", "adGroupId": "G1", "keywordId": f"K{n}",
             "keyword": f"term {n}", "matchType": "EXACT", "impressions": 10, "clicks": 1,
             "cost": 0.25, "sales7d": 1.0, "purchases7d": 0, "unitsSoldClicks7d": 0}
            for d in days for n in range(200)]
    assert len(rows) == 2800
    queue_report(rows)
    result = reports.load(db, profile, reports.SPECS["targets"], days[-1], days[0], client=client(),
                          today=TODAY)
    assert result["stored"] == 2800
    assert len(db.execute(select(PpcMetricsDaily)).scalars().all()) == 2800


def test_a_fortnight_of_search_terms_is_stored(db, profile):
    days = [TODAY - timedelta(days=n) for n in range(1, 15)]
    rows = [{"date": d.isoformat(), "campaignId": "C1", "adGroupId": "G1", "keywordId": "K1",
             "searchTerm": f"pouches flavour {n}", "matchType": "BROAD", "impressions": 5, "clicks": 1,
             "cost": 0.2, "sales7d": 0.0, "purchases7d": 0, "unitsSoldClicks7d": 0}
            for d in days for n in range(250)]
    queue_report(rows)
    result = reports.load(db, profile, reports.SPECS["search_terms"], days[-1], days[0],
                          client=client(), today=TODAY)
    assert result["stored"] == 3500
    assert len(db.execute(select(PpcSearchTermDecision)).scalars().all()) == 250, \
        "one decision per term, however many days it appeared on"


def test_a_restated_search_term_is_logged_like_any_other_number(db, profile):
    day = date(2026, 9, 10)
    row = {"date": day.isoformat(), "campaignId": "C1", "adGroupId": "G1", "keywordId": "K1",
           "searchTerm": "nicotine pouches mint flavour strong", "matchType": "BROAD",
           "impressions": 100, "clicks": 9, "cost": 4.5, "sales7d": 30.0, "purchases7d": 2,
           "unitsSoldClicks7d": 2}
    queue_report([row])
    reports.load(db, profile, reports.SPECS["search_terms"], day, day, client=client(), today=TODAY)
    queue_report([dict(row, sales7d=55.0, purchases7d=3)], report_id="RPT-2")
    result = reports.load(db, profile, reports.SPECS["search_terms"], day, day, client=client(),
                          today=TODAY)
    change = db.execute(select(PpcMetricsRestatement)).scalar_one()
    assert result["restated"] == 1 and change.entity_type == "search_term"
    assert change.amazon_entity_id == "G1|nicotine pouches mint flavour strong", \
        "the id column is wide enough to name the term a shopper actually typed"
    assert float(change.old_sales) == 30.0 and float(change.new_sales) == 55.0


def test_a_restated_placement_is_logged(db, profile):
    day = date(2026, 9, 10)
    row = {"date": day.isoformat(), "campaignId": "C1",
           "placementClassification": "Top of Search on-Amazon", "impressions": 300, "clicks": 12,
           "cost": 8.0, "sales7d": 50.0, "purchases7d": 3, "unitsSoldClicks7d": 3}
    queue_report([row])
    reports.load(db, profile, reports.SPECS["placements"], day, day, client=client(), today=TODAY)
    queue_report([dict(row, sales7d=70.0)], report_id="RPT-2")
    reports.load(db, profile, reports.SPECS["placements"], day, day, client=client(), today=TODAY)
    change = db.execute(select(PpcMetricsRestatement)).scalar_one()
    assert change.entity_type == "placement" and change.amazon_entity_id == "C1|top_of_search"


def test_an_unrelated_400_does_not_make_us_drop_a_column(db, profile):
    """Several column names are ordinary words, so "invalid keyword filter" must not drop `keyword`."""
    assert reports._rejected_columns("Invalid keyword filter value", ["keyword", "date"]) == []
    assert reports._rejected_columns(
        "Unsupported column keyword for this groupBy", ["keyword", "date"]) == ["keyword"]
