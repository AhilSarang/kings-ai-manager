"""P3 M5: edits, price changes, approvals, the freshness rule, submission tracking and revert."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.amazon import changes as changes_svc
from app.amazon import listings as listings_svc
from app.amazon import patches
from app.amazon.fake import FakeAmazonClient as F
from app.db.models import Approval, AmazonChangeRequest, AmazonListing, AmazonListingVersion, Job, Product
from app.llm.fake import FakeLLMProvider
from tests.amazon.helpers import US, connect, expect_listings, listing_item
from tests.conftest import run_jobs
from tests.helpers import post, text_payload

PREVIEW_OK = {"status": "VALID", "issues": []}


@pytest.fixture
def client(db, owner):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def account(db, owner):
    from app.services.common import utcnow

    owner.last_inbound_at = utcnow()
    db.commit()
    return connect(db, write_mode="live")


@pytest.fixture
def listing(db, account):
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


def approval_of(db):
    db.expire_all()
    return db.execute(select(Approval).order_by(Approval.created_at.desc())).scalars().first()


def expect_edit_flow(price=None, title=None):
    """Queue: the freshness re-read, then Amazon's preview."""
    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001", listing_item(price=price or 24.99,
                                                                      title=title or "Skyzz Mouth Tape 60ct"))
    F.expect(r"/listings/2021-08-01/items/", PREVIEW_OK)


# ---------- patch building ----------
def test_patches_touch_only_the_requested_attributes(db, listing):
    expect_edit_flow()
    request = changes_svc.build(db, listing, {"item_name": "Skyzz Mouth Tape 120ct"},
                                operation="patch_attributes", instructions="rename it")
    ops = request.payload["patches"]
    assert len(ops) == 1 and ops[0]["path"] == "/attributes/item_name" and ops[0]["op"] == "replace"
    assert ops[0]["value"] == [{"value": "Skyzz Mouth Tape 120ct", "marketplace_id": US}]
    assert request.diff == [{"field": "item_name", "before": "Skyzz Mouth Tape 60ct",
                             "after": "Skyzz Mouth Tape 120ct"}]
    assert request.status == "ready_for_approval" and request.ref == "LC-1"


def test_new_attribute_uses_add_and_removal_uses_delete(db, listing):
    expect_edit_flow()
    request = changes_svc.build(db, listing, {"generic_keyword": "sleep strips"}, operation="patch_attributes")
    assert request.payload["patches"][0]["op"] == "add"
    expect_edit_flow()
    request = changes_svc.build(db, listing, {"bullet_point": None}, operation="patch_attributes")
    assert request.payload["patches"][0]["op"] == "delete"
    assert request.diff[0]["after"] == "(none)"


def test_price_patch_uses_purchasable_offer(db, listing):
    expect_edit_flow()
    request = changes_svc.price_change(db, listing, 21.99)
    value = request.payload["patches"][0]["value"]
    assert value[0]["currency"] == "USD"
    assert value[0]["our_price"][0]["schedule"][0]["value_with_tax"] == 21.99
    assert patches.price_of({"purchasable_offer": value}) == 21.99


# ---------- validation gates ----------
def test_a_change_that_fails_validation_never_reaches_approval(db, listing, owner):
    expect_edit_flow()
    request = changes_svc.build(db, listing, {"item_name": "Skyzz Tape Best Seller Free Shipping!!"},
                                operation="patch_attributes")
    assert request.status == "needs_info" and not request.local_validation["ok"]
    with pytest.raises(changes_svc.ChangeError, match="can't go to Amazon yet"):
        changes_svc.request_approval(db, request, owner)
    assert db.execute(select(Approval)).first() is None


def test_amazon_preview_errors_block_approval(db, listing, owner):
    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001", listing_item())
    F.expect(r"/listings/2021-08-01/items/", {"status": "INVALID", "issues": [
        {"code": "90220", "message": "Main image is required", "severity": "ERROR"}]})
    request = changes_svc.build(db, listing, {"item_name": "Skyzz Mouth Tape 120ct"}, operation="patch_attributes")
    assert request.status == "needs_info"
    assert any("Main image is required" in w for w in request.warnings)


def test_preview_failure_does_not_block_but_is_recorded(db, listing):
    from app.amazon.errors import AmazonRetryable

    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001", listing_item())
    F.expect(r"/listings/2021-08-01/items/", AmazonRetryable("Amazon unavailable"))
    request = changes_svc.build(db, listing, {"item_name": "Skyzz Mouth Tape 120ct"}, operation="patch_attributes")
    assert request.status == "ready_for_approval"
    assert "Amazon unavailable" in request.amazon_preview_result["error"]


