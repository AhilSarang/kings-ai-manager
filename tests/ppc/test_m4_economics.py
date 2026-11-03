"""P4 M4: fees from Amazon, break-even and target ACOS, TACOS, and the PPC command.

The arithmetic here decides what every later milestone calls "too expensive", so it is tested
against worked examples rather than against itself.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.amazon import fees
from app.amazon.fake import FakeAmazonClient
from app.db.models import (AmazonAccount, AmazonListing, AmazonSalesDaily, Brand, PpcCampaign,
                           PpcMetricsDaily, PpcProductAd, PpcProductEconomics, PpcProfile, PpcRule,
                           Product)
from app.ppc import economics, tacos, views
from app.services import settings_store
from app.services.common import SYSTEM

MARKETPLACE = "ATVPDKIKX0DER"


@pytest.fixture
def profile(db):
    row = PpcProfile(amazon_profile_id="6666666666", marketplace_id=MARKETPLACE, country_code="US",
                     currency="USD", timezone="America/Los_Angeles", status="active")
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def account(db):
    row = AmazonAccount(seller_id="A1SELLER", region="na")
    db.add(row)
    db.commit()
    return row


def listing(db, account, *, asin="B0GMW34NZC", sku="H3-NKLT-J0KI", price=19.99, cost=4.20,
            brand=None, fulfillment="FBA"):
    product = None
    if cost is not None:
        product = Product(internal_name=f"product {sku}", unit_cost=cost,
                          brand_id=brand.id if brand else None)
        db.add(product)
        db.flush()
    row = AmazonListing(account_id=account.id, marketplace_id=MARKETPLACE, sku=sku, asin=asin,
                        price_cached=price, currency="USD", fulfillment=fulfillment,
                        product_id=product.id if product else None,
                        brand_id=brand.id if brand else None)
    db.add(row)
    db.commit()
    return row


def fee_response(*, referral=3.00, fba=3.22, asin="B0GMW34NZC", sku="H3-NKLT-J0KI", status="Success",
                 echo_identifier=True):
    """Amazon's real shape: the identifier we sent comes back in `SellerInputIdentifier`."""
    identity = {"IdType": "SellerSKU", "IdValue": sku, "MarketplaceId": MARKETPLACE}
    if echo_identifier:
        identity["SellerInputIdentifier"] = f"{asin}|{sku}"
    return {"FeesEstimateResultList": [{
        "Status": status,
        "FeesEstimateIdentifier": identity,
        "FeesEstimate": {"TimeOfFeesEstimation": "2026-09-25T00:00:00Z",
                         "TotalFeesEstimate": {"CurrencyCode": "USD", "Amount": referral + fba},
                         "FeeDetailList": [
                             {"FeeType": "ReferralFee",
                              "FeeAmount": {"CurrencyCode": "USD", "Amount": referral},
                              "FinalFee": {"CurrencyCode": "USD", "Amount": referral}},
                             {"FeeType": "FBAFees",
                              "FeeAmount": {"CurrencyCode": "USD", "Amount": fba},
                              "FinalFee": {"CurrencyCode": "USD", "Amount": fba}}]}}]}


# ---------------- Amazon's fees ----------------
def test_the_fee_request_is_the_shape_amazons_model_documents(db):
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response())
    fees.estimate([{"asin": "B0GMW34NZC", "sku": "H3-NKLT-J0KI", "price": 19.99, "fba": True}])
    call = FakeAmazonClient.calls_to("feesEstimate")[-1]
    body = call["json"]
    assert isinstance(body, list), "the endpoint takes a bare array, not an object"
    entry = body[0]
    assert entry["IdType"] == "SellerSKU" and entry["IdValue"] == "H3-NKLT-J0KI"
    request = entry["FeesEstimateRequest"]
    assert request["MarketplaceId"] == MARKETPLACE and request["IsAmazonFulfilled"] is True
    assert request["PriceToEstimateFees"]["ListingPrice"] == {"CurrencyCode": "USD", "Amount": 19.99}
    assert request["Identifier"] == "B0GMW34NZC|H3-NKLT-J0KI", "this is how the answer is matched back"


def test_amazons_fees_are_read_as_referral_and_fba(db):
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response(referral=3.00, fba=3.22))
    [got] = fees.estimate([{"asin": "B0GMW34NZC", "sku": "H3-NKLT-J0KI", "price": 19.99}])
    assert got.usable and got.referral_fee == 3.00 and got.fba_fee == 3.22
    assert got.all_fees == 6.22


def test_the_fee_actually_charged_is_the_one_used(db):
    """FinalFee is after promotions and tax; FeeAmount is before, and is not what you pay."""
    body = fee_response()
    body["FeesEstimateResultList"][0]["FeesEstimate"]["FeeDetailList"][0]["FinalFee"]["Amount"] = 2.10
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", body)
    [got] = fees.estimate([{"asin": "B0GMW34NZC", "sku": "H3-NKLT-J0KI", "price": 19.99}])
    assert got.referral_fee == 2.10


def test_an_asin_amazon_will_not_price_is_reported_not_guessed(db):
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", {"FeesEstimateResultList": [{
        "Status": "ClientError",
        "FeesEstimateIdentifier": {"IdType": "SellerSKU", "IdValue": "SKU-1",
                                   "SellerInputIdentifier": "B0GMW34NZC|SKU-1"},
        "Error": {"Type": "ClientError", "Code": "InvalidInput", "Message": "no such ASIN"}}]})
    [got] = fees.estimate([{"asin": "B0GMW34NZC", "sku": "SKU-1", "price": 19.99}])
    assert not got.usable and "no such ASIN" in got.error
    assert got.referral_fee is None, "a missing fee must never become zero"


def test_fees_are_asked_for_twenty_at_a_time(db):
    for _ in range(3):
        FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", {"FeesEstimateResultList": []})
    fees.estimate([{"asin": f"B0{n:08d}", "sku": f"S{n}", "price": 10.0} for n in range(45)])
    assert len(FakeAmazonClient.calls_to("feesEstimate")) == 3
    assert fees.FEES_PER_SECOND == 0.5, "this operation's own limit, ten times tighter than the rest"


