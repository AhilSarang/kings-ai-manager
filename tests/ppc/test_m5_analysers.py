"""P4 M5: the deterministic analysers, the context filters, and the cooldown.

Each analyser is a function of a `Scene`, so these build an account out of fixture rows and assert on
the decision it produces — including the numbers it shows, because a recommendation without its
evidence is not a recommendation (brief 7.2).
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (AmazonAccount, AmazonInventorySnapshot, AmazonListing, AmazonSalesDaily,
                           PpcAction, PpcAdGroup, PpcCampaign, PpcMetricsDaily, PpcPlacement,
                           PpcProductAd, PpcProfile, PpcRule, PpcSearchTerm, PpcSearchTermDecision,
                           PpcTarget, Product)
from app.ppc import analysers, candidates, context, economics
from app.services import settings_store
from app.services.common import SYSTEM, utcnow

MARKETPLACE = "ATVPDKIKX0DER"
ASIN = "B0GMW34NZC"
SKU = "H3-NKLT-J0KI"


@pytest.fixture
def profile(db):
    row = PpcProfile(amazon_profile_id="7070707070", marketplace_id=MARKETPLACE, country_code="US",
                     currency="USD", timezone="America/Los_Angeles", status="active",
                     last_synced_at=utcnow())
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def account(db):
    row = AmazonAccount(seller_id="A1SELLER", region="na")
    db.add(row)
    db.commit()
    return row


class Build:
    """A small account, assembled a piece at a time so each test says what it needs."""

    def __init__(self, db, profile, account):
        self.db, self.profile, self.account = db, profile, account
        from app.ppc import metrics

        self.today = metrics.today_for(profile)
        self.start, self.end = analysers.aggregates.window(profile, 30, today=self.today)

    # ---- catalogue ----
    def listing(self, *, asin=ASIN, sku=SKU, price=19.99, cost=4.20, stock=500, sales_per_day=6,
                buyable=True, buy_box=95.0, brand_id=None):
        product = Product(internal_name=sku, unit_cost=cost)
        self.db.add(product)
        self.db.flush()
        row = AmazonListing(account_id=self.account.id, marketplace_id=MARKETPLACE, sku=sku,
                            asin=asin, price_cached=price, currency="USD", fulfillment="FBA",
                            product_id=product.id, buyable=buyable, brand_id=brand_id,
                            status_cached="BUYABLE" if buyable else "INACTIVE")
        self.db.add(row)
        if stock is not None:
            self.db.add(AmazonInventorySnapshot(marketplace_id=MARKETPLACE, sku=sku, asin=asin,
                                                fulfillable_qty=stock, snapshot_at=utcnow()))
        day = self.today - timedelta(days=14)
        while day < self.today:
            self.db.add(AmazonSalesDaily(marketplace_id=MARKETPLACE, asin=asin, sku=sku, date=day,
                                         units=sales_per_day, ordered_product_sales=price * sales_per_day,
                                         currency="USD", buy_box_pct=buy_box))
            day += timedelta(days=1)
        self.db.flush()
        return row

    def economics(self, *, asin=ASIN, price=19.99, cost=4.20, referral=3.00, fba=3.22):
        got = economics.compute(self.db, asin=asin, price=price, unit_cost=cost,
                                referral_fee=referral, fba_fee=fba)
        economics.store(self.db, self.profile, got)
        self.db.flush()
        return got

    # ---- account shape ----
    def campaign(self, *, campaign_id="C1", name="Skyzz exact", budget=40.0, targeting="manual",
                 state="enabled", placements=None):
        row = PpcCampaign(ad_product="sp", profile_id=self.profile.id, amazon_campaign_id=campaign_id, name=name,
                          daily_budget=budget, targeting_type=targeting, state=state,
                          placement_adjustments=placements or [])
        self.db.add(row)
        self.db.flush()
        return row

    def ad_group(self, campaign, *, ad_group_id="G1", name="core", asin=ASIN, sku=SKU,
                 listing=None):
        row = PpcAdGroup(ad_product="sp", profile_id=self.profile.id, campaign_id=campaign.id,
                         amazon_ad_group_id=ad_group_id,
                         amazon_campaign_id=campaign.amazon_campaign_id, name=name, default_bid=0.75)
        self.db.add(row)
        self.db.flush()
        if asin:
            self.db.add(PpcProductAd(ad_product="sp", profile_id=self.profile.id, campaign_id=campaign.id,
                                     ad_group_id=row.id, amazon_ad_id=f"A-{ad_group_id}", asin=asin,
                                     sku=sku, listing_id=listing.id if listing else None))
        self.db.flush()
        return row

    def target(self, campaign, group, *, target_id="K1", text="nicotine pouches", bid=1.00,
               match="exact", kind="keyword", state="enabled"):
        row = PpcTarget(ad_product="sp", profile_id=self.profile.id, campaign_id=campaign.id, ad_group_id=group.id,
                        amazon_target_id=target_id,
                        amazon_campaign_id=campaign.amazon_campaign_id,
                        amazon_ad_group_id=group.amazon_ad_group_id, target_kind=kind,
                        target_type="keyword" if kind == "keyword" else "product",
                        expression=text, match_type=match, bid=bid, state=state)
        self.db.add(row)
        self.db.flush()
        return row

    # ---- metrics ----
    def metrics(self, *, entity_type, entity_id, clicks, orders, spend, sales, days=28,
                campaign_id="C1", ad_group_id="G1", impressions=None, top_share=None):
        per_day_clicks = max(clicks // days, 0)
        left = clicks - per_day_clicks * days
        for n in range(days):
            day = self.end - timedelta(days=n)
            if day < self.start:
                break
            c = per_day_clicks + (left if n == 0 else 0)
            self.db.add(PpcMetricsDaily(ad_product="sp", 
                amazon_profile_id=self.profile.amazon_profile_id, marketplace_id=MARKETPLACE,
                entity_type=entity_type, amazon_entity_id=entity_id, date=day,
                amazon_campaign_id=campaign_id, amazon_ad_group_id=ad_group_id,
                impressions=(impressions or clicks * 20) // days, clicks=c,
                spend=round(spend / days, 2), sales=round(sales / days, 2),
                orders=orders if n == 0 else 0, units=orders if n == 0 else 0,
                top_of_search_impression_share=top_share, is_provisional=False))
        self.db.flush()

    def day_of_spend(self, entity_id, *, day, spend, sales=0.0, clicks=10, orders=0,
                     entity_type="campaign"):
        self.db.add(PpcMetricsDaily(ad_product="sp", 
            amazon_profile_id=self.profile.amazon_profile_id, marketplace_id=MARKETPLACE,
            entity_type=entity_type, amazon_entity_id=entity_id, date=day,
            amazon_campaign_id=entity_id if entity_type == "campaign" else "C1",
            clicks=clicks, spend=spend, sales=sales, orders=orders, impressions=clicks * 20,
            is_provisional=False))
        self.db.flush()

    def term(self, *, text, clicks, orders, spend, sales, ad_group_id="G1", campaign_id="C1",
             target_id="K1", status=None, days=4):
        for n in range(days):
            day = self.end - timedelta(days=n)
            self.db.add(PpcSearchTerm(ad_product="sp", 
                amazon_profile_id=self.profile.amazon_profile_id, marketplace_id=MARKETPLACE,
                amazon_campaign_id=campaign_id, amazon_ad_group_id=ad_group_id,
                amazon_target_id=target_id, search_term=text, date=day,
                impressions=clicks * 15 // days, clicks=clicks // days,
                spend=round(spend / days, 2), sales=round(sales / days, 2),
                orders=orders if n == 0 else 0, units=orders if n == 0 else 0,
                is_provisional=False))
        self.db.add(PpcSearchTermDecision(ad_product="sp", 
            amazon_profile_id=self.profile.amazon_profile_id, amazon_campaign_id=campaign_id,
            amazon_ad_group_id=ad_group_id, search_term=text, harvest_status=status or "new"))
        self.db.flush()

    def placement(self, *, campaign_id="C1", placement="top_of_search", clicks, spend, sales,
                  orders=1, days=4):
        for n in range(days):
            self.db.add(PpcPlacement(ad_product="sp", 
                amazon_profile_id=self.profile.amazon_profile_id, marketplace_id=MARKETPLACE,
                amazon_campaign_id=campaign_id, placement=placement, date=self.end - timedelta(days=n),
                clicks=clicks // days, impressions=clicks * 20 // days,
                spend=round(spend / days, 2), sales=round(sales / days, 2),
                orders=orders if n == 0 else 0, is_provisional=False))
        self.db.flush()

    def scene(self):
        self.db.commit()
        return analysers.load(self.db, self.profile, today=self.today)


@pytest.fixture
def build(db, profile, account):
    return Build(db, profile, account)


def only(scene, name):
    return analysers.ANALYSERS[name](scene)


# ---------------- 1. wasted spend ----------------
def test_clicks_with_no_orders_past_the_point_of_doubt_are_paused(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=60, orders=0, spend=30.0, sales=0.0)
    [got] = only(build.scene(), "wasted_spend")
    assert got.change_type == "pause_target" and got.entity_id == "K1"
    assert got.evidence["clicks"] == 60 and got.evidence["orders"] == 0
    assert got.evidence["needed_cvr"] and got.evidence["clicks_before_certain"]
    assert got.evidence["target_acos"] == pytest.approx(0.3351, abs=0.001)
    assert "past the point where this is bad luck" in got.reason
    assert got.monthly_effect > 0


def test_a_dry_target_that_is_not_yet_proven_dead_gets_its_bid_cut(build):
    """30 dry clicks damns a cheap product and says little about an expensive one."""
    listing = build.listing(price=90.00, cost=20.00)
    build.economics(price=90.00, cost=20.00, referral=13.50, fba=5.00)
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=32, orders=0, spend=32.0, sales=0.0)
    [got] = only(build.scene(), "wasted_spend")
    assert got.change_type == "bid_decrease"
    assert got.proposed_value == 0.75, "capped at 25% a step"
    assert got.evidence["clicks_before_certain"] > 32


def test_too_few_clicks_is_not_a_finding(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    build.metrics(entity_type="target", entity_id="K1", clicks=12, orders=0, spend=6.0, sales=0.0)
    assert only(build.scene(), "wasted_spend") == []


# ---------------- 2. over-target ACOS ----------------
def test_a_target_costing_more_than_the_product_can_afford_is_cut(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    # 40 clicks, 4 orders, £40 spend on £50 of sales → 80% ACOS against a 33.5% target.
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=4, spend=40.0, sales=50.0)
    [got] = only(build.scene(), "over_target_acos")
    assert got.change_type == "bid_decrease" and got.current_value == 1.00
    assert got.proposed_value == 0.75, "the arithmetic wants 0.42; 25% a step is the cap"
    assert any("capped to 25% a step" in n for n in got.notes)
    assert got.evidence["acos"] == pytest.approx(0.80, abs=0.01)
    assert got.evidence["affordable_spend"] == pytest.approx(16.75, abs=0.5)
    assert "against a 34% target" in got.reason


def test_a_target_inside_its_target_is_left_alone(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=6, spend=20.0, sales=100.0)
    assert only(build.scene(), "over_target_acos") == []


def test_amits_own_target_is_what_a_target_is_judged_against(build):
    build.db.add(PpcRule(scope="asin", scope_ref=ASIN, target_acos=0.15, active=True))
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=6, spend=20.0, sales=100.0)
    [got] = only(build.scene(), "over_target_acos")
    assert got.evidence["target_acos"] == 0.15 and "owner" in got.evidence["target_acos_source"]


def test_the_strictest_product_in_an_ad_group_sets_the_target(build):
    roomy = build.listing(asin="B0ROOMY0001", sku="R-1", price=40.00, cost=5.00)
    tight = build.listing(asin="B0TIGHT0001", sku="T-1", price=12.00, cost=6.00)
    build.economics(asin="B0ROOMY0001", price=40.00, cost=5.00, referral=6.00, fba=4.00)
    build.economics(asin="B0TIGHT0001", price=12.00, cost=6.00, referral=1.80, fba=3.22)
    campaign = build.campaign()
    group = build.ad_group(campaign, asin="B0ROOMY0001", sku="R-1", listing=roomy)
    build.db.add(PpcProductAd(ad_product="sp", profile_id=build.profile.id, campaign_id=campaign.id,
                              ad_group_id=group.id, amazon_ad_id="A-2", asin="B0TIGHT0001",
                              sku="T-1", listing_id=tight.id))
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=4, spend=30.0, sales=60.0)
    scene = build.scene()
    value, source = scene.target_acos("G1")
    # tight: 12.00 − 6.00 − 1.80 − 3.22 = 0.98 margin → 8.2% break-even → 5.7% target.
    # roomy would allow 17.5%, so a bid set for it would lose money on the tight one.
    assert value == pytest.approx(0.0572, abs=0.001), "the tighter product decides"
    assert "strictest of several" in source


# ---------------- 3. under-exposed ----------------
def test_a_bid_increase_without_amazons_impression_share_is_less_certain(build):
    """The brief pairs efficiency with headroom; unproven headroom is said, not assumed away."""
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=8, spend=20.0, sales=200.0)
    [got] = only(build.scene(), "under_exposed")
    assert got.evidence["headroom_proven"] is False and got.confidence == 0.45
    assert "headroom is unproven" in got.reason


def test_a_target_beating_its_target_with_room_to_grow_is_pushed(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=8, spend=20.0, sales=200.0)
    build.metrics(entity_type="campaign", entity_id="C1", clicks=40, orders=8, spend=20.0,
                  sales=200.0, top_share=25.0)
    [got] = only(build.scene(), "under_exposed")
    assert got.change_type == "bid_increase" and got.proposed_value == 1.25
    assert got.evidence["top_of_search_share"] == 25.0
    assert got.risk == "medium" and got.spend_at_risk > 0


def test_a_target_already_taking_the_top_of_search_is_not_pushed(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=8, spend=20.0, sales=200.0)
    build.metrics(entity_type="campaign", entity_id="C1", clicks=40, orders=8, spend=20.0,
                  sales=200.0, top_share=85.0)
    assert only(build.scene(), "under_exposed") == [], "efficiency with nothing left to buy"


# ---------------- 4. harvesting ----------------
def test_a_search_term_that_sells_is_proposed_as_its_own_target(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, text="pouches", match="broad")
    build.term(text="nicotine pouches mint", clicks=20, orders=4, spend=10.0, sales=80.0)
    [got] = only(build.scene(), "harvest")
    assert got.change_type == "harvest_search_term" and got.label == "nicotine pouches mint"
    assert got.proposed_value["match_type"] == "exact"
    # revenue per click is 4.00; at a 33.5% target a click can cost 1.34
    assert got.proposed_value["bid"] == pytest.approx(1.34, abs=0.02)
    assert got.evidence["orders"] == 4 and got.evidence["revenue_per_click"] == 4.0


def test_a_term_already_targeted_is_not_proposed_again(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, text="nicotine pouches mint", match="exact")
    build.term(text="nicotine pouches mint", clicks=20, orders=4, spend=10.0, sales=80.0)
    assert only(build.scene(), "harvest") == []


def test_a_term_already_dealt_with_is_not_proposed_again(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, text="pouches", match="broad")
    build.term(text="nicotine pouches mint", clicks=20, orders=4, spend=10.0, sales=80.0,
               status="harvested")
    assert only(build.scene(), "harvest") == []


def test_a_term_that_sells_but_too_expensively_is_not_harvested(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, text="pouches", match="broad")
    build.term(text="expensive term", clicks=40, orders=2, spend=40.0, sales=40.0)
    assert only(build.scene(), "harvest") == [], "100% ACOS is not worth keeping"


# ---------------- 5. negatives ----------------
def test_a_search_term_spending_without_selling_becomes_a_negative(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    build.term(text="free nicotine samples", clicks=20, orders=0, spend=12.0, sales=0.0)
    [got] = only(build.scene(), "negatives")
    assert got.change_type == "add_negative_exact"
    assert got.proposed_value["expression"] == "free nicotine samples"
    assert got.evidence["spend"] == 12.0


def test_a_pattern_across_several_dead_searches_is_blocked_as_a_phrase(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    for text in ("wholesale mint pouches", "pouches wholesale bulk", "wholesale nicotine cartons"):
        build.term(text=text, clicks=12, orders=0, spend=8.0, sales=0.0)
    found = only(build.scene(), "negatives")
    phrases = [c for c in found if c.change_type == "add_negative_phrase"]
    assert len(phrases) == 1 and phrases[0].label == "wholesale"
    assert phrases[0].evidence["terms_matched"] == 3
    assert "blocks the pattern rather than chasing each search" in phrases[0].reason
    assert len([c for c in found if c.change_type == "add_negative_exact"]) == 3


def test_a_small_spend_is_not_worth_a_negative(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    build.term(text="one stray click", clicks=2, orders=0, spend=1.20, sales=0.0)
    assert only(build.scene(), "negatives") == []


# ---------------- 6. budget ----------------
def test_a_campaign_stopped_by_its_budget_while_working_is_raised(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign(budget=20.0)
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    for n in range(6):
        build.day_of_spend("C1", day=build.end - timedelta(days=n), spend=20.0, sales=100.0,
                           clicks=25, orders=5)
    [got] = only(build.scene(), "budget")
    assert got.change_type == "budget_increase" and got.current_value == 20.0
    assert got.proposed_value == 25.0
    assert got.evidence["days_at_budget"] == 6
    assert "being stopped while it is working" in got.reason


def test_a_campaign_that_never_spends_its_budget_and_performs_badly_is_trimmed(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign(budget=100.0)
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    for n in range(8):
        build.day_of_spend("C1", day=build.end - timedelta(days=n), spend=10.0, sales=8.0,
                           clicks=12, orders=1)
    [got] = only(build.scene(), "budget")
    assert got.change_type == "budget_decrease" and got.proposed_value < 100.0
    assert "never reaches its" in got.reason


def test_a_campaign_at_budget_but_losing_money_is_not_raised(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign(budget=20.0)
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    for n in range(6):
        build.day_of_spend("C1", day=build.end - timedelta(days=n), spend=20.0, sales=25.0,
                           clicks=25, orders=2)
    assert [c for c in only(build.scene(), "budget")
            if c.change_type == "budget_increase"] == []


# ---------------- 7. placement ----------------
def test_a_placement_much_worse_than_the_campaign_is_adjusted(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign(placements=[{"placement": "PLACEMENT_TOP", "percentage": 50}])
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    build.placement(placement="top_of_search", clicks=100, spend=100.0, sales=100.0, orders=5)
    build.placement(placement="product_pages", clicks=100, spend=50.0, sales=300.0, orders=10)
    found = only(build.scene(), "placement")
    top = [c for c in found if "top of search" in (c.label or "")]
    assert top, "top of search at 100% ACOS against 17% everywhere else should be adjusted"
    assert top[0].change_type == "placement_adjustment"
    assert top[0].current_value == 50.0 and top[0].proposed_value < 50.0
    # Compared against the rest of the campaign, not against an average that includes itself:
    # product pages alone are $50 on $300, so 16.7%.
    assert top[0].evidence["other_placements_acos"] == pytest.approx(0.1667, abs=0.01)
    assert "everywhere else in the campaign" in top[0].reason


def test_a_placement_with_too_few_clicks_is_not_judged(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    build.placement(placement="top_of_search", clicks=12, spend=40.0, sales=10.0)
    build.placement(placement="product_pages", clicks=8, spend=4.0, sales=40.0)
    assert only(build.scene(), "placement") == []


# ---------------- 8. structure ----------------
def test_the_same_keyword_in_two_campaigns_is_reported(build):
    listing = build.listing()
    build.economics()
    first = build.campaign(campaign_id="C1", name="Skyzz exact")
    second = build.campaign(campaign_id="C2", name="Skyzz broad")
    group_one = build.ad_group(first, ad_group_id="G1", listing=listing)
    group_two = build.ad_group(second, ad_group_id="G2", listing=listing)
    build.target(first, group_one, target_id="K1", text="nicotine pouches", bid=1.00)
    build.target(second, group_two, target_id="K2", text="nicotine pouches", bid=1.20)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=6, spend=20.0, sales=100.0,
                  campaign_id="C1", ad_group_id="G1")
    build.metrics(entity_type="target", entity_id="K2", clicks=40, orders=2, spend=40.0, sales=50.0,
                  campaign_id="C2", ad_group_id="G2")
    found = [c for c in only(build.scene(), "structure")
             if c.evidence.get("duplicates")]
    assert len(found) == 1 and found[0].entity_id == "K2", "the worse copy is the one to pause"
    assert "bidding against each other" in found[0].reason


def test_an_ad_group_selling_with_no_exact_coverage_is_reported(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign(targeting="auto")
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, target_id="T1", text="query_high_rel_matches", match="auto_close",
                 kind="target")
    build.metrics(entity_type="target", entity_id="T1", clicks=40, orders=5, spend=20.0, sales=100.0)
    found = [c for c in only(build.scene(), "structure") if c.entity_type == "ad_group"]
    assert found and "no exact-match target of its own" in found[0].reason
    assert found[0].evidence["orders"] == 5


def test_an_auto_campaign_nobody_harvests_is_reported(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign(targeting="auto")
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, target_id="T1", text="query_high_rel_matches", match="auto_close",
                 kind="target")
    build.metrics(entity_type="campaign", entity_id="C1", clicks=60, orders=6, spend=30.0, sales=200.0)
    build.term(text="mint pouches", clicks=20, orders=3, spend=10.0, sales=60.0, target_id="T1")
    found = [c for c in only(build.scene(), "structure") if c.entity_type == "campaign"]
    assert found and "Discovery is doing its job; nothing is being kept" in found[0].reason


# ---------------- the context filters (brief 7.5) ----------------
def _under_target(build, **listing_kw):
    """An account where the numbers plainly say "bid up" — so only the context can stop it."""
    listing = build.listing(**listing_kw)
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=8, spend=20.0, sales=200.0)
    build.metrics(entity_type="campaign", entity_id="C1", clicks=40, orders=8, spend=20.0,
                  sales=200.0, top_share=25.0)
    return build.scene()


def test_a_bid_is_never_raised_on_something_out_of_stock(build):
    scene = _under_target(build, stock=0)
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["under_exposed"],
                               scene=scene)
    assert result["candidates"] == [] and len(result["held"]) == 1
    held = result["held"][0]
    assert "no sellable stock" in held.why_not
    assert held.evidence["context"][ASIN]["fulfillable_qty"] == 0


def test_a_bid_is_never_raised_on_something_amazon_is_not_showing(build):
    scene = _under_target(build, buyable=False)
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["under_exposed"],
                               scene=scene)
    assert "isn't buyable right now" in result["held"][0].why_not


def test_a_bid_is_never_raised_on_something_nearly_out_of_stock(build):
    # 6 units a day and 30 left is five days of cover, under the fourteen-day floor.
    scene = _under_target(build, stock=30, sales_per_day=6)
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["under_exposed"],
                               scene=scene)
    assert "days of stock left" in result["held"][0].why_not


def test_a_bid_is_never_raised_while_the_buy_box_is_going_elsewhere(build):
    scene = _under_target(build, buy_box=45.0)
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["under_exposed"],
                               scene=scene)
    assert "Buy Box" in result["held"][0].why_not


def test_cutting_spend_on_an_out_of_stock_product_is_still_allowed(build):
    """The filter protects against spending more, not against spending less."""
    listing = build.listing(stock=0)
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=60, orders=0, spend=30.0, sales=0.0)
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["wasted_spend"],
                               scene=build.scene())
    assert len(result["candidates"]) == 1 and result["held"] == []


def test_a_healthy_product_is_not_held(build):
    scene = _under_target(build)
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["under_exposed"],
                               scene=scene)
    assert len(result["candidates"]) == 1 and result["held"] == []


def test_an_advertised_asin_we_have_no_catalogue_row_for_is_flagged_not_blocked(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, asin="B0UNKNOWN01", sku="UNK-1", listing=None)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=8, spend=20.0, sales=200.0)
    build.metrics(entity_type="campaign", entity_id="C1", clicks=40, orders=8, spend=20.0,
                  sales=200.0, top_share=25.0)
    scene = build.scene()
    # No economics for that ASIN, so under_exposed cannot judge it; the context note is the point.
    got = candidates.Candidate(analyser="t", change_type="bid_increase", entity_type="target",
                               entity_id="K1", reason="x", ad_group_id="G1")
    analysers._apply_context(scene, got)
    assert not got.held and any("isn't in the catalogue" in n for n in got.notes)


# ---------------- cooldown ----------------
def test_an_entity_changed_last_week_is_left_to_settle(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=60, orders=0, spend=30.0, sales=0.0)
    build.db.add(PpcAction(ad_product="sp", profile_id=build.profile.id, entity_type="target",
                           amazon_entity_id="K1", change_type="bid_decrease",
                           before_value={"bid": 1.2}, after_value={"bid": 1.0},
                           # Anchored on the profile's own day, not on UTC. `_cooldown_block`
                           # measures `scene.today - executed_at.date()`, and `scene.today` is the
                           # marketplace's date — so between midnight and 08:00 UTC, when Los
                           # Angeles is still on the previous day, a UTC-anchored timestamp is a
                           # day out and this test failed for eight hours a day.
                           executed_at=datetime.combine(
                               build.today - timedelta(days=4), time(12, 0), tzinfo=UTC)))
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["wasted_spend"],
                               scene=build.scene())
    assert result["candidates"] == [] and len(result["in_cooldown"]) == 1
    assert "10 days left" in result["in_cooldown"][0].holds[0]


def test_an_entity_changed_long_ago_is_fair_game_again(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=60, orders=0, spend=30.0, sales=0.0)
    build.db.add(PpcAction(ad_product="sp", profile_id=build.profile.id, entity_type="target",
                           amazon_entity_id="K1", change_type="bid_decrease",
                           before_value={"bid": 1.2}, after_value={"bid": 1.0},
                           executed_at=utcnow() - timedelta(days=40)))
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["wasted_spend"],
                               scene=build.scene())
    assert len(result["candidates"]) == 1


# ---------------- running them together ----------------
def test_every_analyser_runs_and_one_failing_does_not_silence_the_rest(build, monkeypatch):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=60, orders=0, spend=30.0, sales=0.0)

    def explode(scene):
        raise RuntimeError("this analyser is having a day")

    monkeypatch.setitem(analysers.ANALYSERS, "placement", explode)
    result = analysers.run_all(build.db, build.profile, today=build.today, scene=build.scene())
    assert result["per_analyser"]["placement"] == -1
    assert result["per_analyser"]["wasted_spend"] == 1
    assert len(result["candidates"]) >= 1


def test_findings_are_ordered_by_what_they_are_worth(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, target_id="K1", text="small waste", bid=1.00)
    build.target(campaign, group, target_id="K2", text="big waste", bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=60, orders=0, spend=15.0, sales=0.0)
    build.metrics(entity_type="target", entity_id="K2", clicks=90, orders=0, spend=120.0, sales=0.0)
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["wasted_spend"],
                               scene=build.scene())
    assert [c.entity_id for c in result["candidates"]] == ["K2", "K1"]


def test_nothing_is_proposed_from_provisional_data(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    # Sixty dry clicks, all of them on days Amazon is still validating.
    for n in range(3):
        build.db.add(PpcMetricsDaily(ad_product="sp", 
            amazon_profile_id=build.profile.amazon_profile_id, marketplace_id=MARKETPLACE,
            entity_type="target", amazon_entity_id="K1", date=build.today - timedelta(days=n),
            amazon_campaign_id="C1", amazon_ad_group_id="G1", clicks=20, spend=10.0, sales=0.0,
            orders=0, impressions=400, is_provisional=True))
    assert only(build.scene(), "wasted_spend") == []


def test_every_candidate_carries_its_numbers(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign(budget=20.0)
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, target_id="K1", text="over target", bid=1.00)
    build.target(campaign, group, target_id="K2", text="dead weight", bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=4, spend=40.0, sales=50.0)
    build.metrics(entity_type="target", entity_id="K2", clicks=60, orders=0, spend=30.0, sales=0.0)
    build.term(text="free samples please", clicks=20, orders=0, spend=12.0, sales=0.0)
    build.term(text="mint pouches strong", clicks=20, orders=4, spend=10.0, sales=80.0)
    result = analysers.run_all(build.db, build.profile, today=build.today, scene=build.scene())
    everything = result["candidates"] + result["held"] + result["in_cooldown"]
    assert everything
    for got in everything:
        assert got.evidence, f"{got.analyser} produced a finding with no evidence"
        assert got.reason and len(got.reason) > 20
        assert got.change_type and got.entity_id
        assert 0 <= got.confidence <= 1


# ---------------- the read-only view ----------------
def test_the_findings_view_groups_by_kind_and_shows_the_reasons(build):
    from app.ppc import views

    listing = build.listing()
    build.economics()
    campaign = build.campaign(budget=20.0)
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, target_id="K1", text="over target", bid=1.00)
    build.target(campaign, group, target_id="K2", text="dead weight", bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=4, spend=40.0, sales=50.0)
    build.metrics(entity_type="target", entity_id="K2", clicks=60, orders=0, spend=30.0, sales=0.0)
    build.term(text="free samples please", clicks=20, orders=0, spend=12.0, sales=0.0)
    build.db.commit()
    text = views.findings_summary(build.db)
    assert "What I'd change" in text
    assert "pause a target" in text and "block a search" in text
    assert "dead weight" in text and "clicks and no orders" in text
    assert "Nothing here is queued or sent" in text


def test_the_findings_view_says_plainly_when_there_is_nothing_to_do(build):
    from app.ppc import views

    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    # 30% ACOS against a 33.5% target: inside it, but not so far inside that there is room to push.
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=6, spend=30.0, sales=100.0)
    build.db.commit()
    text = views.findings_summary(build.db)
    assert "Nothing worth changing" in text and "a real answer, not an empty one" in text


def test_the_findings_view_reports_what_it_held_back(build):
    from app.ppc import views

    listing = build.listing(stock=0)
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=8, spend=20.0, sales=200.0)
    build.metrics(entity_type="campaign", entity_id="C1", clicks=40, orders=8, spend=20.0,
                  sales=200.0, top_share=25.0)
    build.db.commit()
    text = views.findings_summary(build.db)
    assert "held back" in text and "no sellable stock" in text


def test_the_findings_view_waits_until_the_account_has_been_read(db, profile):
    from app.ppc import views

    profile.last_synced_at = None
    db.commit()
    assert "haven't read the account yet" in views.findings_summary(db)


@pytest.mark.parametrize("text", ["PPC FINDINGS", "ppc findings", "ppc analysis", "ppc analyse",
                                  "ppc ideas"])
def test_the_ways_of_asking_for_findings(text):
    from app.services import commands

    assert commands.match(text)[0].name == "PPC_FINDINGS"


def test_another_marketplaces_changes_do_not_freeze_this_one(build, db):
    """The cooldown read every action ever executed, for every profile."""
    other = PpcProfile(amazon_profile_id="9999999999", marketplace_id="A2EUQ1WTGCTBG2",
                       country_code="CA", currency="CAD", timezone="America/Toronto",
                       status="active")
    db.add(other)
    db.flush()
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=60, orders=0, spend=30.0, sales=0.0)
    db.add(PpcAction(ad_product="sp", profile_id=other.id, entity_type="target", amazon_entity_id="K1",
                     change_type="bid_decrease", before_value={"bid": 1.2},
                     after_value={"bid": 1.0}, executed_at=utcnow() - timedelta(days=2)))
    result = analysers.run_all(db, build.profile, today=build.today, only=["wasted_spend"],
                               scene=build.scene())
    assert len(result["candidates"]) == 1 and result["in_cooldown"] == []


# ---------------- what the M5 review found ----------------
def test_a_budget_rise_is_held_when_the_product_is_out_of_stock(build):
    """A budget increase carries no ASIN of its own, so the filter has to find them itself."""
    listing = build.listing(stock=0)
    build.economics()
    campaign = build.campaign(budget=20.0)
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    for n in range(6):
        build.day_of_spend("C1", day=build.end - timedelta(days=n), spend=20.0, sales=100.0,
                           clicks=25, orders=5)
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["budget"],
                               scene=build.scene())
    assert result["candidates"] == []
    assert any("no sellable stock" in (c.why_not or "") for c in result["held"])


def test_a_placement_increase_is_a_spend_increase(build):
    got = candidates.Candidate(analyser="placement", change_type="placement_adjustment",
                               entity_type="campaign", entity_id="C1", reason="x",
                               current_value=0.0, proposed_value=25.0)
    assert analysers._spends_more(got) is True
    down = candidates.Candidate(analyser="placement", change_type="placement_adjustment",
                                entity_type="campaign", entity_id="C1", reason="x",
                                current_value=50.0, proposed_value=25.0)
    assert analysers._spends_more(down) is False, "turning a multiplier down spends less"


def test_every_product_in_an_ad_group_is_checked_not_just_the_first(build):
    healthy = build.listing(asin="B0HEALTHY01", sku="H-1")
    build.listing(asin="B0NOSTOCK01", sku="N-1", stock=0)
    build.economics(asin="B0HEALTHY01")
    build.economics(asin="B0NOSTOCK01")
    campaign = build.campaign()
    group = build.ad_group(campaign, asin="B0HEALTHY01", sku="H-1", listing=healthy)
    build.db.add(PpcProductAd(ad_product="sp", profile_id=build.profile.id, campaign_id=campaign.id,
                              ad_group_id=group.id, amazon_ad_id="A-2", asin="B0NOSTOCK01",
                              sku="N-1"))
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=8, spend=20.0, sales=200.0)
    build.metrics(entity_type="campaign", entity_id="C1", clicks=40, orders=8, spend=20.0,
                  sales=200.0, top_share=25.0)
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["under_exposed"],
                               scene=build.scene())
    assert result["candidates"] == [], "one product out of stock is enough to hold the increase"
    assert "B0NOSTOCK01" in (result["held"][0].why_not or "")


def test_a_negative_phrase_never_blocks_the_ad_groups_own_keyword(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, text="pouches", match="broad")
    for text in ("pouches wholesale", "pouches bulk order", "pouches distributor"):
        build.term(text=text, clicks=12, orders=0, spend=8.0, sales=0.0)
    found = only(build.scene(), "negatives")
    phrases = [c.label for c in found if c.change_type == "add_negative_phrase"]
    assert "pouches" not in phrases, "that would switch off the ad group's own keyword"


def test_an_existing_negative_is_not_proposed_again(build):
    from app.db.models import PpcNegative

    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    build.db.add(PpcNegative(ad_product="sp", profile_id=build.profile.id, campaign_id=campaign.id,
                             ad_group_id=group.id, amazon_negative_id="N1", target_kind="keyword",
                             expression="free samples", match_type="exact", state="enabled"))
    build.term(text="free samples", clicks=20, orders=0, spend=12.0, sales=0.0)
    assert only(build.scene(), "negatives") == [], "it is already blocked here"


def test_a_negative_in_one_campaign_does_not_silence_another(build):
    from app.db.models import PpcNegative

    listing = build.listing()
    build.economics()
    first = build.campaign(campaign_id="C1", name="one")
    second = build.campaign(campaign_id="C2", name="two")
    g1 = build.ad_group(first, ad_group_id="G1", listing=listing)
    g2 = build.ad_group(second, ad_group_id="G2", listing=listing)
    build.target(first, g1, target_id="K1")
    build.target(second, g2, target_id="K2")
    build.db.add(PpcNegative(ad_product="sp", profile_id=build.profile.id, campaign_id=first.id, ad_group_id=g1.id,
                             amazon_negative_id="N1", target_kind="keyword",
                             expression="free samples", match_type="exact", state="enabled"))
    build.term(text="free samples", clicks=20, orders=0, spend=12.0, sales=0.0, ad_group_id="G1")
    build.term(text="free samples", clicks=20, orders=0, spend=12.0, sales=0.0, ad_group_id="G2",
               campaign_id="C2", target_id="K2")
    found = [c for c in only(build.scene(), "negatives") if c.change_type == "add_negative_exact"]
    assert [c.ad_group_id for c in found] == ["G2"], "still wasting money in the other campaign"


def test_a_campaigns_target_acos_comes_from_its_own_products(build):
    """It used to be able to borrow an unrelated campaign's, then be cut for missing it."""
    priced = build.listing(asin="B0PRICED001", sku="P-1")
    build.economics(asin="B0PRICED001")
    first = build.campaign(campaign_id="C1", name="priced")
    g1 = build.ad_group(first, ad_group_id="G1", asin="B0PRICED001", sku="P-1", listing=priced)
    build.target(first, g1, target_id="K1")
    unpriced = build.listing(asin="B0UNPRICED1", sku="U-1", cost=None)
    second = build.campaign(campaign_id="C2", name="unpriced")
    g2 = build.ad_group(second, ad_group_id="G2", asin="B0UNPRICED1", sku="U-1", listing=unpriced)
    build.target(second, g2, target_id="K2")
    scene = build.scene()
    assert analysers._campaign_target_acos(scene, "C1")[0] is not None
    assert analysers._campaign_target_acos(scene, "C2")[0] is None, \
        "C2's products can't be priced, so C2 has no target to be judged against"


