"""P4 M3: profiles and the structure sync.

The expensive mistakes here are silent ones — a wrong media type, a state Amazon uses that we read
as "enabled", a half-finished sync that archives the account — so those have tests.
"""

import pytest
from sqlalchemy import select

from app.db.models import (AmazonAccount, AmazonListing, PpcAdGroup, PpcCampaign, PpcNegative,
                           PpcPortfolio, PpcProductAd, PpcProfile, PpcTarget)
from app.ppc import profiles, structure
from app.ppc.errors import AdsError
from app.ppc.fake import FakeAdsClient
from app.ppc.profiles import PpcNotConnected

PROFILE = "1111111111"


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


# ---------------- profiles ----------------
def test_the_profile_is_built_from_env_on_first_use(db, monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "ads_profile_id_us", "9090909090", raising=False)
    monkeypatch.setattr(s, "ads_account_id", "ACC-1", raising=False)
    got = profiles.require(db)
    assert got.amazon_profile_id == "9090909090"
    assert got.marketplace_id == "ATVPDKIKX0DER" and got.currency == "USD"
    assert got.timezone == "America/Los_Angeles", "Ads reports in the marketplace's own time zone"
    assert got.region == "na" and got.status == "active"


def test_without_configuration_it_says_what_to_set(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ads_profile_id_us", "", raising=False)
    with pytest.raises(PpcNotConnected, match="ADS_PROFILE_ID_US"):
        profiles.require(db)


def test_a_paused_profile_is_refused_with_its_reason(db, profile):
    profiles.set_status(db, profile, "auth_error", "consent withdrawn")
    db.commit()
    with pytest.raises(PpcNotConnected, match="auth error: consent withdrawn"):
        profiles.require(db)


def test_writes_are_off_until_the_setting_says_live(db, profile, owner):
    from app.services import settings_store
    from app.services.common import Actor

    assert profiles.writes_allowed(db, profile) is False
    settings_store.set_value(db, "ppc_write_mode", "live", Actor.user(owner.id))
    profile.write_mode = "live"
    db.commit()
    assert profiles.writes_allowed(db, profile) is True


def test_a_stored_write_mode_beats_a_stale_env_file(db, profile, owner, monkeypatch):
    from app.config import get_settings
    from app.services import settings_store
    from app.services.common import Actor

    monkeypatch.setattr(get_settings(), "ppc_write_mode", "live", raising=False)
    settings_store.set_value(db, "ppc_write_mode", "read_only", Actor.user(owner.id))
    profile.write_mode = "live"
    db.commit()
    assert profiles.writes_allowed(db, profile) is False, "the owner's setting decides, not .env"


def test_discovery_stores_amazons_description_but_never_switches_a_profile_on(db):
    FakeAdsClient.expect(r"/v2/profiles", [
        {"profileId": 555, "countryCode": "CA", "currencyCode": "CAD",
         "timezone": "America/Toronto",
         "accountInfo": {"marketplaceStringId": "A2EUQ1WTGCTBG2", "id": "SELLER-1",
                         "type": "seller", "name": "Kings Distro CA"}}])
    stats = profiles.sync(db, client())
    row = profiles.get(db, "555")
    assert stats["profiles"] == 1 and stats["new"] == ["555"]
    assert row.country_code == "CA" and row.currency == "CAD" and row.timezone == "America/Toronto"
    assert row.marketplace_id == "A2EUQ1WTGCTBG2" and row.seller_id == "SELLER-1"
    assert row.status == "paused", "a newly discovered marketplace must not start advertising itself"
    assert row.write_mode == "read_only"


def test_discovery_links_the_profile_to_the_phase_3_account(db):
    db.add(AmazonAccount(seller_id="A1SELLER", region="na"))
    db.commit()
    FakeAdsClient.expect(r"/v2/profiles", [{"profileId": 7, "countryCode": "US",
                                            "accountInfo": {"marketplaceStringId": "ATVPDKIKX0DER"}}])
    profiles.sync(db, client())
    assert profiles.get(db, "7").amazon_account_id is not None


def test_a_profile_amazon_stops_listing_is_flagged_not_forgotten(db, profile):
    # Amazon lists profiles, just not ours — this application's access to it has gone.
    FakeAdsClient.expect(r"/v2/profiles", [
        {"profileId": 999, "countryCode": "CA", "accountInfo": {"marketplaceStringId": "A2EUQ1WTGCTBG2"}}])
    profiles.sync(db, client())
    db.refresh(profile)
    assert profile.status == "auth_error" and "no longer lists" in profile.status_reason


def test_an_empty_profile_list_does_not_switch_everything_off(db, profile):
    """An empty response is far more likely our request than the loss of every marketplace."""
    FakeAdsClient.expect(r"/v2/profiles", [])
    stats = profiles.sync(db, client())
    db.refresh(profile)
    assert stats["profiles"] == 0
    assert profile.status == "active", "one odd response must not stop all collection"


def test_access_coming_back_restarts_collection_without_anyone_asking(db, profile):
    profiles.set_status(db, profile, "auth_error", "consent was withdrawn")
    db.commit()
    FakeAdsClient.expect(r"/v2/profiles", [
        {"profileId": int(PROFILE), "countryCode": "US",
         "accountInfo": {"marketplaceStringId": "ATVPDKIKX0DER"}}])
    stats = profiles.sync(db, client())
    db.refresh(profile)
    assert stats["recovered"] == [PROFILE]
    assert profile.status == "active" and profile.status_reason is None


def test_recovery_never_switches_on_a_marketplace_the_owner_paused(db):
    paused = PpcProfile(amazon_profile_id="7777777777", marketplace_id="A2EUQ1WTGCTBG2",
                        country_code="CA", currency="CAD", timezone="America/Toronto",
                        status="paused", status_reason="waiting to be enabled")
    db.add(paused)
    db.commit()
    FakeAdsClient.expect(r"/v2/profiles", [
        {"profileId": 7777777777, "countryCode": "CA",
         "accountInfo": {"marketplaceStringId": "A2EUQ1WTGCTBG2"}}])
    profiles.sync(db, client())
    db.refresh(paused)
    assert paused.status == "paused", "only an access failure heals itself; a decision does not"


# ---------------- the endpoints themselves ----------------
def _queue_structure(*, campaigns=(), ad_groups=(), product_ads=(), keywords=(), targets=(),
                     portfolios=(), negatives=()):
    FakeAdsClient.expect(r"/portfolios/list", {"portfolios": list(portfolios)})
    FakeAdsClient.expect(r"/sp/campaigns/list", {"campaigns": list(campaigns)})
    FakeAdsClient.expect(r"/sp/adGroups/list", {"adGroups": list(ad_groups)})
    FakeAdsClient.expect(r"/sp/productAds/list", {"productAds": list(product_ads)})
    FakeAdsClient.expect(r"/sp/keywords/list", {"keywords": list(keywords)})
    FakeAdsClient.expect(r"/sp/targets/list", {"targetingClauses": list(targets)})
    FakeAdsClient.expect(r"/sp/negativeKeywords/list", {"negativeKeywords": list(negatives)})
    FakeAdsClient.expect(r"/sp/campaignNegativeKeywords/list", {"campaignNegativeKeywords": []})
    FakeAdsClient.expect(r"/sp/negativeTargets/list", {"negativeTargetingClauses": []})
    FakeAdsClient.expect(r"/sp/campaignNegativeTargets/list", {"campaignNegativeTargetingClauses": []})


CAMPAIGN = {"campaignId": "C1", "name": "Skyzz exact", "state": "ENABLED", "targetingType": "MANUAL",
            "budget": {"budget": 40.0, "budgetType": "DAILY"},
            "dynamicBidding": {"strategy": "AUTO_FOR_SALES",
                               "placementBidding": [{"placement": "PLACEMENT_TOP", "percentage": 50}]},
            "startDate": "2026-01-05",
            "extendedData": {"creationDateTime": "2026-01-05T10:00:00Z", "servingStatus": "DELIVERING"}}
AD_GROUP = {"adGroupId": "G1", "campaignId": "C1", "name": "core", "state": "ENABLED",
            "defaultBid": 0.85, "extendedData": {"servingStatus": "AD_GROUP_STATUS_ENABLED"}}


def test_every_list_call_uses_amazons_exact_media_type(db, profile):
    _queue_structure(campaigns=[CAMPAIGN], ad_groups=[AD_GROUP])
    structure.sync_all(db, profile, client())
    used = {c["path"]: c["content_type"] for c in FakeAdsClient.calls}
    # camelCase, exactly as Amazon's OpenAPI spec spells them.
    assert used["/sp/campaigns/list"] == "application/vnd.spCampaign.v3+json"
    assert used["/sp/adGroups/list"] == "application/vnd.spAdGroup.v3+json"
    assert used["/sp/targets/list"] == "application/vnd.spTargetingClause.v3+json"
    assert used["/portfolios/list"] == "application/vnd.spPortfolio.v3+json"
    assert used["/sp/campaignNegativeTargets/list"] == \
        "application/vnd.spCampaignNegativeTargetingClause.v3+json"
    for path, media in used.items():
        if path.endswith("/list"):
            assert used[path] == media and media.endswith(".v3+json")


def test_the_request_asks_for_extended_data_and_leaves_page_size_to_amazon(db, profile):
    _queue_structure(campaigns=[CAMPAIGN])
    structure.sync_all(db, profile, client())
    body = FakeAdsClient.last_body("/sp/campaigns/list")
    assert body["includeExtendedDataFields"] is True, "creation dates and serving status need it"
    assert "maxResults" not in body, "Amazon's default page size is already its maximum"
    assert body["stateFilter"] == {"include": ["ENABLED", "PAUSED", "ARCHIVED"]}


def test_pagination_follows_next_token_to_the_end(db, profile):
    FakeAdsClient.expect(r"/portfolios/list", {"portfolios": []})
    FakeAdsClient.expect(r"/sp/campaigns/list", {"campaigns": [CAMPAIGN], "nextToken": "PAGE2"})
    FakeAdsClient.expect(r"/sp/campaigns/list", {"campaigns": [dict(CAMPAIGN, campaignId="C2",
                                                                   name="Vibezy auto")]})
    for path, key in ((r"/sp/adGroups/list", "adGroups"), (r"/sp/productAds/list", "productAds"),
                      (r"/sp/keywords/list", "keywords"), (r"/sp/targets/list", "targetingClauses"),
                      (r"/sp/negativeKeywords/list", "negativeKeywords"),
                      (r"/sp/campaignNegativeKeywords/list", "campaignNegativeKeywords"),
                      (r"/sp/negativeTargets/list", "negativeTargetingClauses"),
                      (r"/sp/campaignNegativeTargets/list", "campaignNegativeTargetingClauses")):
        FakeAdsClient.expect(path, {key: []})
    stats = structure.sync_all(db, profile, client())
    assert stats["campaigns"] == 2
    second = FakeAdsClient.calls_to("/sp/campaigns/list")[1]
    assert second["json"]["nextToken"] == "PAGE2"


# ---------------- what gets stored ----------------
def test_a_campaign_is_stored_as_amazon_describes_it(db, profile):
    _queue_structure(campaigns=[CAMPAIGN], ad_groups=[AD_GROUP])
    structure.sync_all(db, profile, client())
    row = db.execute(select(PpcCampaign)).scalar_one()
    assert row.name == "Skyzz exact" and row.state == "enabled" and row.targeting_type == "manual"
    assert float(row.daily_budget) == 40.0 and row.budget_type == "daily"
    assert row.bidding_strategy == "AUTO_FOR_SALES"
    assert row.placement_adjustments == [{"placement": "PLACEMENT_TOP", "percentage": 50}], \
        "kept verbatim so a placement change can be rolled back unchanged"
    assert row.created_at_amazon is not None, "creationDateTime lives inside extendedData"
    assert row.serving_status == "DELIVERING"
    group = db.execute(select(PpcAdGroup)).scalar_one()
    assert group.campaign_id == row.id and float(group.default_bid) == 0.85


def test_a_state_amazon_uses_that_we_do_not_know_is_not_read_as_enabled(db, profile):
    _queue_structure(campaigns=[dict(CAMPAIGN, state="USER_DELETED")])
    structure.sync_all(db, profile, client())
    assert db.execute(select(PpcCampaign)).scalar_one().state == "paused", \
        "a draft state read as 'enabled' would put dead campaigns in front of the analysers"


def test_an_archived_campaign_is_kept_with_the_date_it_went(db, profile):
    _queue_structure(campaigns=[dict(CAMPAIGN, state="ARCHIVED")])
    structure.sync_all(db, profile, client())
    row = db.execute(select(PpcCampaign)).scalar_one()
    assert row.state == "archived" and row.archived_at is not None


def test_something_amazon_stops_listing_is_archived_after_missing_twice(db, profile):
    """One miss can be a state Amazon's filter cannot name, so a live campaign is not archived on it."""
    _queue_structure(campaigns=[CAMPAIGN, dict(CAMPAIGN, campaignId="C2", name="old")],
                     ad_groups=[AD_GROUP])
    structure.sync_all(db, profile, client())
    db.commit()

    FakeAdsClient.reset()
    _queue_structure(campaigns=[CAMPAIGN], ad_groups=[AD_GROUP])
    first_miss = structure.sync_all(db, profile, client())
    db.commit()
    gone = db.execute(select(PpcCampaign).where(PpcCampaign.amazon_campaign_id == "C2")).scalar_one()
    assert first_miss["archived"] == 0 and gone.state == "enabled", "one absence is not proof"

    FakeAdsClient.reset()
    _queue_structure(campaigns=[CAMPAIGN], ad_groups=[AD_GROUP])
    second_miss = structure.sync_all(db, profile, client())
    db.expire_all()
    gone = db.execute(select(PpcCampaign).where(PpcCampaign.amazon_campaign_id == "C2")).scalar_one()
    assert second_miss["archived"] == 1 and gone.state == "archived" and gone.archived_at is not None
    assert len(db.execute(select(PpcCampaign)).scalars().all()) == 2, \
        "the row stays; the metrics history points at it"


def test_a_campaign_that_reappears_is_not_left_looking_archived(db, profile):
    for _ in range(3):
        FakeAdsClient.reset()
        _queue_structure(campaigns=[CAMPAIGN, dict(CAMPAIGN, campaignId="C2")], ad_groups=[AD_GROUP])
        structure.sync_all(db, profile, client())
        db.commit()
    rows = {c.amazon_campaign_id: c for c in db.execute(select(PpcCampaign)).scalars()}
    assert all(c.archived_at is None and c.state == "enabled" for c in rows.values())


def test_the_date_an_entity_was_archived_does_not_drift(db, profile):
    _queue_structure(campaigns=[dict(CAMPAIGN, state="ARCHIVED")])
    structure.sync_all(db, profile, client())
    db.commit()
    first = db.execute(select(PpcCampaign)).scalar_one().archived_at
    FakeAdsClient.reset()
    _queue_structure(campaigns=[dict(CAMPAIGN, state="ARCHIVED")])
    structure.sync_all(db, profile, client())
    assert db.execute(select(PpcCampaign)).scalar_one().archived_at == first, \
        "otherwise it records the last sync rather than when the campaign went"


def test_a_sync_that_fails_part_way_through_does_not_archive_the_account(db, profile):
    _queue_structure(campaigns=[CAMPAIGN], ad_groups=[AD_GROUP])
    structure.sync_all(db, profile, client())
    db.commit()
    FakeAdsClient.reset()
    FakeAdsClient.expect(r"/portfolios/list", {"portfolios": []})
    FakeAdsClient.expect(r"/sp/campaigns/list", {"campaigns": [CAMPAIGN]})
    FakeAdsClient.expect(r"/sp/adGroups/list", AdsError("Amazon is having a moment"))
    with pytest.raises(AdsError):
        structure.sync_all(db, profile, client())
    db.rollback()
    assert db.execute(select(PpcAdGroup)).scalar_one().archived_at is None, \
        "a half-finished read must never look like 'Amazon deleted everything'"


def test_an_ad_group_whose_campaign_has_not_arrived_yet_is_left_for_next_time(db, profile):
    _queue_structure(campaigns=[CAMPAIGN],
                     ad_groups=[AD_GROUP, dict(AD_GROUP, adGroupId="G9", campaignId="C-NEW")])
    stats = structure.sync_all(db, profile, client())
    assert stats["ad_groups"] == 1 and stats["ad_groups_without_a_campaign"] == 1
    assert db.execute(select(PpcAdGroup)).scalar_one().amazon_ad_group_id == "G1"


def test_a_product_ad_is_linked_to_the_phase_3_listing(db, profile):
    account = AmazonAccount(seller_id="A1SELLER", region="na")
    db.add(account)
    db.flush()
    listing = AmazonListing(account_id=account.id, marketplace_id="ATVPDKIKX0DER",
                            sku="H3-NKLT-J0KI", asin="B0GMW34NZC")
    db.add(listing)
    db.commit()
    _queue_structure(campaigns=[CAMPAIGN], ad_groups=[AD_GROUP],
                     product_ads=[{"adId": "A1", "campaignId": "C1", "adGroupId": "G1",
                                   "asin": "B0GMW34NZC", "sku": "H3-NKLT-J0KI", "state": "ENABLED"}])
    structure.sync_all(db, profile, client())
    ad = db.execute(select(PpcProductAd)).scalar_one()
    assert ad.listing_id == listing.id, "this link is how stock and suppression reach the analysers"


def test_keywords_and_targets_share_a_table_without_colliding(db, profile):
    _queue_structure(
        campaigns=[CAMPAIGN], ad_groups=[AD_GROUP],
        keywords=[{"keywordId": "X1", "campaignId": "C1", "adGroupId": "G1",
                   "keywordText": "nicotine pouches", "matchType": "EXACT", "state": "ENABLED",
                   "bid": 1.10}],
        # Amazon's keyword and target ids are separate namespaces, so the same id can be both.
        targets=[{"targetId": "X1", "campaignId": "C1", "adGroupId": "G1", "state": "ENABLED",
                  "bid": 0.75, "expressionType": "MANUAL",
                  "expression": [{"type": "ASIN_SAME_AS", "value": "B0COMPETITOR"}]}])
    stats = structure.sync_all(db, profile, client())
    assert stats["keywords"] == 1 and stats["targets"] == 1
    rows = {r.target_kind: r for r in db.execute(select(PpcTarget)).scalars()}
    assert set(rows) == {"keyword", "target"}
    assert rows["keyword"].expression == "nicotine pouches" and rows["keyword"].match_type == "exact"
    assert rows["keyword"].target_type == "keyword" and float(rows["keyword"].bid) == 1.10
    assert rows["target"].target_type == "product" and rows["target"].match_type == "asin"
    assert "b0competitor" in rows["target"].expression.lower()


def test_a_target_with_no_bid_of_its_own_is_marked_as_inheriting_one(db, profile):
    _queue_structure(campaigns=[CAMPAIGN], ad_groups=[AD_GROUP],
                     keywords=[{"keywordId": "X2", "campaignId": "C1", "adGroupId": "G1",
                                "keywordText": "vape", "matchType": "BROAD", "state": "ENABLED"}])
    structure.sync_all(db, profile, client())
    row = db.execute(select(PpcTarget)).scalar_one()
    assert row.bid_inherited is True, "the ad group default is in force, which every bid analyser needs"


def test_negatives_are_stored_at_the_level_amazon_put_them(db, profile):
    FakeAdsClient.expect(r"/portfolios/list", {"portfolios": []})
    FakeAdsClient.expect(r"/sp/campaigns/list", {"campaigns": [CAMPAIGN]})
    FakeAdsClient.expect(r"/sp/adGroups/list", {"adGroups": [AD_GROUP]})
    FakeAdsClient.expect(r"/sp/productAds/list", {"productAds": []})
    FakeAdsClient.expect(r"/sp/keywords/list", {"keywords": []})
    FakeAdsClient.expect(r"/sp/targets/list", {"targetingClauses": []})
    FakeAdsClient.expect(r"/sp/negativeKeywords/list", {"negativeKeywords": [
        {"keywordId": "N1", "campaignId": "C1", "adGroupId": "G1", "keywordText": "free samples",
         "matchType": "NEGATIVE_EXACT", "state": "ENABLED"}]})
    FakeAdsClient.expect(r"/sp/campaignNegativeKeywords/list", {"campaignNegativeKeywords": [
        {"keywordId": "N2", "campaignId": "C1", "keywordText": "cheap", "matchType": "NEGATIVE_PHRASE",
         "state": "ENABLED"}]})
    FakeAdsClient.expect(r"/sp/negativeTargets/list", {"negativeTargetingClauses": []})
    FakeAdsClient.expect(r"/sp/campaignNegativeTargets/list", {"campaignNegativeTargetingClauses": []})
    stats = structure.sync_all(db, profile, client())
    assert stats["negatives"] == 2
    rows = {r.amazon_negative_id: r for r in db.execute(select(PpcNegative)).scalars()}
    assert rows["N1"].ad_group_id is not None and rows["N1"].match_type == "exact"
    assert rows["N2"].ad_group_id is None and rows["N2"].campaign_id is not None
    assert rows["N2"].match_type == "phrase", "Amazon's NEGATIVE_ prefix is not a match type"


def test_a_portfolio_carries_its_budget(db, profile):
    _queue_structure(portfolios=[{"portfolioId": "P1", "name": "Skyzz", "state": "ENABLED",
                                  "inBudget": True,
                                  "budget": {"amount": 500.0, "currencyCode": "USD",
                                             "policy": "MONTHLY_RECURRING",
                                             "startDate": "2026-09-01"}}],
                     campaigns=[dict(CAMPAIGN, portfolioId="P1")])
    structure.sync_all(db, profile, client())
    row = db.execute(select(PpcPortfolio)).scalar_one()
    assert float(row.budget_amount) == 500.0 and row.budget_policy == "MONTHLY_RECURRING"
    assert row.in_budget is True
    assert db.execute(select(PpcCampaign)).scalar_one().portfolio_id == row.id


# ---------------- expressions ----------------
@pytest.mark.parametrize("expression,expected", [
    ([{"type": "QUERY_HIGH_REL_MATCHES"}], ("auto", "auto_close")),
    ([{"type": "QUERY_BROAD_REL_MATCHES"}], ("auto", "auto_loose")),
    ([{"type": "ASIN_SUBSTITUTE_RELATED"}], ("auto", "auto_substitutes")),
    ([{"type": "ASIN_ACCESSORY_RELATED"}], ("auto", "auto_complements")),
    ([{"type": "ASIN_SAME_AS", "value": "B01"}], ("product", "asin")),
    ([{"type": "ASIN_CATEGORY_SAME_AS", "value": "12345"}], ("category", "category")),
])
def test_amazons_expressions_become_the_words_a_ppc_manager_uses(expression, expected):
    target_type, match_type, _text = structure.describe_expression(expression)
    assert (target_type, match_type) == expected


def test_amazons_resolved_expression_is_preferred_when_it_says_more():
    _t, _m, text = structure.describe_expression(
        [{"type": "ASIN_CATEGORY_SAME_AS", "value": "12345"}],
        resolved=[{"type": "ASIN_CATEGORY_SAME_AS", "value": "Vape Accessories and Pouches"}])
    assert "Vape Accessories" in text, "a recommendation should name the category, not its id"