# ---------------- break-even and target ----------------
def test_the_worked_example_from_the_brief(db, profile):
    """price 19.99, cost 4.20, referral 3.00, FBA 3.22 → margin 9.57, break-even 47.9%."""
    got = economics.compute(db, asin="B0X", price=19.99, unit_cost=4.20, referral_fee=3.00,
                            fba_fee=3.22)
    assert got.contribution_margin == 9.57
    assert got.break_even_acos == pytest.approx(0.4787, abs=0.0001)
    assert got.target_acos == pytest.approx(0.4787 * 0.70, abs=0.0005), "70% of break-even by default"
    assert got.target_source == "derived" and got.verified


def test_the_safety_margin_is_a_setting(db, profile, owner):
    settings_store.set_value(db, "ppc_target_acos_safety_margin_pct", 50, SYSTEM)
    got = economics.compute(db, asin="B0X", price=20.0, unit_cost=5.0, referral_fee=3.0, fba_fee=2.0)
    assert got.break_even_acos == 0.5 and got.target_acos == 0.25


def test_a_missing_cost_produces_no_target_at_all(db, profile):
    got = economics.compute(db, asin="B0X", price=19.99, unit_cost=None, referral_fee=3.00,
                            fba_fee=3.22)
    assert got.target_acos is None and got.break_even_acos is None
    assert got.missing == ["unit cost"] and not got.verified


def test_a_product_that_loses_money_says_so_rather_than_targeting_a_negative(db, profile):
    got = economics.compute(db, asin="B0X", price=10.0, unit_cost=8.0, referral_fee=1.50,
                            fba_fee=3.00)
    assert got.contribution_margin == -2.50 and got.target_acos is None
    assert "loses money on every sale" in got.note


def test_amits_own_target_beats_the_derived_one(db, profile):
    db.add(PpcRule(scope="asin", scope_ref="B0X", target_acos=0.20, active=True))
    db.commit()
    got = economics.compute(db, asin="B0X", price=20.0, unit_cost=5.0, referral_fee=3.0, fba_fee=2.0)
    assert got.target_acos == 0.20 and got.target_source == "owner"


def test_an_account_wide_rule_applies_where_there_is_no_asin_rule(db, profile):
    db.add(PpcRule(scope="account", target_acos=0.25, active=True))
    db.commit()
    assert economics.compute(db, asin="B0X", price=20.0, unit_cost=5.0, referral_fee=3.0,
                             fba_fee=2.0).target_acos == 0.25


# ---------------- history ----------------
def test_a_price_change_opens_a_new_row_and_closes_the_old_one(db, profile):
    first = economics.compute(db, asin="B0X", price=19.99, unit_cost=4.20, referral_fee=3.00,
                              fba_fee=3.22)
    economics.store(db, profile, first)
    second = economics.compute(db, asin="B0X", price=24.99, unit_cost=4.20, referral_fee=3.75,
                               fba_fee=3.22)
    economics.store(db, profile, second)
    rows = db.execute(select(PpcProductEconomics).order_by(PpcProductEconomics.valid_from)).scalars().all()
    assert len(rows) == 2
    assert rows[0].valid_to is not None and rows[1].valid_to is None
    assert float(rows[1].sale_price) == 24.99


def test_recomputing_with_the_same_numbers_writes_nothing(db, profile):
    for _ in range(3):
        economics.store(db, profile, economics.compute(
            db, asin="B0X", price=19.99, unit_cost=4.20, referral_fee=3.00, fba_fee=3.22))
    assert len(db.execute(select(PpcProductEconomics)).scalars().all()) == 1


def test_an_old_analysis_can_still_be_explained(db, profile):
    from app.services.common import utcnow

    economics.store(db, profile, economics.compute(db, asin="B0X", price=19.99, unit_cost=4.20,
                                                   referral_fee=3.00, fba_fee=3.22))
    db.commit()
    when = utcnow()
    economics.store(db, profile, economics.compute(db, asin="B0X", price=29.99, unit_cost=4.20,
                                                   referral_fee=4.50, fba_fee=3.22))
    db.commit()
    then = economics.at(db, "B0X", MARKETPLACE, when)
    now = economics.current(db, "B0X", MARKETPLACE)
    assert float(then.sale_price) == 19.99 and float(now.sale_price) == 29.99


# ---------------- the whole catalogue ----------------
def test_the_catalogue_refresh_uses_our_cost_and_amazons_fees(db, profile, account):
    listing(db, account)
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response())
    stats = economics.refresh(db, profile)
    row = economics.current(db, "B0GMW34NZC", MARKETPLACE)
    assert stats["asins"] == 1 and stats["with_fees"] == 1 and stats["missing_cost"] == []
    assert float(row.unit_cost) == 4.20 and float(row.referral_fee) == 3.00
    assert float(row.contribution_margin) == 9.57 and row.cost_verified is True


def test_an_asin_with_no_cost_is_flagged_and_asked_about_once(db, profile, account, sent,
                                                              no_quiet_hours, owner):
    from app.services.common import utcnow

    owner.last_inbound_at = utcnow()
    listing(db, account, cost=None)
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response())
    economics.refresh(db, profile)
    assert [r.asin for r in economics.unverified(db, profile)] == ["B0GMW34NZC"]

    assert economics.ask_for_missing_costs(db, profile) == 1
    db.commit()
    from tests.conftest import run_jobs

    run_jobs()
    assert economics.ask_for_missing_costs(db, profile) == 0, "asked once, not every morning"
    assert any("don't have your unit cost" in m.get("text", "") for m in sent)


# ---------------- TACOS ----------------
def spend_row(db, profile, day, *, entity="C1", entity_type="campaign", spend=10.0, sales=40.0,
              provisional=False):
    db.add(PpcMetricsDaily(ad_product="sp", amazon_profile_id=profile.amazon_profile_id, marketplace_id=MARKETPLACE,
                           entity_type=entity_type, amazon_entity_id=entity, date=day,
                           impressions=100, clicks=10, spend=spend, sales=sales, orders=2, units=2,
                           is_provisional=provisional))


def sales_row(db, day, *, asin="B0GMW34NZC", sales=200.0, units=10):
    db.add(AmazonSalesDaily(marketplace_id=MARKETPLACE, asin=asin, sku="H3-NKLT-J0KI", date=day,
                            units=units, ordered_product_sales=sales, currency="USD"))