def test_a_target_inheriting_the_ad_group_bid_can_still_be_changed(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=None)        # inherits the ad group default of 0.75
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=4, spend=40.0, sales=50.0)
    [got] = only(build.scene(), "over_target_acos")
    assert got.current_value == 0.75 and got.proposed_value < 0.75
    assert got.evidence["bid_inherited"] is True


def test_stock_we_cannot_vouch_for_is_not_treated_as_stock(build):
    from app.db.models import AmazonInventorySnapshot

    listing = build.listing(stock=None)
    build.db.add(AmazonInventorySnapshot(marketplace_id=MARKETPLACE, sku=listing.sku,
                                         asin=listing.asin, fulfillable_qty=500,
                                         snapshot_at=utcnow() - timedelta(days=4)))
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=8, spend=20.0, sales=200.0)
    build.metrics(entity_type="campaign", entity_id="C1", clicks=40, orders=8, spend=20.0,
                  sales=200.0, top_share=25.0)
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["under_exposed"],
                               scene=build.scene())
    assert result["candidates"] == []
    assert "hours old" in (result["held"][0].why_not or "")


def test_a_campaign_spending_with_no_sales_at_all_is_a_finding(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign(budget=50.0)
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    for n in range(8):
        build.day_of_spend("C1", day=build.end - timedelta(days=n), spend=12.0, sales=0.0,
                           clicks=15, orders=0)
    [got] = [c for c in only(build.scene(), "budget") if c.change_type == "budget_decrease"]
    assert "sold nothing at all" in got.reason
    assert got.proposed_value >= analysers.MIN_DAILY_BUDGET


def test_a_budget_is_never_proposed_below_amazons_minimum(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign(budget=20.0)
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    for n in range(8):
        build.day_of_spend("C1", day=build.end - timedelta(days=n), spend=0.30, sales=0.0,
                           clicks=15, orders=0)
    for got in only(build.scene(), "budget"):
        assert got.proposed_value >= 1.00, "Amazon would refuse anything less"


def test_a_term_found_in_an_auto_campaign_is_harvested_somewhere_it_can_live(build):
    listing = build.listing()
    build.economics()
    auto = build.campaign(campaign_id="C1", name="discovery", targeting="auto")
    manual = build.campaign(campaign_id="C2", name="exact", targeting="manual")
    auto_group = build.ad_group(auto, ad_group_id="G1", listing=listing)
    manual_group = build.ad_group(manual, ad_group_id="G2", listing=listing)
    build.target(auto, auto_group, target_id="T1", text="query_high_rel_matches",
                 match="auto_close", kind="target")
    build.target(manual, manual_group, target_id="K2", text="pouches", match="exact")
    build.term(text="mint nicotine pouches", clicks=20, orders=4, spend=10.0, sales=80.0,
               ad_group_id="G1", target_id="T1")
    [got] = only(build.scene(), "harvest")
    assert got.ad_group_id == "G2", "an automatic campaign cannot hold a keyword"
    assert "automatic campaigns can't hold keywords" in " ".join(got.notes)


def test_a_term_already_covered_by_exact_is_blocked_in_the_campaign_that_found_it(build):
    """7.1 asks for this by name: otherwise discovery keeps buying what exact already owns."""
    listing = build.listing()
    build.economics()
    auto = build.campaign(campaign_id="C1", name="discovery", targeting="auto")
    manual = build.campaign(campaign_id="C2", name="exact", targeting="manual")
    auto_group = build.ad_group(auto, ad_group_id="G1", listing=listing)
    manual_group = build.ad_group(manual, ad_group_id="G2", listing=listing)
    build.target(auto, auto_group, target_id="T1", text="query_high_rel_matches",
                 match="auto_close", kind="target")
    build.target(manual, manual_group, target_id="K2", text="mint nicotine pouches", match="exact")
    build.term(text="mint nicotine pouches", clicks=20, orders=3, spend=10.0, sales=60.0,
               ad_group_id="G1", target_id="T1")
    found = [c for c in only(build.scene(), "structure")
             if c.change_type == "add_negative_exact"]
    assert found and found[0].ad_group_id == "G1"
    assert "still buying it" in found[0].reason


def test_one_placement_gap_is_not_booked_from_both_ends(build):
    """Measured against an average including itself, a two-placement campaign found it twice."""
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    build.placement(placement="top_of_search", clicks=100, spend=100.0, sales=200.0, orders=5)
    build.placement(placement="product_pages", clicks=100, spend=100.0, sales=800.0, orders=10)
    found = only(build.scene(), "placement")
    assert len({c.evidence["placement"] for c in found}) == len(found), "no placement twice"
    worth = sum(c.monthly_effect for c in found)
    assert worth > 0
    # Both placements can legitimately be findings — one worse, one better than the other — but the
    # money must be counted once per placement, not once per pairing.
    assert len(found) <= 2


def test_a_product_no_cpc_can_make_work_is_paused_not_nudged(build):
    """Needed CVR above 100% means no conversion rate works at this cost per click."""
    listing = build.listing(price=9.00, cost=4.00)
    build.economics(price=9.00, cost=4.00, referral=1.35, fba=3.22)
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=2.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=40, orders=0, spend=80.0, sales=0.0)
    [got] = only(build.scene(), "wasted_spend")
    assert got.change_type == "pause_target"
    assert "no conversion rate that makes this work" in got.reason


def test_a_product_we_cannot_price_is_still_paused_on_the_click_count(build):
    """A missing unit cost is the normal state until the owner answers; it is not a reason to keep paying."""
    listing = build.listing(cost=None)
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, bid=1.00)
    build.metrics(entity_type="target", entity_id="K1", clicks=90, orders=0, spend=90.0, sales=0.0)
    [got] = only(build.scene(), "wasted_spend")
    assert got.change_type == "pause_target"
    assert "going on the click count alone" in got.reason


