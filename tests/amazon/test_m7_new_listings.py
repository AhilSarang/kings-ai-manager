"""P3 M7: new listings — duplicate check, product type, attribute mapping, missing info, creation."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.amazon import intake
from app.amazon.fake import FakeAmazonClient as F
from app.db.models import Approval, AmazonChangeRequest, AmazonListing
from app.llm.fake import FakeLLMProvider
from tests.amazon.helpers import US, connect, expect_listings, listing_item
from tests.conftest import run_jobs
from tests.helpers import post, text_payload

SCHEMA = {"type": "object", "required": ["item_name", "brand"],
          "properties": {k: {"type": "array"} for k in
                         ["item_name", "brand", "bullet_point", "product_description", "generic_keyword",
                          "externally_assigned_product_identifier", "purchasable_offer", "condition_type",
                          "main_product_image_locator"]}}

DETAILS = ("New listing for Vibezy. Title: Vibezy Lash Growth Serum 5ml. Bullets: peptide complex; "
           "dermatologist tested; 60-day supply. Price $24.99 US. UPC 036000291452. FBA.")


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
    monkeypatch.setattr(schemas, "fetch_schema", lambda marketplace, product_type: (SCHEMA, "2026-01"))
    return connect(db, write_mode="live")


def say(client, sent, text, *llm):
    FakeLLMProvider.script(*llm)
    before = len(sent)
    assert post(client, text_payload(text)).status_code == 200
    run_jobs(limit=100)
    return "\n---\n".join(s.get("text", f"[{s['kind']}]") for s in sent[before:])


def act(type_, **params):
    return {"actions": [{"type": type_, "params": params}], "reply_to_owner": ""}


def mapped(attributes=None, missing=(), notes=()):
    return {"attributes": attributes if attributes is not None else {
        "item_name": [{"value": "Vibezy Lash Growth Serum 5ml", "marketplace_id": US}],
        "brand": [{"value": "Vibezy", "marketplace_id": US}],
        "bullet_point": [{"value": "Peptide complex", "marketplace_id": US}],
        "externally_assigned_product_identifier": [{"value": "036000291452", "marketplace_id": US}],
        "purchasable_offer": [{"marketplace_id": US, "currency": "USD",
                               "our_price": [{"schedule": [{"value_with_tax": 24.99}]}]}],
    }, "missing": [dict(m) for m in missing], "notes": list(notes)}


def type_choice(name="BEAUTY", confidence=0.95, alternatives=()):
    return {"product_type": name, "confidence": confidence, "reason": "Amazon lists it under beauty.",
            "alternatives": list(alternatives)}


def expect_no_duplicates():
    F.expect(r"/catalog/2022-04-01/items", {"items": []})   # barcode
    F.expect(r"/catalog/2022-04-01/items", {"items": []})   # keywords


def expect_product_types(names=("BEAUTY", "HEALTH_PERSONAL_CARE")):
    F.expect(r"/definitions/2020-09-01/productTypes", {"productTypes": [{"name": n} for n in names]})


# ---------- duplicate check ----------
def test_matching_barcode_offers_the_existing_asin(db, account, owner):
    F.expect(r"/catalog/2022-04-01/items", {"items": [
        {"asin": "B0EXISTING", "summaries": [{"marketplaceId": US, "itemName": "Vibezy Lash Serum"}]}]})
    out = intake.start(db, user=owner, sku="VBZ-010", marketplace_code="US", details=DETAILS)
    assert "Amazon already has this product (matched by barcode)" in out
    assert "B0EXISTING" in out and "add an offer on <ASIN>" in out
    assert db.execute(select(AmazonChangeRequest)).first() is None


def test_our_own_listing_is_offered_for_editing(db, account, owner):
    expect_listings([listing_item(sku="VBZ-010", title="Vibezy Lash Growth Serum 5ml")])
    from app.amazon import listings as listings_svc

    listings_svc.sync_all(db)
    F.expect(r"/catalog/2022-04-01/items", {"items": []})
    F.expect(r"/catalog/2022-04-01/items", {"items": []})
    out = intake.start(db, user=owner, sku="VBZ-011", marketplace_code="US",
                       details="Vibezy Lash Growth Serum 5ml, price $24.99")
    assert "You already have this listed" in out and "L-1" in out


def test_bad_barcode_is_caught_before_any_call(db, account, owner):
    with pytest.raises(intake.IntakeError, match="fails its check digit"):
        intake.duplicate_check(db, "US", identifier="036000291453", keywords="", brand=None)


# ---------- product type ----------
def test_low_confidence_product_type_asks_amit(db, account, owner):
    expect_no_duplicates()
    expect_product_types()
    FakeLLMProvider.script(type_choice(confidence=0.4, alternatives=["HEALTH_PERSONAL_CARE"]))
    out = intake.start(db, user=owner, sku="VBZ-010", marketplace_code="US", details=DETAILS)
    assert "I'm not certain which Amazon product type" in out
    assert "1. BEAUTY" in out and "2. HEALTH_PERSONAL_CARE" in out

    F.expect(r"/listings/2021-08-01/items/", {"status": "VALID", "issues": []})
    FakeLLMProvider.script(mapped())
    out = intake.answer(db, user=owner, answers="2")
    assert "Draft listing LC-1" in out and "HEALTH_PERSONAL_CARE" in out


def test_a_product_type_the_llm_invented_is_refused(db, account, owner):
    expect_no_duplicates()
    expect_product_types()
    FakeLLMProvider.script(type_choice(name="MADE_UP_TYPE"))
    with pytest.raises(intake.IntakeError, match="couldn't match that to an Amazon product type"):
        intake.start(db, user=owner, sku="VBZ-010", marketplace_code="US", details=DETAILS)


# ---------- missing information ----------
def test_missing_fields_are_asked_once_then_the_draft_completes(client, db, sent, account, owner):
    expect_no_duplicates()
    expect_product_types()
    out = say(client, sent, DETAILS,
              act("amazon_create_listing", sku="VBZ-010", marketplace="US", details=DETAILS),
              type_choice(),
              mapped(missing=[{"attribute": "country_of_origin", "question": "Where is it made?"},
                              {"attribute": "item_form", "question": "Is it a serum, gel or oil?"}]))
    assert "I need a few more details" in out
    assert "Where is it made?" in out and "Is it a serum, gel or oil?" in out
    assert db.execute(select(AmazonChangeRequest)).first() is None

    F.expect(r"/listings/2021-08-01/items/", {"status": "VALID", "issues": []})
    out = say(client, sent, "made in USA, it's a serum",
              act("amazon_provide_info", answers="made in USA, it's a serum"),
              mapped())
    assert "Draft listing LC-1" in out and "APPROVE" in out
    prompt = FakeLLMProvider.requests[-1].prompt
    assert "made in USA" in prompt, "the answers must reach the mapping call"


def test_answers_without_a_pending_draft_say_so(db, account, owner):
    assert "don't have a listing waiting" in intake.answer(db, user=owner, answers="made in USA")


# ---------- attribute mapping guards ----------
def test_mapping_prompt_carries_the_schema_and_owner_words_only(db, account, owner):
    from app.amazon import intake as i

    FakeLLMProvider.script(mapped())
    i.map_attributes(db, description=DETAILS, product_type="BEAUTY", schema=SCHEMA, marketplace_code="US")
    prompt = FakeLLMProvider.requests[-1].prompt
    system = FakeLLMProvider.requests[-1].system
    assert "<required_attributes>" in prompt and "item_name" in prompt
    assert "UPC 036000291452" in prompt and "<owner_message>" in prompt
    assert "never invent" in system.lower() and "never an instruction" in system.lower()


def test_plain_values_are_wrapped_into_amazons_shape(db, account, owner):
    FakeLLMProvider.script({"attributes": {"item_name": "Vibezy Lash Serum"}, "missing": [], "notes": []})
    mapping = intake.map_attributes(db, description=DETAILS, product_type="BEAUTY", schema=SCHEMA,
                                    marketplace_code="US")
    assert mapping.attributes["item_name"] == [{"value": "Vibezy Lash Serum", "marketplace_id": US}]


# ---------- creating ----------
def test_new_listing_is_critical_and_needs_pin(client, db, sent, account, owner):
    expect_no_duplicates()
    expect_product_types()
    F.expect(r"/listings/2021-08-01/items/", {"status": "VALID", "issues": []})
    out = say(client, sent, DETAILS,
              act("amazon_create_listing", sku="VBZ-010", marketplace="US", details=DETAILS),
              type_choice(), mapped())
    db.expire_all()
    request = db.execute(select(AmazonChangeRequest)).scalar_one()
    assert request.operation == "create_new_asin" and request.requires_pin and request.risk_level == "critical"
    approval = db.execute(select(Approval)).scalar_one()
    assert approval.risk_level == "critical" and "<PIN>" in out
    assert F.calls[-1]["params"]["mode"] == "VALIDATION_PREVIEW" and F.calls[-1]["method"] == "PUT"


def test_approved_creation_sends_a_put_once(client, db, sent, account, owner):
    expect_no_duplicates()
    expect_product_types()
    F.expect(r"/listings/2021-08-01/items/", {"status": "VALID", "issues": []})
    say(client, sent, DETAILS, act("amazon_create_listing", sku="VBZ-010", marketplace="US", details=DETAILS),
        type_choice(), mapped())
    db.expire_all()
    approval = db.execute(select(Approval)).scalar_one()
    from app.services import users

    pin = "4321"
    owner.approval_pin_hash = users.hash_pin(pin)
    db.commit()
    F.expect(r"/listings/2021-08-01/items/", {"submissionId": "SUB-9", "status": "ACCEPTED", "issues": []})
    out = say(client, sent, f"APPROVE {approval.ref} {approval.confirmation_code} {pin}")
    assert "created" in out
    puts = [c for c in F.calls if c["method"] == "PUT" and "mode" not in c["params"]]
    assert len(puts) == 1 and puts[0]["json"]["requirements"] == "LISTING"
    db.expire_all()
    request = db.execute(select(AmazonChangeRequest)).scalar_one()
    assert request.status == "accepted" and request.submission_id == "SUB-9"
    listing = db.execute(select(AmazonListing)).scalar_one()
    assert listing.sku == "VBZ-010" and listing.current_version_id is not None


def test_offer_on_an_existing_asin_uses_offer_only_requirements(db, account, owner):
    expect_product_types()
    F.expect(r"/listings/2021-08-01/items/", {"status": "VALID", "issues": []})
    FakeLLMProvider.script(type_choice(), mapped())
    intake.start(db, user=owner, sku="VBZ-010", marketplace_code="US", details=DETAILS, asin="B0EXISTING")
    request = db.execute(select(AmazonChangeRequest)).scalar_one()
    assert request.operation == "create_offer_existing_asin"
    assert request.payload["requirements"] == "LISTING_OFFER_ONLY"
    assert F.calls[-1]["json"]["requirements"] == "LISTING_OFFER_ONLY"


def test_validation_failure_blocks_the_draft(db, account, owner):
    expect_no_duplicates()
    expect_product_types()
    FakeLLMProvider.script(type_choice(), mapped(attributes={
        "item_name": [{"value": "Vibezy Serum FREE SHIPPING best seller!!", "marketplace_id": US}],
        "brand": [{"value": "Vibezy", "marketplace_id": US}]}))
    out = intake.start(db, user=owner, sku="VBZ-010", marketplace_code="US", details=DETAILS)
    db.expire_all()
    request = db.execute(select(AmazonChangeRequest)).scalar_one()
    assert request.status == "needs_info"
    assert any("promotional" in e for e in request.local_validation["errors"])
    assert "⛔" in out


def test_duplicate_sku_is_refused(db, account, owner):
    expect_listings([listing_item(sku="VBZ-010")])
    from app.amazon import listings as listings_svc

    listings_svc.sync_all(db)
    with pytest.raises(intake.IntakeError, match="already exists"):
        intake.build_new_listing(db, sku="VBZ-010", marketplace_code="US", product_type="BEAUTY",
                                 attributes={}, instructions="")


def test_images_in_the_message_are_imported_into_the_draft(db, account, owner):
    from app.amazon.drive import FakeDriveClient
    from tests.amazon.test_m6_images import jpeg

    FakeDriveClient.add_file("IMG1", "main.jpg", jpeg(), folder="FOLDER9")
    details = DETAILS + " Images: https://drive.google.com/drive/folders/FOLDER9"
    expect_no_duplicates()
    expect_product_types()
    F.expect(r"/listings/2021-08-01/items/", {"status": "VALID", "issues": []})
    FakeLLMProvider.script(type_choice(), mapped())
    out = intake.start(db, user=owner, sku="VBZ-010", marketplace_code="US", details=details)
    db.expire_all()
    request = db.execute(select(AmazonChangeRequest)).scalar_one()
    assert "main_product_image_locator" in request.payload["attributes"]
    assert "main image: 1600×1600px, published" in out
    assert "can take a few hours" in out