def test_tacos_is_ad_spend_over_all_sales_not_just_advertised_ones(db, profile):
    from app.ppc import metrics

    today = metrics.today_for(profile)
    start, end = tacos.window(profile, 7, today=today)
    day = start
    while day <= end:
        spend_row(db, profile, day, spend=10.0, sales=40.0)
        sales_row(db, day, sales=200.0)
        day += timedelta(days=1)
    db.commit()
    got = tacos.for_account(db, profile, days=7, today=today)
    assert got.ad_spend == 70.0 and got.ad_sales == 280.0 and got.total_sales == 1400.0
    assert got.acos == 0.25, "advertising returned four times its cost"
    assert got.tacos == 0.05, "but it is only 5% of what the business sold"
    assert got.organic_sales == 1120.0 and got.ad_share == 0.2


def test_provisional_days_never_reach_a_published_number(db, profile):
    from app.ppc import metrics

    today = metrics.today_for(profile)
    spend_row(db, profile, today - timedelta(days=1), spend=99.0, sales=0.0, provisional=True)
    spend_row(db, profile, today - timedelta(days=5), spend=10.0, sales=40.0)
    sales_row(db, today - timedelta(days=5), sales=200.0)
    db.commit()
    got = tacos.for_account(db, profile, days=7, today=today)
    assert got.ad_spend == 10.0, "yesterday is still moving, so it is not in the figure"


def test_spend_with_no_sales_is_a_sentence_not_a_ratio(db, profile):
    from app.ppc import metrics

    today = metrics.today_for(profile)
    spend_row(db, profile, today - timedelta(days=5), spend=40.0, sales=0.0)
    db.commit()
    got = tacos.for_account(db, profile, days=7, today=today)
    assert got.ad_spend == 40.0 and got.tacos is None and got.acos is None
    assert any("no sales recorded" in n for n in got.notes)


def test_spend_is_counted_once_even_though_four_reports_carry_it(db, profile):
    """Campaign, ad group, target and advertised-product rows all hold the same cost."""
    from app.ppc import metrics

    today = metrics.today_for(profile)
    day = today - timedelta(days=5)
    for entity_type, entity in (("campaign", "C1"), ("ad_group", "G1"), ("target", "K1"),
                                ("asin", "B0GMW34NZC")):
        spend_row(db, profile, day, entity=entity, entity_type=entity_type, spend=10.0, sales=40.0)
    sales_row(db, day, sales=200.0)
    db.commit()
    assert tacos.for_account(db, profile, days=7, today=today).ad_spend == 10.0


def test_tacos_for_one_asin_uses_the_rows_that_name_an_asin(db, profile):
    from app.ppc import metrics

    today = metrics.today_for(profile)
    day = today - timedelta(days=5)
    spend_row(db, profile, day, entity="B0GMW34NZC", entity_type="asin", spend=12.0, sales=60.0)
    spend_row(db, profile, day, entity="B0OTHER", entity_type="asin", spend=90.0, sales=10.0)
    sales_row(db, day, asin="B0GMW34NZC", sales=300.0)
    db.commit()
    got = tacos.for_asin(db, profile, "B0GMW34NZC", days=7, today=today)
    assert got.ad_spend == 12.0 and got.total_sales == 300.0 and got.tacos == 0.04


def test_a_better_acos_bought_by_selling_less_is_flagged(db, profile):
    now = tacos.Tacos(scope="account", ref=None, start=date(2026, 9, 1), end=date(2026, 9, 7),
                      ad_spend=50.0, ad_sales=250.0, total_sales=600.0)
    before = tacos.Tacos(scope="account", ref=None, start=date(2026, 8, 25), end=date(2026, 8, 31),
                         ad_spend=100.0, ad_sales=400.0, total_sales=1000.0)
    assert now.acos == 0.2 and before.acos == 0.25, "ACOS improved"
    assert tacos.shrinking_the_business(now, before) is True, \
        "but the business sold 40% less, which is the trade that matters"


def test_growing_sales_at_the_same_acos_is_not_flagged(db, profile):
    now = tacos.Tacos(scope="account", ref=None, start=date(2026, 9, 1), end=date(2026, 9, 7),
                      ad_spend=120.0, ad_sales=600.0, total_sales=1400.0)
    before = tacos.Tacos(scope="account", ref=None, start=date(2026, 8, 25), end=date(2026, 8, 31),
                         ad_spend=100.0, ad_sales=400.0, total_sales=1000.0)
    assert tacos.shrinking_the_business(now, before) is False


def test_tacos_per_brand_follows_the_ads_back_to_the_listings(db, profile, account):
    from app.ppc import metrics

    # Skyzz and Vibezy are seeded by migration 0008; use the real row rather than a duplicate.
    brand = db.execute(select(Brand).where(Brand.name == "Skyzz")).scalar_one()
    row = listing(db, account, brand=brand)
    campaign = PpcCampaign(ad_product="sp", profile_id=profile.id, amazon_campaign_id="C1", name="Skyzz")
    db.add(campaign)
    db.flush()
    from app.db.models import PpcAdGroup

    group = PpcAdGroup(ad_product="sp", profile_id=profile.id, campaign_id=campaign.id, amazon_ad_group_id="G1",
                       amazon_campaign_id="C1", name="core")
    db.add(group)
    db.flush()
    db.add(PpcProductAd(ad_product="sp", profile_id=profile.id, campaign_id=campaign.id, ad_group_id=group.id,
                        amazon_ad_id="A1", asin=row.asin, sku=row.sku, listing_id=row.id))
    today = metrics.today_for(profile)
    day = today - timedelta(days=5)
    spend_row(db, profile, day, entity=row.asin, entity_type="asin", spend=20.0, sales=100.0)
    sales_row(db, day, asin=row.asin, sales=400.0)
    db.commit()
    got = tacos.for_brand(db, profile, brand, days=7, today=today)
    assert got.ad_spend == 20.0 and got.total_sales == 400.0 and got.tacos == 0.05