def test_a_pattern_and_its_own_searches_do_not_claim_the_same_money_twice(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, text="pouches", match="broad")
    for text in ("wholesale mint tins", "wholesale bulk cartons", "wholesale nicotine boxes"):
        build.term(text=text, clicks=12, orders=0, spend=8.0, sales=0.0)
    found = only(build.scene(), "negatives")
    phrase = [c for c in found if c.change_type == "add_negative_phrase"]
    exacts = [c for c in found if c.change_type == "add_negative_exact"]
    assert phrase and len(exacts) == 3
    assert phrase[0].monthly_effect > 0
    assert all(c.monthly_effect == 0 for c in exacts), "the pattern already claims this money"
    assert all("counted under the pattern" in " ".join(c.notes) for c in exacts)


def test_a_budget_cut_stays_above_the_busiest_day(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign(budget=100.0)
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group)
    spends = [5.0, 8.0, 20.0, 12.0, 7.0, 9.0, 11.0, 6.0]
    for n, spend in enumerate(spends):
        build.day_of_spend("C1", day=build.end - timedelta(days=n), spend=spend, sales=spend * 0.6,
                           clicks=15, orders=1)
    [got] = [c for c in only(build.scene(), "budget") if c.change_type == "budget_decrease"]
    assert got.proposed_value >= max(spends), "a budget under the busiest day changes how it runs"
    assert "above its busiest day" in got.reason