def test_price_below_cost_makes_the_change_critical(db, listing):
    product = Product(internal_name="Mouth tape", unit_cost=9.00)
    db.add(product)
    db.flush()
    listing.product_id = product.id
    db.commit()
    expect_edit_flow()
    request = changes_svc.price_change(db, listing, 4.99)
    assert request.status == "needs_info"  # below cost is an error, so it needs the owner to confirm the cost first
    assert any("below what the unit costs" in e for e in request.local_validation["errors"])


def test_big_price_cut_requires_pin(db, listing):
    expect_edit_flow()
    request = changes_svc.price_change(db, listing, 12.99)  # −48%
    assert request.requires_pin and request.risk_level == "critical"
    assert any("-48% price change" in w for w in request.warnings)


# ---------- approval and execution ----------
def test_full_edit_flow_submits_once_and_schedules_checks(client, db, sent, listing, owner):
    expect_edit_flow()
    request = changes_svc.build(db, listing, {"item_name": "Skyzz Mouth Tape 120ct"},
                                operation="patch_attributes", instructions="rename")
    message = changes_svc.request_approval(db, request, owner)
    db.commit()
    a = approval_of(db)
    assert "LISTING CHANGE LC-1 — Edit (Skyzz, US)" in a.summary and "Before: Skyzz Mouth Tape 60ct" in a.summary
    assert f"APPROVE {a.ref} {a.confirmation_code}" in message

    for word in ("ok", "yes", "go ahead"):
        assert "Nothing was approved" in say(client, sent, word)
    assert not F.calls_to("patchListingsItem")

    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001", listing_item())  # freshness re-read
    F.expect(r"/listings/2021-08-01/items/", {"submissionId": "SUB-1", "status": "ACCEPTED", "issues": []})
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "sent to Amazon" in out
    db.expire_all()
    request = db.execute(select(AmazonChangeRequest)).scalar_one()
    assert request.status == "accepted" and request.submission_id == "SUB-1"
    patch_calls = [c for c in F.calls if c["method"] == "PATCH" and "mode" not in c["params"]]
    assert len(patch_calls) == 1, "exactly one write to Amazon"
    checks = db.execute(select(Job).where(Job.type == "amazon_check_submission")).scalars().all()
    assert len(checks) == 4  # 5 min, 30 min, 2 h, 24 h
    versions = listings_svc.versions(db, listing)
    assert versions[0].source == "submitted_change" and versions[0].change_request_id == request.id


def test_seller_central_edit_between_approval_and_execution_blocks_the_write(client, db, sent, listing, owner):
    expect_edit_flow()
    request = changes_svc.build(db, listing, {"item_name": "Skyzz Mouth Tape 120ct"}, operation="patch_attributes")
    changes_svc.request_approval(db, request, owner)
    db.commit()
    a = approval_of(db)
    # somebody edits the same field in Seller Central before the approval is used
    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001",
             listing_item(attributes={"item_name": [{"value": "Changed by hand"}]}))
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "changed in Seller Central after you approved" in out
    assert not [c for c in F.calls if c["method"] == "PATCH" and "mode" not in c["params"]]
    db.expire_all()
    assert db.execute(select(AmazonChangeRequest)).scalar_one().status == "cancelled"


def test_validation_only_mode_records_but_sends_nothing(client, db, sent, listing, owner, account):
    account.write_mode = "validation_only"
    db.commit()
    expect_edit_flow()
    request = changes_svc.build(db, listing, {"item_name": "Skyzz Mouth Tape 120ct"}, operation="patch_attributes")
    message = changes_svc.request_approval(db, request, owner)
    assert "validation only" in message
    db.commit()
    a = approval_of(db)
    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001", listing_item())
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "nothing was sent" in out and "AMAZON LIVE" in out
    assert not [c for c in F.calls if c["method"] == "PATCH" and "mode" not in c["params"]]


def test_paused_amazon_blocks_an_approved_write(client, db, sent, listing, owner):
    from app.services import settings_store
    from app.services.common import SYSTEM

    expect_edit_flow()
    request = changes_svc.build(db, listing, {"item_name": "Skyzz Mouth Tape 120ct"}, operation="patch_attributes")
    changes_svc.request_approval(db, request, owner)
    settings_store.set_value(db, "amazon_paused", True, SYSTEM)
    db.commit()
    a = approval_of(db)
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "Amazon is paused" in out
    assert not [c for c in F.calls if c["method"] == "PATCH" and "mode" not in c["params"]]


# ---------- submission tracking ----------
def _submit(db, listing, owner, client, sent):
    expect_edit_flow()
    request = changes_svc.build(db, listing, {"item_name": "Skyzz Mouth Tape 120ct"}, operation="patch_attributes")
    changes_svc.request_approval(db, request, owner)
    db.commit()
    a = approval_of(db)
    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001", listing_item())
    F.expect(r"/listings/2021-08-01/items/", {"submissionId": "SUB-1", "status": "ACCEPTED"})
    say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    db.expire_all()
    return db.execute(select(AmazonChangeRequest)).scalar_one()