# ---------------- the command ----------------
def test_the_ppc_command_reports_both_numbers(db, profile, account):
    from app.ppc import metrics

    today = metrics.today_for(profile)
    start, end = tacos.window(profile, 7, today=today)
    day = start
    while day <= end:
        spend_row(db, profile, day, spend=10.0, sales=40.0)
        sales_row(db, day, sales=200.0)
        day += timedelta(days=1)
    db.commit()
    text = views.ppc_summary(db)
    assert "ACOS 25.0%" in text and "TACOS 5.0%" in text
    assert "read only" in text and "without your approval" in text


def test_the_ppc_command_explains_itself_before_ads_is_connected(db):
    text = views.ppc_summary(db)
    assert "isn't connected yet" in text and "separate registration" in text


def test_the_asin_view_says_when_a_target_cannot_be_trusted(db, profile, account):
    listing(db, account, cost=None)
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response())
    economics.refresh(db, profile)
    db.commit()
    text = views.asin_summary(db, "B0GMW34NZC")
    assert "B0GMW34NZC" in text and "unit cost" in text


def test_the_owner_can_give_a_cost_and_gets_the_target_straight_back(db, profile, account, owner):
    from app.services.common import Actor

    listing(db, account, cost=None)
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response())
    economics.refresh(db, profile)
    db.commit()
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response())
    answer = economics.set_unit_cost(db, "B0GMW34NZC", 4.20, Actor.user(owner.id))
    assert "break-even ACOS 47.9%" in answer and "aim at 33.5%" in answer
    row = economics.current(db, "B0GMW34NZC", MARKETPLACE)
    assert row.cost_verified is True and float(row.unit_cost) == 4.20


def test_a_cost_for_a_product_we_do_not_have_is_refused(db, profile, owner):
    from app.services.common import Actor

    answer = economics.set_unit_cost(db, "B0NOTMINE1", 4.20, Actor.user(owner.id))
    assert "can't find a listing" in answer


# ---------------- the command surface, as the owner types it ----------------
@pytest.mark.parametrize("text,expected", [
    ("PPC", "PPC"), ("ppc", "PPC"), ("ads", "PPC"), ("advertising", "PPC"),
    ("PPC ASIN B0GMW34NZC", "PPC_ASIN"), ("ppc B0GMW34NZC", "PPC_ASIN"),
    ("PPC BRANDS", "PPC_BRANDS"), ("ppc brand", "PPC_BRANDS"),
    ("PPC ECONOMICS", "PPC_ECONOMICS"), ("ppc margins", "PPC_ECONOMICS"),
    ("ppc break even", "PPC_ECONOMICS"), ("PPC PAUSE", "PPC_PAUSE"), ("ppc resume", "PPC_RESUME"),
    ("COST B0GMW34NZC 4.20", "PPC_COST"), ("cost B0GMW34NZC $4.20", "PPC_COST"),
])
def test_the_short_ways_of_asking_all_work(text, expected):
    from app.services import commands

    found = commands.match(text)
    assert found is not None, f"{text!r} matched nothing"
    assert found[0].name == expected


def test_a_bare_ppc_does_not_swallow_the_specific_ones():
    from app.services import commands

    assert commands.match("PPC ASIN B0GMW34NZC")[0].name == "PPC_ASIN"
    assert commands.match("PPC BRANDS")[0].name == "PPC_BRANDS"


def test_pausing_ppc_stops_the_agent_not_the_campaigns(db, owner):
    from app.jobs.handlers.ppc import ppc_paused
    from app.services.commands import COMMANDS, CommandContext

    pause = next(c for c in COMMANDS if c.name == "PPC_PAUSE")
    result = pause.handler(CommandContext(db, owner, None, pause.pattern.match("PPC PAUSE")))
    assert ppc_paused(db) is True
    assert "keep running at Amazon" in result.text, "pausing me must not sound like pausing the ads"


def test_tacos_is_available_day_by_day_not_only_as_a_rolling_figure(db, profile):
    """Brief 6.7 asks for daily as well as 7 and 30-day rolling."""
    from app.ppc import metrics

    today = metrics.today_for(profile)
    start, end = tacos.window(profile, 7, today=today)
    spend_row(db, profile, start, spend=10.0, sales=40.0)
    sales_row(db, start, sales=200.0)
    spend_row(db, profile, end, spend=20.0, sales=50.0)
    sales_row(db, end, sales=100.0)
    db.commit()
    series = tacos.daily_series(db, profile, days=7, today=today)
    assert len(series) == 7, "one row per day, including the quiet ones"
    assert series[0]["tacos"] == 0.05 and series[-1]["tacos"] == 0.2
    assert series[1]["tacos"] is None and series[1]["ad_spend"] == 0
    assert [r["start"] for r in series] == sorted(r["start"] for r in series)


def test_a_price_change_asks_for_the_economics_to_be_redone(db, account, profile, monkeypatch):
    """A target ACOS is a fraction of a price, so a price change makes every one of them wrong."""
    from pydantic import SecretStr

    from app.amazon import listings as listings_svc
    from app.config import get_settings
    from app.db.models import Job

    s = get_settings()
    monkeypatch.setattr(s, "ads_lwa_client_id", "c", raising=False)
    monkeypatch.setattr(s, "ads_lwa_client_secret", SecretStr("s"), raising=False)
    monkeypatch.setattr(s, "ads_lwa_refresh_token", SecretStr("r"), raising=False)
    row = listing(db, account, price=19.99)
    item = {"sku": row.sku, "summaries": [{"asin": row.asin, "itemName": "Skyzz pouches",
                                           "status": ["BUYABLE"]}],
            "offers": [{"price": {"amount": 24.99, "currencyCode": "USD"}}],
            "fulfillmentAvailability": [{"fulfillmentChannelCode": "AMAZON_NA"}]}
    listings_svc.upsert(db, account, MARKETPLACE, item)
    db.commit()
    queued = db.execute(select(Job).where(Job.type == "ppc_economics")).scalars().all()
    assert len(queued) == 1 and queued[0].payload["asins"] == [row.asin]