def test_a_messy_account_does_not_carry_thousands_of_findings_silently(build):
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    build.target(campaign, group, text="pouches", match="broad")
    for n in range(140):
        build.term(text=f"dead search number {n}", clicks=12, orders=0, spend=9.0, sales=0.0)
    build.db.commit()
    result = analysers.run_all(build.db, build.profile, today=build.today, only=["negatives"],
                               scene=build.scene())
    assert result["dropped_beyond_cap"] > 0, "what was dropped is counted, not silently truncated"
    assert len(result["candidates"]) == max(
        analysers.candidates.Thresholds.load(build.db).daily_cap * 4, 100)


def test_the_analysis_does_not_re_ask_the_same_questions(build):
    """Economics, campaign days and impression share are each read once per run, not per target."""
    listing = build.listing()
    build.economics()
    campaign = build.campaign()
    group = build.ad_group(campaign, listing=listing)
    for n in range(12):
        build.target(campaign, group, target_id=f"K{n}", text=f"term {n}", bid=1.00)
        build.metrics(entity_type="target", entity_id=f"K{n}", clicks=40, orders=8, spend=20.0,
                      sales=200.0)
    build.metrics(entity_type="campaign", entity_id="C1", clicks=480, orders=96, spend=240.0,
                  sales=2400.0, top_share=25.0)
    build.db.commit()

    counted = {"n": 0}
    from sqlalchemy import event

    engine = build.db.get_bind()

    def count(*_args, **_kw):
        counted["n"] += 1

    event.listen(engine, "before_cursor_execute", count)
    try:
        scene = analysers.load(build.db, build.profile, today=build.today)
        before = counted["n"]
        result = analysers.run_all(build.db, build.profile, today=build.today, scene=scene)
        during = counted["n"] - before
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert len(result["candidates"]) >= 12
    # Twelve targets in one campaign: the per-target work must not each cost a round trip.
    assert during < 12, f"running the analysers issued {during} queries for 12 targets"
