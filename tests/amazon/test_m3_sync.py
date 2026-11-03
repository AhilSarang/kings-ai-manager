"""P3 M3: importing listings, versions, inventory, schema cache, catalogue search and the commands."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.amazon import accounts, catalog, inventory, listings, schemas
from app.amazon.errors import AmazonAuthError, AmazonRetryable
from app.amazon.fake import FakeAmazonClient as F
from app.db.models import AmazonApiCall, AmazonListing, AmazonListingVersion, AmazonProductTypeSchema
from app.llm.fake import FakeLLMProvider
from tests.amazon.helpers import (
    CA,
    US,
    connect,
    expect_inventory,
    expect_listings,
    inventory_summary,
    issue,
    listing_item,
    sales,
)
from tests.conftest import run_jobs
from tests.helpers import post, text_payload


@pytest.fixture
def client(db, owner):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def account(db, owner):
    from app.services.common import utcnow

    owner.last_inbound_at = utcnow()  # 24h WhatsApp window open, as it is in daily use
    db.commit()
    return connect(db)


def say(client, sent, text, *llm):
    FakeLLMProvider.script(*llm)
    before = len(sent)
    assert post(client, text_payload(text)).status_code == 200
    run_jobs(limit=100)
    return "\n---\n".join(s.get("text", f"[{s['kind']}]") for s in sent[before:])


# ---------- account ----------
def test_account_row_comes_from_env(db, owner):
    account = accounts.ensure(db)
    assert account.seller_id and account.region == "na"
    assert account.enabled_marketplaces == ["US"] and account.write_mode == "validation_only"
    assert accounts.ensure(db).id == account.id  # idempotent
    assert not accounts.writes_allowed(db)  # validation_only


def test_first_use_creates_the_account_row(db, owner):
    from app.db.models import AmazonAccount

    assert db.execute(select(AmazonAccount)).first() is None
    assert accounts.require(db).seller_id  # a fresh deployment doesn't need anyone to press anything


def test_require_explains_when_not_connected_or_broken(db, owner, account):
    accounts.set_status(db, account, "auth_error", "Login with Amazon refused the refresh token")
    with pytest.raises(accounts.AmazonNotConnected, match="refused the refresh token"):
        accounts.require(db)


def test_inventory_quantities_parse_as_numbers_or_breakdowns():
    from app.amazon.inventory import _detail

    real = {"inventoryDetails": {"fulfillableQuantity": 120, "inboundWorkingQuantity": 0,
                                 "inboundShippedQuantity": 48, "inboundReceivingQuantity": 0,
                                 "reservedQuantity": {"totalReservedQuantity": 6, "pendingCustomerOrderQuantity": 6},
                                 "unfulfillableQuantity": {"totalUnfulfillableQuantity": 3,
                                                           "customerDamagedQuantity": 3},
                                 "researchingQuantity": {"totalResearchingQuantity": 1}}}
    assert _detail(real) == {"fulfillable": 120, "inbound": 48, "reserved": 6, "unfulfillable": 4}
    older = {"inventoryDetails": {"fulfillableQuantity": 5, "unfulfillableQuantity": 2}}
    assert _detail(older) == {"fulfillable": 5, "inbound": 0, "reserved": 0, "unfulfillable": 2}


# ---------- listing sync ----------
def test_sync_imports_listings_with_price_status_and_issues(db, account):
    expect_listings([listing_item(), listing_item(sku="VBZ-010", asin="B0VIBEZY10", title="Vibezy Lash Serum",
                                                 price=18.5, brand="Vibezy", status=("DISCOVERABLE",),
                                                 issues=[issue()])])
    stats = listings.sync_all(db)
    assert stats["stored"] == 2
    rows = {r.sku: r for r in db.execute(select(AmazonListing)).scalars()}
    skyzz = rows["SKZ-001"]
    assert skyzz.ref == "L-1" and skyzz.asin == "B0SKYZZ001" and float(skyzz.price_cached) == 24.99
    assert skyzz.currency == "USD" and skyzz.buyable and skyzz.fulfillment == "FBA"
    assert skyzz.brand.name == "Skyzz" and skyzz.product_type == "HEALTH_PERSONAL_CARE"
    vibezy = rows["VBZ-010"]
    assert not vibezy.buyable and vibezy.issues_cached[0]["severity"] == "ERROR"
    assert vibezy.issues_cached[0]["attributes"] == ["main_product_image_locator"]


def test_sync_follows_pages_and_is_repeatable(db, account):
    expect_listings([listing_item()], page_two=[listing_item(sku="SKZ-002", asin="B0SKYZZ002")])
    assert listings.sync_all(db)["stored"] == 2
    expect_listings([listing_item(price=21.99), listing_item(sku="SKZ-002", asin="B0SKYZZ002")])
    listings.sync_all(db)
    rows = db.execute(select(AmazonListing)).scalars().all()
    assert len(rows) == 2, "a second sync must update in place, not duplicate"
    assert float(db.execute(select(AmazonListing).where(AmazonListing.sku == "SKZ-001"))
                 .scalar_one().price_cached) == 21.99


def test_a_version_is_kept_only_when_attributes_change(db, account):
    expect_listings([listing_item()])
    listings.sync_all(db)
    listing = db.execute(select(AmazonListing)).scalar_one()
    assert len(listings.versions(db, listing)) == 1 and listing.current_version_id

    expect_listings([listing_item()])  # identical attributes
    listings.sync_all(db)
    assert len(listings.versions(db, listing)) == 1

    expect_listings([listing_item(attributes={"item_name": [{"value": "Skyzz Mouth Tape 120ct"}]})])
    listings.sync_all(db)
    db.expire_all()
    history = listings.versions(db, listing)
    assert [v.version_number for v in history] == [2, 1]
    assert history[0].attributes["item_name"][0]["value"] == "Skyzz Mouth Tape 120ct"
    assert history[0].source == "amazon_sync"
    assert db.get(AmazonListing, listing.id).current_version_id == history[0].id


def test_sync_one_refetches_a_single_listing(db, account):
    expect_listings([listing_item()])
    listings.sync_all(db)
    listing = db.execute(select(AmazonListing)).scalar_one()
    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001", listing_item(price=19.99))
    listings.sync_one(db, listing)
    assert float(db.get(AmazonListing, listing.id).price_cached) == 19.99
    assert F.calls_to("/SKZ-001")[0]["operation"] == "getListingsItem"


def test_sync_covers_every_enabled_marketplace(db, account):
    account.enabled_marketplaces = ["US", "CA"]
    db.commit()
    F.expect(r"/listings", {"items": [listing_item()]})
    F.expect(r"/listings", {"items": [listing_item(marketplace_id=CA, price=32.99, currency="CAD")]})
    listings.sync_all(db)
    rows = db.execute(select(AmazonListing)).scalars().all()
    assert {r.marketplace_id for r in rows} == {US, CA}
    assert {r.currency for r in rows} == {"USD", "CAD"}


def test_api_calls_are_logged_to_the_database(db, account, monkeypatch):
    from app.amazon import api

    # the fake client bypasses the recorder, so exercise it directly
    api._record({"operation": "searchListingsItems", "marketplace_id": US, "sku": None, "http_status": 200,
                 "request_id": "req-1", "duration_ms": 42, "error_code": None})
    db.expire_all()
    row = db.execute(select(AmazonApiCall)).scalar_one()
    assert (row.operation, row.http_status, row.request_id) == ("searchListingsItems", 200, "req-1")


# ---------- inventory ----------
def test_inventory_snapshot_and_days_of_cover(db, account):
    expect_inventory([inventory_summary(fulfillable=120, inbound_shipped=60, reserved=5, unfulfillable=2),
                      inventory_summary(sku="VBZ-010", asin="B0VIBEZY10", fulfillable=0)])
    assert inventory.snapshot(db) == 2
    snap = inventory.for_sku(db, "SKZ-001")
    assert (snap.fulfillable_qty, snap.inbound_qty, snap.reserved_qty, snap.unfulfillable_qty) == (120, 60, 5, 2)
    assert inventory.days_of_cover(db, "SKZ-001") is None  # no sales history yet
    sales(db, "SKZ-001", units_per_day=4)
    assert inventory.daily_velocity(db, "SKZ-001") == 4.0
    assert inventory.days_of_cover(db, "SKZ-001") == 30.0


def test_latest_snapshot_wins(db, account):
    expect_inventory([inventory_summary(fulfillable=120)])
    inventory.snapshot(db)
    expect_inventory([inventory_summary(fulfillable=80)])
    inventory.snapshot(db)
    assert [s.fulfillable_qty for s in inventory.latest(db)] == [80]


# ---------- schema cache ----------
def test_schema_is_downloaded_once_then_cached(db, account, monkeypatch):
    calls = []

    def fake_fetch(marketplace_code, product_type):
        calls.append(product_type)
        return {"properties": {"item_name": {}}, "required": ["item_name"]}, "2024-01"

    monkeypatch.setattr(schemas, "fetch_schema", fake_fetch)
    first = schemas.get_schema(db, "US", "HEALTH_PERSONAL_CARE")
    second = schemas.get_schema(db, "US", "HEALTH_PERSONAL_CARE")
    assert first == second and calls == ["HEALTH_PERSONAL_CARE"]
    row = db.execute(select(AmazonProductTypeSchema)).scalar_one()
    assert row.schema_version == "2024-01" and row.locale == "en_US"
    schemas.get_schema(db, "US", "HEALTH_PERSONAL_CARE", refresh=True)
    assert len(calls) == 2


def test_stale_schema_is_refreshed(db, account, monkeypatch):
    from datetime import timedelta

    from app.services.common import utcnow

    monkeypatch.setattr(schemas, "fetch_schema", lambda m, p: ({"v": 1}, "old"))
    schemas.get_schema(db, "US", "TOY")
    row = db.execute(select(AmazonProductTypeSchema)).scalar_one()
    row.fetched_at = utcnow() - timedelta(days=8)
    db.commit()
    monkeypatch.setattr(schemas, "fetch_schema", lambda m, p: ({"v": 2}, "new"))
    assert schemas.get_schema(db, "US", "TOY") == {"v": 2}


# ---------- catalogue ----------
@pytest.mark.parametrize("code,ok", [("036000291452", True), ("036000291453", False), ("4006381333931", True),
                                     ("10614141000415", True), ("12345", False), ("", False)])
def test_identifier_check_digits(code, ok):
    assert catalog.check_digit_ok(code) is ok


def test_catalog_search_by_upc_and_keywords(db, account):
    F.expect(r"/catalog/2022-04-01/items", {"items": [{"asin": "B0EXISTING",
                                                       "summaries": [{"marketplaceId": US, "itemName": "Existing"}]}]})
    found = catalog.search_by_identifier("US", "036000291452")
    assert found[0]["asin"] == "B0EXISTING"
    assert F.calls[-1]["params"]["identifiersType"] == "UPC"
    F.expect(r"/catalog/2022-04-01/items", {"items": []})
    assert catalog.search_by_keywords("US", "mouth tape", brand="Skyzz") == []
    assert F.calls[-1]["params"]["brandNames"] == "Skyzz"


def test_catalog_ignores_identifiers_it_cannot_type(db, account):
    assert catalog.search_by_identifier("US", "nonsense") == []
    assert F.calls == []


# ---------- jobs ----------
def test_sync_job_runs_and_respects_the_pause(db, account, sent):
    from app.jobs import queue
    from app.services import settings_store
    from app.services.common import SYSTEM

    settings_store.set_value(db, "amazon_paused", True, SYSTEM)
    queue.enqueue(db, "amazon_sync_listings", {})
    db.commit()
    run_jobs(["amazon_sync_listings"])
    assert db.execute(select(AmazonListing)).first() is None

    settings_store.set_value(db, "amazon_paused", False, SYSTEM)
    expect_listings([listing_item()])
    queue.enqueue(db, "amazon_sync_listings", {})
    db.commit()
    run_jobs(["amazon_sync_listings"])
    db.expire_all()
    assert db.execute(select(AmazonListing)).scalar_one().sku == "SKZ-001"


def test_auth_failure_pauses_and_warns_once(db, account, sent):
    from app.jobs import queue

    for _ in range(2):
        F.expect(r"/listings", AmazonAuthError("Login with Amazon refused the refresh token"))
        queue.enqueue(db, "amazon_sync_listings", {})
        db.commit()
        run_jobs(["amazon_sync_listings", "send_owner_message", "send_messages"])
    db.expire_all()
    assert accounts.get_primary(db).status == "auth_error"
    warnings = [s for s in sent if "Amazon access stopped working" in s.get("text", "")]
    assert len(warnings) == 1


def test_retryable_error_is_retried_not_dropped(db, account):
    from app.db.models import Job
    from app.jobs import queue

    F.expect(r"/listings", AmazonRetryable("rate limited"))
    queue.enqueue(db, "amazon_sync_listings", {})
    db.commit()
    run_jobs(["amazon_sync_listings"])
    db.expire_all()
    job = db.execute(select(Job).where(Job.type == "amazon_sync_listings")).scalar_one()
    assert job.status == "pending" and job.attempts == 1


# ---------- commands ----------
def test_listings_command_groups_by_health(client, db, sent, account):
    expect_listings([listing_item(),
                     listing_item(sku="VBZ-010", asin="B0VIBEZY10", title="Vibezy Lash Serum", brand="Vibezy",
                                  status=("DISCOVERABLE",), issues=[issue()])])
    listings.sync_all(db)
    expect_inventory([inventory_summary(fulfillable=120, inbound_shipped=60)])
    inventory.snapshot(db)
    out = say(client, sent, "LISTINGS")
    assert "*Amazon listings* (2)" in out and "1 fine · 1 with errors" in out
    assert "L-2 Vibezy Lash Serum" in out and "❗ 1 error(s)" in out
    assert "L-1 Skyzz Mouth Tape 60ct" in out and "120 in stock (+60 inbound)" in out
    assert "USD 24.99" in out and FakeLLMProvider.requests == []


def test_listing_detail_by_ref_sku_and_asin(client, db, sent, account):
    expect_listings([listing_item(issues=[issue(severity="WARNING", message="Add a bullet point")])])
    listings.sync_all(db)
    expect_inventory([inventory_summary(fulfillable=120, inbound_shipped=60, reserved=5)])
    inventory.snapshot(db)
    sales(db, "SKZ-001", units_per_day=4)
    out = say(client, sent, "L-1")
    assert "*L-1* Skyzz Mouth Tape 60ct" in out and "SKU: SKZ-001 · ASIN: B0SKYZZ001" in out
    assert "US · USD 24.99 · FBA · buyable" in out
    assert "120 sellable · 60 inbound · 5 reserved" in out and "about 30.0 days" in out
    assert "Sold in the last 7 days: 28" in out and "⚠️ Add a bullet point" in out
    assert say(client, sent, "sku SKZ-001").startswith("🛒 *L-1*")
    assert say(client, sent, "ASIN B0SKYZZ001").startswith("🛒 *L-1*")
    assert "no listing for SKU NOPE" in say(client, sent, "sku NOPE")


def test_stock_command_sorts_by_urgency(client, db, sent, account):
    expect_listings([listing_item(), listing_item(sku="VBZ-010", asin="B0VIBEZY10")])
    listings.sync_all(db)
    expect_inventory([inventory_summary(fulfillable=20, inbound_shipped=100),
                      inventory_summary(sku="VBZ-010", asin="B0VIBEZY10", fulfillable=0),
                      inventory_summary(sku="SKZ-900", asin="B0SKYZZ900", fulfillable=500)])
    inventory.snapshot(db)
    sales(db, "SKZ-001", units_per_day=4)
    sales(db, "SKZ-900", asin="B0SKYZZ900", units_per_day=1)
    out = say(client, sent, "STOCK")
    assert "*Out of stock*" in out and "VBZ-010 — 0 sellable, nothing inbound" in out
    assert "SKZ-001 — 20 left, about 5.0 days, 100 inbound" in out
    assert "1 other SKU(s) have enough stock" in out


def test_versions_command(client, db, sent, account):
    expect_listings([listing_item()])
    listings.sync_all(db)
    expect_listings([listing_item(attributes={"item_name": [{"value": "Skyzz Mouth Tape 120ct"}]})])
    listings.sync_all(db)
    out = say(client, sent, "VERSIONS L-1")
    assert "*Versions of L-1*" in out and "V2 ·" in out and "← current" in out
    assert "read from Amazon" in out and "REVERT L-1 V<n>" in out
    assert "can't find listing L-9" in say(client, sent, "versions L-9")


def test_pause_and_resume_amazon(client, db, sent, account):
    from app.db.models import Job
    from app.services import settings_store

    assert "Amazon paused" in say(client, sent, "AMAZON PAUSE")
    db.expire_all()
    assert settings_store.get(db, "amazon_paused") is True
    expect_listings([listing_item()])
    assert "Amazon resumed" in say(client, sent, "amazon resume")
    db.expire_all()
    assert settings_store.get(db, "amazon_paused") is False
    assert db.execute(select(Job).where(Job.type == "amazon_sync_listings")).first() is not None


def test_listings_command_before_the_first_sync(client, db, sent, account):
    assert "No Amazon listings stored yet" in say(client, sent, "listings")
    assert "No FBA inventory stored yet" in say(client, sent, "stock")