def test_the_two_reasons_a_target_is_untrustworthy_are_told_apart(db, profile, account):
    listing(db, account, asin="B0NOCOST001", sku="NC-1", price=19.99, cost=None)
    listing(db, account, asin="B0LOSES0001", sku="LM-1", price=10.00, cost=8.00)
    for _ in range(2):
        FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", {"FeesEstimateResultList": [
            {"Status": "Success",
             "FeesEstimateIdentifier": {"IdType": "SellerSKU", "IdValue": sku,
                                        "SellerInputIdentifier": f"{asin}|{sku}"},
             "FeesEstimate": {"TimeOfFeesEstimation": "2026-09-25T00:00:00Z",
                              "FeeDetailList": [
                                  {"FeeType": "ReferralFee",
                                   "FeeAmount": {"Amount": 1.50}, "FinalFee": {"Amount": 1.50}},
                                  {"FeeType": "FBAFees",
                                   "FeeAmount": {"Amount": 3.00}, "FinalFee": {"Amount": 3.00}}]}}
            for asin, sku in (("B0NOCOST001", "NC-1"), ("B0LOSES0001", "LM-1"))]})
        break
    economics.refresh(db, profile)
    db.commit()
    assert [r.asin for r in economics.missing_cost(db, profile)] == ["B0NOCOST001"]
    assert [r.asin for r in economics.loss_making(db, profile)] == ["B0LOSES0001"]
    text = views.ppc_summary(db)
    assert "don't have your unit cost" in text and "B0NOCOST001" in text
    assert "lose money on every sale" in text and "B0LOSES0001" in text
    assert "COST <ASIN>" in text


def test_per_asin_spend_exists_even_when_amazon_gives_an_ad_id(db, profile):
    """The advertised-product report is stored twice: by ad id, and by ASIN.

    Without the ASIN rows, `PPC ASIN B0…` and per-ASIN TACOS found no spend at all on any account
    whose ads carry ad ids — which is every normal account.
    """
    from app.ppc import metrics, reports
    from app.ppc.fake import FakeAdsClient

    day = metrics.today_for(profile) - timedelta(days=5)
    url = "https://ads-reports.test/AP.json.gz"
    FakeAdsClient.expect(r"/reporting/reports$", {"reportId": "AP", "status": "PENDING"})
    FakeAdsClient.expect(r"/reporting/reports/AP", {"reportId": "AP", "status": "COMPLETED",
                                                   "url": url})
    FakeAdsClient.downloads[url] = [
        {"date": day.isoformat(), "campaignId": "C1", "adGroupId": "G1", "adId": "A1",
         "advertisedAsin": "B0GMW34NZC", "advertisedSku": "SKU-1", "impressions": 50, "clicks": 4,
         "cost": 2.00, "sales7d": 20.0, "purchases7d": 1, "unitsSoldClicks7d": 1},
        {"date": day.isoformat(), "campaignId": "C2", "adGroupId": "G2", "adId": "A2",
         "advertisedAsin": "B0GMW34NZC", "advertisedSku": "SKU-1", "impressions": 30, "clicks": 2,
         "cost": 1.00, "sales7d": 5.0, "purchases7d": 0, "unitsSoldClicks7d": 0}]
    reports.load(db, profile, reports.SPECS["advertised_products"], day, day,
                 client=FakeAdsClient(), today=metrics.today_for(profile))
    by_kind = {}
    for row in db.execute(select(PpcMetricsDaily)).scalars():
        by_kind.setdefault(row.entity_type, []).append(row)
    assert {r.amazon_entity_id for r in by_kind["product_ad"]} == {"A1", "A2"}
    [asin_row] = by_kind["asin"]
    assert asin_row.amazon_entity_id == "B0GMW34NZC"
    assert float(asin_row.spend) == 3.00 and asin_row.clicks == 6, "the ASIN is the sum of its ads"

    sales_row(db, day, asin="B0GMW34NZC", sales=100.0)
    db.commit()
    got = tacos.for_asin(db, profile, "B0GMW34NZC", days=7, today=metrics.today_for(profile))
    assert got.ad_spend == 3.00 and got.tacos == 0.03


def test_a_canadian_listing_is_priced_with_canadian_fees(db, account, owner):
    """Asking Amazon for US fees on a CA listing produces a plausible, wrong margin."""
    from app.services.common import Actor

    ca = PpcProfile(amazon_profile_id="8888888888", marketplace_id="A2EUQ1WTGCTBG2",
                    country_code="CA", currency="CAD", timezone="America/Toronto", status="active")
    db.add(ca)
    product = Product(internal_name="pouches", unit_cost=4.20)
    db.add(product)
    db.flush()
    db.add(AmazonListing(account_id=account.id, marketplace_id="A2EUQ1WTGCTBG2", sku="CA-1",
                         asin="B0CANADA001", price_cached=27.99, currency="CAD", fulfillment="FBA",
                         product_id=product.id))
    db.commit()
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate",
                            fee_response(asin="B0CANADA001", sku="CA-1", referral=4.20, fba=4.00))
    economics.set_unit_cost(db, "B0CANADA001", 4.20, Actor.user(owner.id))
    call = FakeAmazonClient.calls_to("feesEstimate")[-1]
    request = call["json"][0]["FeesEstimateRequest"]
    assert request["MarketplaceId"] == "A2EUQ1WTGCTBG2", "Canada's marketplace, not the default US one"
    assert request["PriceToEstimateFees"]["ListingPrice"]["CurrencyCode"] == "CAD"


# ---------------- what the review found ----------------
def test_an_estimate_is_matched_back_by_the_field_amazon_actually_echoes(db):
    """Amazon echoes our Identifier in SellerInputIdentifier. Matching anywhere else drops every
    SKU-based estimate — which is all of them — and leaves the account with no margins at all."""
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response())
    [got] = fees.estimate([{"asin": "B0GMW34NZC", "sku": "H3-NKLT-J0KI", "price": 19.99}])
    assert got.usable and got.asin == "B0GMW34NZC" and got.sku == "H3-NKLT-J0KI"


def test_an_estimate_is_still_matched_when_amazon_echoes_nothing(db):
    """Fall back to the id we asked by, matched against the SKU or ASIN rather than as a prefix."""
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response(echo_identifier=False))
    [got] = fees.estimate([{"asin": "B0GMW34NZC", "sku": "H3-NKLT-J0KI", "price": 19.99}])
    assert got.usable and got.asin == "B0GMW34NZC"


