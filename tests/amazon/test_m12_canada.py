"""P3 M12: Canada — enabling the marketplace, copying listings, CAD pricing, bilingual warning."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.amazon import canada
from app.amazon import listings as listings_svc
from app.amazon import patches
from app.amazon.fake import FakeAmazonClient as F
from app.db.models import AmazonChangeRequest, AmazonListing, Approval
from app.llm.fake import FakeLLMProvider
from tests.amazon.helpers import CA, US, connect, expect_listings, listing_item
from tests.conftest import run_jobs
from tests.helpers import post, text_payload

SCHEMA = {"type": "object", "required": ["item_name"], "properties": {k: {"type": "array"} for k in
          ["item_name", "brand", "bullet_point", "purchasable_offer"]}}


@pytest.fixture
def client(db, owner):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def account(db, owner, monkeypatch):
    from app.amazon import schemas
    from app.services.common import utcnow

    owner.last_inbound_at = utcnow()
    db.commit()
    monkeypatch.setattr(schemas, "fetch_schema", lambda m, p: (SCHEMA, "2026-01"))
    return connect(db, write_mode="live")


@pytest.fixture
def us_listing(db, account):
    expect_listings([listing_item()])
    listings_svc.sync_all(db)
    return db.execute(select(AmazonListing)).scalar_one()


def say(client, sent, text, *llm):
    FakeLLMProvider.script(*llm)
    before = len(sent)
    assert post(client, text_payload(text)).status_code == 200
    run_jobs(limit=100)
    return "\n---\n".join(s.get("text", f"[{s['kind']}]") for s in sent[before:])


def act(type_, **params):
    return {"actions": [{"type": type_, "params": params}], "reply_to_owner": ""}


def enable_ca(db, owner):
    from app.services.common import Actor

    canada.set_enabled(db, ["US", "CA"], Actor.user(owner.id))
    db.commit()


# ---------- enabling ----------
def test_canada_is_off_until_it_is_enabled(db, us_listing, owner):
    assert canada.enabled_codes(db) == ["US"]
    with pytest.raises(canada.CopyError, match="isn't enabled yet"):
        canada.copy_listing(db, us_listing, target_code="CA", price=32.99)
    enable_ca(db, owner)
    assert canada.enabled_codes(db) == ["US", "CA"]


def test_enable_command(client, db, sent, us_listing):
    out = say(client, sent, "enable canada")
    assert "marketplaces enabled for changes: CA, US" in out
    db.expire_all()
    assert set(canada.enabled_codes(db)) == {"US", "CA"}


def test_unknown_marketplace_is_refused(db, us_listing, owner):
    from app.services.common import Actor

    with pytest.raises(KeyError, match="Unknown marketplace 'UK'"):
        canada.set_enabled(db, ["US", "UK"], Actor.user(owner.id))


# ---------- copying ----------
def test_copy_needs_a_price_from_amit(db, us_listing, owner):
    enable_ca(db, owner)
    with pytest.raises(canada.CopyError, match="I don't convert prices"):
        canada.copy_listing(db, us_listing, target_code="CA", price=None)
    assert db.execute(select(AmazonChangeRequest)).first() is None


def test_copy_creates_a_ca_draft_with_the_given_price(db, us_listing, owner):
    enable_ca(db, owner)
    F.expect(r"/catalog/2022-04-01/items", {"items": []})          # ASIN not in CA yet
    F.expect(r"/listings/2021-08-01/items/", {"status": "VALID", "issues": []})
    request = canada.copy_listing(db, us_listing, target_code="CA", price=32.99)
    assert request.operation == "create_new_asin" and request.marketplace_id == CA
    attributes = request.payload["attributes"]
    assert patches.price_of(attributes) == 32.99
    assert attributes["purchasable_offer"][0]["currency"] == "CAD"
    assert attributes["item_name"][0]["marketplace_id"] == CA, "content is re-tagged for Canada"
    assert any("bilingual" in w for w in request.warnings)


def test_copy_adds_an_offer_when_the_asin_already_exists_in_canada(db, us_listing, owner):
    enable_ca(db, owner)
    F.expect(r"/catalog/2022-04-01/items", {"items": [{"asin": "B0SKYZZ001"}]})
    F.expect(r"/listings/2021-08-01/items/", {"status": "VALID", "issues": []})
    request = canada.copy_listing(db, us_listing, target_code="CA", price=32.99)
    assert request.operation == "create_offer_existing_asin"
    assert request.payload["requirements"] == "LISTING_OFFER_ONLY"
    summary = canada.copy_summary(db, request, us_listing, "CA")
    assert "adds our offer rather than creating a second ASIN" in summary


def test_copying_twice_is_refused(db, us_listing, owner):
    enable_ca(db, owner)
    F.expect(r"/catalog/2022-04-01/items", {"items": []})
    F.expect(r"/listings/2021-08-01/items/", {"status": "VALID", "issues": []})
    canada.copy_listing(db, us_listing, target_code="CA", price=32.99)
    with pytest.raises(canada.CopyError, match="already exists in CA"):
        canada.copy_listing(db, us_listing, target_code="CA", price=32.99)


def test_copy_to_the_same_marketplace_is_refused(db, us_listing, owner):
    with pytest.raises(canada.CopyError, match="already the US listing"):
        canada.copy_listing(db, us_listing, target_code="US", price=10)


def test_ca_validation_uses_the_ca_schema_and_preview(db, us_listing, owner):
    enable_ca(db, owner)
    F.expect(r"/catalog/2022-04-01/items", {"items": []})
    F.expect(r"/listings/2021-08-01/items/", {"status": "INVALID", "issues": [
        {"code": "90220", "message": "Le titre est trop long", "severity": "ERROR"}]})
    request = canada.copy_listing(db, us_listing, target_code="CA", price=32.99)
    assert request.status == "needs_info"
    assert any("Le titre est trop long" in w for w in request.warnings)
    preview_call = [c for c in F.calls if c["params"].get("mode") == "VALIDATION_PREVIEW"][-1]
    assert preview_call["params"]["marketplaceIds"] == CA


# ---------- bulk ----------
def test_brand_copy_needs_every_price(db, account, owner):
    expect_listings([listing_item(sku="VBZ-1", asin="B0VIB00001", brand="Vibezy"),
                     listing_item(sku="VBZ-2", asin="B0VIB00002", brand="Vibezy")])
    listings_svc.sync_all(db)          # sync US before Canada is enabled, or sync_all wants CA pages too
    enable_ca(db, owner)
    with pytest.raises(canada.CopyError, match="I need CA prices for: VBZ-1, VBZ-2"):
        canada.copy_brand(db, "Vibezy", prices={})
    for _ in range(2):
        F.expect(r"/catalog/2022-04-01/items", {"items": []})
        F.expect(r"/listings/2021-08-01/items/", {"status": "VALID", "issues": []})
    requests = canada.copy_brand(db, "Vibezy", prices={"VBZ-1": 29.99, "VBZ-2": 34.99})
    assert len(requests) == 2
    assert {patches.price_of(r.payload["attributes"]) for r in requests} == {29.99, 34.99}


def test_price_lines_are_parsed():
    prices = canada.parse_prices("VBZ-1 29.99\nVBZ-2 | 34.99\nVBZ-3: $39.99\nnot a price line")
    assert prices == {"VBZ-1": 29.99, "VBZ-2": 34.99, "VBZ-3": 39.99}


# ---------- commands and natural language ----------
def test_copy_command_end_to_end(client, db, sent, us_listing, owner):
    enable_ca(db, owner)
    F.expect(r"/catalog/2022-04-01/items", {"items": []})
    F.expect(r"/listings/2021-08-01/items/", {"status": "VALID", "issues": []})
    out = say(client, sent, "COPY L-1 TO CA 32.99")
    assert "Copying L-1 SKZ-001 to CA" in out
    assert "CAD 32.99 (your number, not a conversion)" in out
    assert "bilingual" in out and "APPROVE" in out
    db.expire_all()
    approval = db.execute(select(Approval)).scalar_one()
    assert approval.risk_level == "critical", "a new ASIN, so the PIN is needed"


def test_copy_without_a_price_asks_for_one(client, db, sent, us_listing, owner):
    enable_ca(db, owner)
    out = say(client, sent, "copy L-1 to CA")
    assert "I don't convert prices" in out
    assert db.execute(select(AmazonChangeRequest)).first() is None


def test_plain_language_copy(client, db, sent, us_listing, owner):
    enable_ca(db, owner)
    F.expect(r"/catalog/2022-04-01/items", {"items": []})
    F.expect(r"/listings/2021-08-01/items/", {"status": "VALID", "issues": []})
    out = say(client, sent, "put L-1 on amazon canada at 32.99 canadian",
              act("amazon_copy_marketplace", listing_ref="L-1", target="CA", price=32.99))
    assert "Copying L-1" in out and "CAD 32.99" in out
