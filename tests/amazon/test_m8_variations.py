"""P3 M8: variation families, adding and removing children, batch approval, deletion with PIN + SKU."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.amazon import changes as changes_svc
from app.amazon import inventory as inv
from app.amazon import listings as listings_svc
from app.amazon import variations
from app.amazon.fake import FakeAmazonClient as F
from app.db.models import Approval, AmazonChangeRequest, AmazonListing
from app.llm.fake import FakeLLMProvider
from app.services import users
from tests.amazon.helpers import US, connect, expect_inventory, expect_listings, inventory_summary, listing_item
from tests.conftest import run_jobs
from tests.helpers import post, text_payload

SCHEMA = {"type": "object", "required": ["item_name"],
          "properties": {"item_name": {"type": "array"}, "size": {"type": "array"}, "color": {"type": "array"},
                         "variation_theme": {"type": "array", "items": {"properties": {
                             "name": {"enum": ["SIZE", "COLOR", "SIZE_COLOR"]}}}}}}
PIN = "4321"
BASE = {"item_name": [{"value": "Skyzz Mouth Tape", "marketplace_id": US}],
        "brand": [{"value": "Skyzz", "marketplace_id": US}]}


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
    owner.approval_pin_hash = users.hash_pin(PIN)
    db.commit()
    monkeypatch.setattr(schemas, "fetch_schema", lambda m, p: (SCHEMA, "2026-01"))
    return connect(db, write_mode="live")


@pytest.fixture
def parent(db, account):
    expect_listings([listing_item(sku="SKZ-PARENT", asin="B0SKYZZP", title="Skyzz Mouth Tape")])
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


def approval_of(db):
    db.expire_all()
    return db.execute(select(Approval).order_by(Approval.created_at.desc())).scalars().first()


GRID = "SKZ-S | Small | 24.99\nSKZ-M | Medium | 26.99\nSKZ-L | Large | 28.99"


# ---------- parsing ----------
def test_grid_parsing_handles_the_shapes_people_type():
    children = variations.parse_grid(GRID, "SIZE")
    assert [c["sku"] for c in children] == ["SKZ-S", "SKZ-M", "SKZ-L"]
    assert children[0]["values"] == {"size": "Small"} and children[0]["price"] == 24.99
    commas = variations.parse_grid("SKU, Size, Price\n- SKZ-S, Small, $24.99\n• SKZ-M, Medium, $26.99", "SIZE")
    assert [c["sku"] for c in commas] == ["SKZ-S", "SKZ-M"] and commas[1]["price"] == 26.99
    two = variations.parse_grid("SKZ-SB | Small | Blue | 24.99", "SIZE_COLOR")
    assert two[0]["values"] == {"size": "Small", "color": "Blue"} and two[0]["price"] == 24.99
    assert variations.parse_grid("nothing useful here", "SIZE") == []


def test_theme_attributes():
    assert variations.theme_attributes("size") == ["size"]
    assert variations.theme_attributes("SIZE_COLOR") == ["size", "color"]
    assert variations.theme_attributes("SIZE/COLOR") == ["size", "color"]


# ---------- building a family ----------
def test_family_is_one_change_request_with_parent_and_children(db, account, parent):
    request = variations.build_family(db, parent_sku="SKZ-PARENT", theme="SIZE",
                                      children=variations.parse_grid(GRID, "SIZE"),
                                      product_type="HEALTH_PERSONAL_CARE", marketplace_code="US",
                                      base_attributes=BASE)
    batch = request.payload["batch"]
    assert [b["role"] for b in batch] == ["parent", "child", "child", "child"]
    parent_attrs = batch[0]["attributes"]
    assert parent_attrs["parentage_level"][0]["value"] == "parent"
    assert "purchasable_offer" not in parent_attrs, "the parent must not be buyable"
    child = batch[1]["attributes"]
    assert child["child_parent_sku_relationship"][0]["parent_sku"] == "SKZ-PARENT"
    assert child["size"][0]["value"] == "Small"
    assert child["purchasable_offer"][0]["our_price"][0]["schedule"][0]["value_with_tax"] == 24.99
    assert request.requires_pin and request.risk_level == "critical"
    assert request.status == "ready_for_approval"


def test_duplicate_or_missing_theme_values_block_the_family(db, account, parent):
    request = variations.build_family(db, parent_sku="SKZ-PARENT", theme="SIZE",
                                      children=variations.parse_grid("SKZ-S | Small | 1.99\nSKZ-2 | Small | 2.99",
                                                                     "SIZE"),
                                      product_type="HEALTH_PERSONAL_CARE", marketplace_code="US",
                                      base_attributes=BASE)
    assert request.status == "needs_info"
    assert any("have the same SIZE" in e for e in request.local_validation["errors"])


def test_unsupported_theme_is_refused(db, account, parent):
    request = variations.build_family(db, parent_sku="SKZ-PARENT", theme="FLAVOR",
                                      children=variations.parse_grid("SKZ-A | Mint | 1.99\nSKZ-B | Berry | 2.99",
                                                                     "FLAVOR"),
                                      product_type="HEALTH_PERSONAL_CARE", marketplace_code="US",
                                      base_attributes=BASE)
    assert request.status == "needs_info"
    assert any("isn't a variation theme Amazon supports" in e for e in request.local_validation["errors"])


def test_one_child_is_not_a_family(db, account, parent):
    with pytest.raises(variations.VariationError, match="at least two children"):
        variations.build_family(db, parent_sku="SKZ-PARENT", theme="SIZE",
                                children=variations.parse_grid("SKZ-S | Small | 1.99", "SIZE"),
                                product_type="X", marketplace_code="US", base_attributes={})


# ---------- batch approval and execution ----------
def test_family_approval_shows_every_sku_then_creates_them(client, db, sent, account, parent, owner):
    request = variations.build_family(db, parent_sku="SKZ-PARENT", theme="SIZE",
                                      children=variations.parse_grid(GRID, "SIZE"),
                                      product_type="HEALTH_PERSONAL_CARE", marketplace_code="US",
                                      base_attributes=BASE)
    changes_svc.request_approval(db, request, owner)
    db.commit()
    a = approval_of(db)
    assert a.risk_level == "critical"
    for sku in ("SKZ-PARENT", "SKZ-S", "SKZ-M", "SKZ-L"):
        assert sku in a.summary
    assert "Small · 24.99" in a.summary

    for _ in range(4):
        F.expect(r"/listings/2021-08-01/items/", {"submissionId": "SUB", "status": "ACCEPTED"})
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code} {PIN}")
    assert "4 of 4 SKUs sent to Amazon" in out
    puts = [c for c in F.calls if c["method"] == "PUT" and "mode" not in c["params"]]
    assert len(puts) == 4 and puts[0]["path"].endswith("SKZ-PARENT"), "parent first"
    db.expire_all()
    assert db.execute(select(AmazonChangeRequest)).scalar_one().status == "accepted"


def test_partial_failure_reports_each_sku(client, db, sent, account, parent, owner):
    from app.amazon.errors import AmazonError

    request = variations.build_family(db, parent_sku="SKZ-PARENT", theme="SIZE",
                                      children=variations.parse_grid(GRID, "SIZE"),
                                      product_type="HEALTH_PERSONAL_CARE", marketplace_code="US",
                                      base_attributes=BASE)
    changes_svc.request_approval(db, request, owner)
    db.commit()
    a = approval_of(db)
    F.expect(r"/SKZ-PARENT", {"submissionId": "S1", "status": "ACCEPTED"})
    F.expect(r"/SKZ-S", {"submissionId": "S2", "status": "ACCEPTED"})
    F.expect(r"/SKZ-M", AmazonError("putListingsItem: size value is not valid [InvalidInput]"))
    F.expect(r"/SKZ-L", {"submissionId": "S4", "status": "ACCEPTED"})
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code} {PIN}")
    assert "3 of 4 SKUs sent" in out and "SKZ-M: putListingsItem" in out
    assert "were not created" in out
    db.expire_all()
    assert db.execute(select(AmazonChangeRequest)).scalar_one().status == "completed_with_issues"


# ---------- add and remove children ----------
def test_add_child_needs_approval_but_not_pin(db, account, parent, owner):
    request = variations.add_child(db, parent, variations.parse_grid("SKZ-XL | XLarge | 30.99", "SIZE")[0],
                                   theme="SIZE", base_attributes=BASE)
    assert request.operation == "add_variation_child" and not request.requires_pin
    assert request.risk_level == "high"
    assert request.payload["attributes"]["size"][0]["value"] == "XLarge"


def test_remove_child_detaches_without_deleting(db, account, parent, owner):
    child = AmazonListing(account_id=parent.account_id, marketplace_id=US, sku="SKZ-S", parentage="child",
                          parent_listing_id=parent.id, product_type="HEALTH_PERSONAL_CARE")
    db.add(child)
    db.commit()
    request = variations.remove_child(db, child)
    assert request.operation == "remove_variation_child" and request.requires_pin
    assert request.payload["patches"][0]["op"] == "delete"
    assert "not deleted" in request.diff[0]["after"]
    assert any("stock stay on Amazon" in w for w in request.warnings)


# ---------- deletion ----------
def test_deletion_warns_about_stock_and_needs_pin_plus_sku(client, db, sent, account, parent, owner):
    expect_inventory([inventory_summary(sku="SKZ-PARENT", fulfillable=42, inbound_shipped=60, reserved=3)])
    inv.snapshot(db)
    request = variations.build_delete(db, parent)
    message = changes_svc.request_approval(db, request, owner)
    db.commit()
    a = approval_of(db)
    assert a.risk_level == "critical" and a.action_payload["confirm_text"] == "SKZ-PARENT"
    assert "42 sellable, 60 inbound and 3 reserved" in a.summary
    assert "becomes stranded" in a.summary and "keep the last version" in a.summary
    assert f"APPROVE {a.ref} {a.confirmation_code} <PIN> SKZ-PARENT" in message

    assert "needs the SKU typed back" in say(client, sent, f"APPROVE {a.ref} {a.confirmation_code} {PIN}")
    assert not [c for c in F.calls if c["method"] == "DELETE"]
    assert "needs the SKU typed back" in say(client, sent, f"APPROVE {a.ref} {a.confirmation_code} {PIN} SKZ-WRONG")
    assert not [c for c in F.calls if c["method"] == "DELETE"]

    F.expect(r"/listings/2021-08-01/items/", {"status": "ACCEPTED"})
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code} {PIN} SKZ-PARENT")
    assert "deleted from Amazon" in out and "stranded" in out
    deletes = [c for c in F.calls if c["method"] == "DELETE"]
    assert len(deletes) == 1 and deletes[0]["path"].endswith("SKZ-PARENT")
    db.expire_all()
    assert db.get(AmazonListing, parent.id).current_version_id is not None, "the last version is kept"


def test_deletion_of_a_listing_with_no_stock_still_warns_about_recreation(db, account, parent, owner):
    request = variations.build_delete(db, parent)
    assert any("keep the last version" in w for w in request.warnings)
    assert not any("sellable" in w for w in request.warnings)


# ---------- natural language ----------
def test_plain_language_family_and_deletion(client, db, sent, account, parent, owner):
    out = say(client, sent, f"create a size family under SKZ-PARENT:\n{GRID}",
              act("amazon_create_variations", parent_sku="SKZ-PARENT", theme="SIZE", children=GRID))
    assert "create variation family" in out and "APPROVE" in out and "<PIN>" in out

    out = say(client, sent, "delete L-1", act("amazon_delete_listing", listing_ref="L-1"))
    assert "delete listing" in out and "SKZ-PARENT" in out


def test_family_without_a_parent_listing_says_so(client, db, sent, account):
    out = say(client, sent, "create a family under NOPE-1",
              act("amazon_create_variations", parent_sku="NOPE-1", theme="SIZE", children=GRID))
    assert "Create the parent listing first" in out