def test_an_estimate_with_no_referral_fee_is_not_treated_as_a_bargain(db):
    body = fee_response()
    body["FeesEstimateResultList"][0]["FeesEstimate"]["FeeDetailList"] = [
        {"FeeType": "FBAFees", "FeeAmount": {"Amount": 3.22}, "FinalFee": {"Amount": 3.22}}]
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", body)
    [got] = fees.estimate([{"asin": "B0GMW34NZC", "sku": "H3-NKLT-J0KI", "price": 19.99}])
    assert not got.usable and "no referral fee" in got.error
    assert got.referral_fee is None, "every Amazon sale has one; its absence is a broken answer"


def test_the_fba_aggregate_and_its_parts_are_not_both_counted(db):
    body = fee_response(referral=3.00, fba=3.22)
    body["FeesEstimateResultList"][0]["FeesEstimate"]["FeeDetailList"] += [
        {"FeeType": "FBAPickAndPack", "FeeAmount": {"Amount": 2.22}, "FinalFee": {"Amount": 2.22}},
        {"FeeType": "FBAWeightHandling", "FeeAmount": {"Amount": 1.00}, "FinalFee": {"Amount": 1.00}}]
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", body)
    [got] = fees.estimate([{"asin": "B0GMW34NZC", "sku": "H3-NKLT-J0KI", "price": 19.99}])
    assert got.fba_fee == 3.22, "the aggregate already contains the parts"


def test_fba_parts_alone_are_added_up(db):
    body = fee_response(referral=3.00)
    body["FeesEstimateResultList"][0]["FeesEstimate"]["FeeDetailList"] = [
        {"FeeType": "ReferralFee", "FeeAmount": {"Amount": 3.00}, "FinalFee": {"Amount": 3.00}},
        {"FeeType": "FBAPickAndPack", "FeeAmount": {"Amount": 2.22}, "FinalFee": {"Amount": 2.22}},
        {"FeeType": "FBAWeightHandling", "FeeAmount": {"Amount": 1.00}, "FinalFee": {"Amount": 1.00}}]
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", body)
    [got] = fees.estimate([{"asin": "B0GMW34NZC", "sku": "H3-NKLT-J0KI", "price": 19.99}])
    assert got.fba_fee == 3.22


def test_a_fee_we_have_no_name_for_still_counts_against_the_margin(db):
    body = fee_response(referral=3.00, fba=3.22)
    body["FeesEstimateResultList"][0]["FeesEstimate"]["FeeDetailList"].append(
        {"FeeType": "SomeNewAmazonFee", "FeeAmount": {"Amount": 0.99},
         "FinalFee": {"Amount": 0.99}})
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", body)
    [got] = fees.estimate([{"asin": "B0GMW34NZC", "sku": "H3-NKLT-J0KI", "price": 19.99}])
    assert got.other_fees == 0.99 and got.all_fees == 7.21, \
        "an unknown fee must reduce the margin, not be dropped from it"


def test_two_sales_rows_for_one_asin_day_do_not_double_the_sales(db, profile):
    """An older Phase 3 loader could label the same ASIN-day with two different SKUs."""
    from app.ppc import metrics

    today = metrics.today_for(profile)
    start, end = tacos.window(profile, 7, today=today)
    db.add(AmazonSalesDaily(marketplace_id=MARKETPLACE, asin="B0GMW34NZC", sku="SKU-A", date=start,
                            units=10, ordered_product_sales=200.0, currency="USD"))
    db.add(AmazonSalesDaily(marketplace_id=MARKETPLACE, asin="B0GMW34NZC", sku="SKU-B", date=start,
                            units=10, ordered_product_sales=200.0, currency="USD"))
    spend_row(db, profile, start, spend=10.0, sales=40.0)
    db.commit()
    got = tacos.for_account(db, profile, days=7, today=today)
    assert got.total_sales == 200.0, "the ASIN sold 200 that day, whatever it is labelled"
    assert got.tacos == 0.05
    assert tacos.daily_series(db, profile, days=7, today=today)[0]["total_sales"] == 200.0


def test_the_sales_load_is_idempotent_across_runs(db, account):
    """The row is keyed by ASIN and day; a re-run must update it, not add a second one."""
    from app.amazon import monitor
    from app.amazon.fake import FakeAmazonClient as F
    from app.amazon.marketplaces import by_code

    listing(db, account, asin="B0GMW34NZC", sku="SKU-A")
    listing(db, account, asin="B0GMW34NZC", sku="SKU-B", cost=None)
    payload = {"salesAndTrafficByAsin": [
        {"childAsin": "B0GMW34NZC", "date": "2026-09-20",
         "salesByAsin": {"unitsOrdered": 10,
                         "orderedProductSales": {"amount": 200.0, "currencyCode": "USD"}},
         "trafficByAsin": {"sessions": 100}}]}
    for _ in range(2):
        F.expect(r"/reports/2021-06-30/reports$", {"reportId": "R1"})
        F.expect(r"/reports/2021-06-30/reports/R1",
                 {"processingStatus": "DONE", "reportDocumentId": "D1"})
        F.expect(r"/reports/2021-06-30/documents/D1", {"url": "https://x/doc"})
    import gzip
    import json as jsonlib

    import app.amazon.reports as reports_mod

    def fake_get(url, **kw):
        class R:
            status_code = 200
            content = gzip.compress(jsonlib.dumps(payload).encode())
        return R()

    original = reports_mod.httpx.get
    reports_mod.httpx.get = fake_get
    try:
        monitor.collect_sales(db, days=1, marketplace_code="US")
        monitor.collect_sales(db, days=1, marketplace_code="US")
    finally:
        reports_mod.httpx.get = original
    rows = db.execute(select(AmazonSalesDaily).where(
        AmazonSalesDaily.asin == "B0GMW34NZC", AmazonSalesDaily.date == date(2026, 9, 20))).scalars().all()
    assert len(rows) == 1, "one ASIN-day is one row, however many SKUs share the ASIN"
    assert float(rows[0].ordered_product_sales) == 200.0