def test_check_confirms_when_amazon_applied_the_change(client, db, sent, listing, owner):
    request = _submit(db, listing, owner, client, sent)
    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001",
             listing_item(attributes={"item_name": [{"value": "Skyzz Mouth Tape 120ct", "marketplace_id": US}]}))
    before = len(sent)
    assert changes_svc.check_submission(db, request, 5) == "live"
    db.commit()
    run_jobs(["send_owner_message", "send_messages"])
    assert request.status == "live_verified"
    assert any("LC-1 is live on Amazon" in s.get("text", "") for s in sent[before:])


def test_check_reports_amazon_errors(client, db, sent, listing, owner):
    request = _submit(db, listing, owner, client, sent)
    from tests.amazon.helpers import issue

    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001",
             listing_item(issues=[issue(message="Title is too long for this category")]))
    assert changes_svc.check_submission(db, request, 30) == "issues"
    db.commit()
    run_jobs(["send_owner_message", "send_messages"])
    assert request.status == "completed_with_issues"
    assert any("Title is too long" in s.get("text", "") for s in sent)


def test_after_24_hours_an_unapplied_change_offers_a_case(client, db, sent, listing, owner):
    request = _submit(db, listing, owner, client, sent)
    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001", listing_item())  # Amazon kept the old title
    assert changes_svc.check_submission(db, request, 24 * 60) == "not applied"
    db.commit()
    run_jobs(["send_owner_message", "send_messages"])
    assert request.status == "completed_with_issues"
    assert any("another contributor's data is ranked higher" in s.get("text", "") for s in sent)


# ---------- revert ----------
def test_revert_builds_a_patch_back_to_an_old_version(client, db, sent, listing, owner):
    expect_listings([listing_item(attributes={"item_name": [{"value": "Skyzz Mouth Tape 120ct"}]})])
    listings_svc.sync_all(db)
    db.expire_all()
    assert len(listings_svc.versions(db, listing)) == 2

    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001",
             listing_item(attributes={"item_name": [{"value": "Skyzz Mouth Tape 120ct"}]}))
    F.expect(r"/listings/2021-08-01/items/", PREVIEW_OK)
    request = changes_svc.build_revert(db, listing, 1, owner)
    assert request.operation == "revert" and request.payload["revert_to_version"] == 1
    title = [d for d in request.diff if d["field"] == "item_name"][0]
    assert title["before"] == "Skyzz Mouth Tape 120ct" and title["after"] == "Skyzz Mouth Tape 60ct"


def test_reverting_to_the_current_version_is_refused(db, listing, owner):
    F.expect(r"/listings/2021-08-01/items/[^/]+/SKZ-001", listing_item())
    with pytest.raises(changes_svc.ChangeError, match="nothing to revert"):
        changes_svc.build_revert(db, listing, 1, owner)


# ---------- commands and natural language ----------
def test_changes_list_detail_and_cancel(client, db, sent, listing, owner):
    expect_edit_flow()
    request = changes_svc.build(db, listing, {"item_name": "Skyzz Mouth Tape 120ct"}, operation="patch_attributes")
    db.commit()
    out = say(client, sent, "CHANGES")
    assert "LC-1 — L-1 SKZ-001 — item name (ready to approve)" in out
    out = say(client, sent, "LC-1")
    assert "*LC-1* — patch attributes on L-1" in out and "Before: Skyzz Mouth Tape 60ct" in out
    out = say(client, sent, "CANCEL LC-1")
    assert "cancelled" in out and "Nothing was sent to Amazon" in out
    db.expire_all()
    assert db.execute(select(AmazonChangeRequest)).scalar_one().status == "cancelled"


def test_plain_language_edit_and_price_change(client, db, sent, listing, owner):
    expect_edit_flow()
    out = say(client, sent, "change the title of L-1 to Skyzz Mouth Tape 120ct",
              act("amazon_edit_listing", listing_ref="L-1", changes={"item_name": "Skyzz Mouth Tape 120ct"},
                  instructions="rename"))
    assert "LC-1" in out and "APPROVE" in out
    expect_edit_flow()
    out = say(client, sent, "drop the price of SKZ-001 to 21.99",
              act("amazon_update_price", sku="SKZ-001", price=21.99))
    assert "LC-2" in out and "Price change" in out or "purchasable offer" in out


def test_edit_without_a_listing_reference_asks(client, db, sent, listing):
    out = say(client, sent, "change the title", act("amazon_edit_listing", changes={"item_name": "X"}))
    assert "Which listing?" in out


def test_amazon_query_answers_from_the_database(client, db, sent, listing):
    out = say(client, sent, "how are my listings doing?", act("amazon_query", question="listings"))
    assert "*Amazon listings* (1)" in out
    assert len([r for r in FakeLLMProvider.requests]) == 1  # one supervisor call, no extra LLM work