def test_a_brands_tacos_divides_by_everything_the_brand_sold(db, profile, account):
    """Dividing by only the advertised products' sales flatters a brand that advertises one of ten."""
    from app.db.models import PpcAdGroup
    from app.ppc import metrics

    brand = db.execute(select(Brand).where(Brand.name == "Skyzz")).scalar_one()
    advertised = listing(db, account, asin="B0ADVERT001", sku="AD-1", brand=brand)
    quiet = listing(db, account, asin="B0QUIET0001", sku="QT-1", brand=brand, cost=None)
    campaign = PpcCampaign(ad_product="sp", profile_id=profile.id, amazon_campaign_id="C1", name="Skyzz")
    db.add(campaign)
    db.flush()
    group = PpcAdGroup(ad_product="sp", profile_id=profile.id, campaign_id=campaign.id, amazon_ad_group_id="G1",
                       amazon_campaign_id="C1", name="core")
    db.add(group)
    db.flush()
    db.add(PpcProductAd(ad_product="sp", profile_id=profile.id, campaign_id=campaign.id, ad_group_id=group.id,
                        amazon_ad_id="A1", asin=advertised.asin, sku=advertised.sku,
                        listing_id=advertised.id))
    today = metrics.today_for(profile)
    start, _end = tacos.window(profile, 7, today=today)
    spend_row(db, profile, start, entity=advertised.asin, entity_type="asin", spend=10.0, sales=50.0)
    sales_row(db, start, asin=advertised.asin, sales=100.0)
    sales_row(db, start, asin=quiet.asin, sales=300.0)
    db.commit()
    got = tacos.for_brand(db, profile, brand, days=7, today=today)
    assert got.ad_spend == 10.0
    assert got.total_sales == 400.0, "the brand sold 400, only 100 of it advertised"
    assert got.tacos == 0.025


@pytest.mark.parametrize("now_sales,now_spend,now_ad_sales,flagged", [
    (600.0, 50.0, 250.0, True),     # ACOS better, sales down 40%
    (1400.0, 120.0, 600.0, False),  # sales up
    (600.0, 50.0, 0.0, True),       # sales collapsed and nothing attributed — the worst case
    (995.0, 90.0, 400.0, False),    # sales within tolerance
])
def test_the_shrink_flag_catches_the_trade_it_exists_for(now_sales, now_spend, now_ad_sales, flagged):
    before = tacos.Tacos(scope="account", ref=None, start=date(2026, 8, 25), end=date(2026, 8, 31),
                         ad_spend=100.0, ad_sales=400.0, total_sales=1000.0)
    now = tacos.Tacos(scope="account", ref=None, start=date(2026, 9, 1), end=date(2026, 9, 7),
                      ad_spend=now_spend, ad_sales=now_ad_sales, total_sales=now_sales)
    assert tacos.shrinking_the_business(now, before) is flagged


def test_flat_acos_with_falling_sales_is_the_same_bad_trade():
    """Requiring a *visible* ACOS improvement let a 10% drop through whenever ACOS was flat."""
    before = tacos.Tacos(scope="account", ref=None, start=date(2026, 8, 25), end=date(2026, 8, 31),
                         ad_spend=100.0, ad_sales=400.0, total_sales=1000.0)
    now = tacos.Tacos(scope="account", ref=None, start=date(2026, 9, 1), end=date(2026, 9, 7),
                      ad_spend=95.0, ad_sales=381.0, total_sales=900.0)
    assert round(now.acos, 4) == pytest.approx(round(before.acos, 4), abs=0.001), "ACOS barely moved"
    assert tacos.shrinking_the_business(now, before) is True, "but a tenth of the business went"


def test_paying_more_for_less_is_left_to_a_different_analyser():
    """Sales down and ACOS clearly worse is a problem, but not the trade this flag is about."""
    before = tacos.Tacos(scope="account", ref=None, start=date(2026, 8, 25), end=date(2026, 8, 31),
                         ad_spend=100.0, ad_sales=400.0, total_sales=1000.0)
    now = tacos.Tacos(scope="account", ref=None, start=date(2026, 9, 1), end=date(2026, 9, 7),
                      ad_spend=150.0, ad_sales=300.0, total_sales=700.0)
    assert now.acos > before.acos
    assert tacos.shrinking_the_business(now, before) is False


def test_reports_disagreeing_is_said_not_hidden():
    got = tacos.Tacos(scope="asin", ref="B0X", start=date(2026, 9, 1), end=date(2026, 9, 7),
                      ad_spend=10.0, ad_sales=500.0, total_sales=300.0)
    assert got.organic_sales == 0.0
    assert any("disagree" in n for n in got.notes), "a clamp that says nothing hides a data problem"


def test_a_mistyped_cost_flags_one_product_rather_than_breaking_the_run(db, profile, account):
    """420 instead of 4.20: the ratio must not overflow Numeric(6,4) and abort the transaction."""
    got = economics.compute(db, asin="B0X", price=19.99, unit_cost=420.00, referral_fee=3.00,
                            fba_fee=3.22)
    economics.store(db, profile, got)
    db.commit()
    row = economics.current(db, "B0X", MARKETPLACE)
    assert float(row.contribution_margin) < 0 and row.target_acos is None
    assert "loses money" in row.source_note


def test_two_skus_on_one_asin_do_not_churn_the_history(db, profile, account):
    listing(db, account, asin="B0SHARED001", sku="SH-A", price=19.99, cost=4.20)
    listing(db, account, asin="B0SHARED001", sku="SH-B", price=None, cost=None)
    for _ in range(3):
        FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate",
                                fee_response(asin="B0SHARED001", sku="SH-A"))
        economics.refresh(db, profile)
        db.commit()
    rows = db.execute(select(PpcProductEconomics).where(
        PpcProductEconomics.asin == "B0SHARED001")).scalars().all()
    assert len(rows) == 1, "one row per ASIN, not one per refresh"
    assert rows[0].sku == "SH-A", "the SKU that can actually be priced"


def test_amits_extra_costs_reach_the_margin(db, profile, account, owner):
    from app.services.common import Actor

    economics.set_extra_cost(db, "B0GMW34NZC", 1.00, Actor.user(owner.id))
    listing(db, account)
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response())
    economics.refresh(db, profile)
    row = economics.current(db, "B0GMW34NZC", MARKETPLACE)
    assert float(row.other_costs) == 1.00
    assert float(row.contribution_margin) == 8.57, "9.57 less the pound the owner says it also costs him"


def test_amazons_other_fees_reach_the_margin_too(db, profile, account):
    body = fee_response(referral=3.00, fba=3.22)
    body["FeesEstimateResultList"][0]["FeesEstimate"]["FeeDetailList"].append(
        {"FeeType": "VariableClosingFee", "FeeAmount": {"Amount": 0.50},
         "FinalFee": {"Amount": 0.50}})
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", body)
    listing(db, account)
    economics.refresh(db, profile)
    row = economics.current(db, "B0GMW34NZC", MARKETPLACE)
    assert float(row.other_costs) == 0.50 and float(row.contribution_margin) == 9.07


def test_a_bare_ppc_does_not_swallow_an_instruction():
    """An ungrouped alternation binds the anchors to one alternative each."""
    from app.services import commands

    assert commands.match("PPC")[0].name == "PPC"
    assert commands.match("ads")[0].name == "PPC"
    for real_instruction in ("ppc please cut the bids on skyzz",
                             "ads are too expensive lately, do something",
                             "advertising is eating the margin"):
        assert commands.match(real_instruction) is None, \
            f"{real_instruction!r} is for the agent to read, not a summary command"


def test_the_summary_says_when_it_is_paused(db, profile, owner):
    from app.services.common import Actor

    settings_store.set_value(db, "ppc_paused", True, Actor.user(owner.id))
    text = views.ppc_summary(db)
    assert "paused" in text and "still running at Amazon" in text


def test_the_summary_does_not_claim_zero_campaigns_before_it_has_looked(db, profile):
    assert "haven't read the account's campaigns yet" in views.ppc_summary(db)
    profile.last_synced_at = __import__("app.services.common", fromlist=["utcnow"]).utcnow()
    db.commit()
    assert "0 campaigns running" in views.ppc_summary(db)


def test_a_missing_fee_estimate_is_not_shown_as_amazon_taking_nothing(db, profile, account):
    listing(db, account)
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", {"FeesEstimateResultList": [{
        "Status": "ClientError",
        "FeesEstimateIdentifier": {"IdType": "SellerSKU", "IdValue": "H3-NKLT-J0KI",
                                   "SellerInputIdentifier": "B0GMW34NZC|H3-NKLT-J0KI"},
        "Error": {"Message": "no estimate"}}]})
    economics.refresh(db, profile)
    db.commit()
    text = views.asin_summary(db, "B0GMW34NZC")
    assert "Amazon's cut not known yet" in text and "$0.00" not in text


def test_a_cost_command_with_a_silly_number_is_questioned_not_stored(db, profile, account, owner):
    from app.services.commands import COMMANDS, CommandContext

    listing(db, account)
    cost = next(c for c in COMMANDS if c.name == "PPC_COST")
    result = cost.handler(CommandContext(db, owner, None,
                                         cost.pattern.match("COST B0GMW34NZC 99999999")))
    assert "doesn't look like a unit cost" in result.text
    assert economics.current(db, "B0GMW34NZC", MARKETPLACE) is None


def test_the_first_cost_does_not_claim_it_was_already_that(db, profile, account, owner):
    from app.services.common import Actor

    listing(db, account, cost=None)
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response())
    answer = economics.set_unit_cost(db, "B0GMW34NZC", 4.20, Actor.user(owner.id))
    assert "(was 4.20)" not in answer
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response())
    answer = economics.set_unit_cost(db, "B0GMW34NZC", 5.00, Actor.user(owner.id))
    assert "(was 4.20)" in answer


def test_amazons_other_fees_survive_a_later_cost_correction(db, profile, account, owner):
    """Re-running COST reused the stored estimate but dropped its other fees from the margin."""
    from app.services.common import Actor

    body = fee_response(referral=3.00, fba=3.22)
    body["FeesEstimateResultList"][0]["FeesEstimate"]["FeeDetailList"].append(
        {"FeeType": "VariableClosingFee", "FeeAmount": {"Amount": 1.80},
         "FinalFee": {"Amount": 1.80}})
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", body)
    listing(db, account)
    economics.refresh(db, profile)
    db.commit()
    assert float(economics.current(db, "B0GMW34NZC", MARKETPLACE).contribution_margin) == 7.77

    # No fee response queued: the price hasn't moved, so the stored estimate must be reused whole.
    economics.set_unit_cost(db, "B0GMW34NZC", 5.00, Actor.user(owner.id))
    row = economics.current(db, "B0GMW34NZC", MARKETPLACE)
    assert float(row.other_costs) == 1.80, "Amazon's closing fee must not vanish"
    assert float(row.contribution_margin) == 6.97


def test_a_new_product_without_a_cost_is_asked_about_even_after_an_earlier_ask(db, profile, account,
                                                                              sent, no_quiet_hours,
                                                                              owner):
    """A fixed message key marked later ASINs as asked while suppressing the message."""
    from tests.conftest import run_jobs

    from app.services.common import utcnow

    owner.last_inbound_at = utcnow()
    listing(db, account, asin="B0FIRST0001", sku="F-1", cost=None)
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate",
                            fee_response(asin="B0FIRST0001", sku="F-1"))
    economics.refresh(db, profile)
    assert economics.ask_for_missing_costs(db, profile) == 1
    db.commit()
    run_jobs()

    listing(db, account, asin="B0SECOND001", sku="S-1", cost=None)
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate",
                            fee_response(asin="B0SECOND001", sku="S-1"))
    economics.refresh(db, profile)
    assert economics.ask_for_missing_costs(db, profile) == 1, "the new product is still unasked"
    db.commit()
    run_jobs()
    assert any("B0SECOND001" in m.get("text", "") for m in sent), \
        "a product added later must not be silently marked as asked about"


def test_a_hand_entered_cost_is_audited(db, profile, account, owner):
    """It rewrites every target ACOS derived from it, so who set it is worth keeping."""
    from app.db.models import AuditLog
    from app.services.common import Actor

    listing(db, account, cost=None)
    FakeAmazonClient.expect(r"/products/fees/v0/feesEstimate", fee_response())
    economics.set_unit_cost(db, "B0GMW34NZC", 4.20, Actor.user(owner.id))
    entry = db.execute(select(AuditLog).where(AuditLog.action == "ppc_unit_cost_set")).scalar_one()
    assert entry.details["asin"] == "B0GMW34NZC" and entry.details["new"] == 4.20
    assert entry.actor_id == str(owner.id)
