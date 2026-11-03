"""P3 M4: every local check from brief Section 7, plus Amazon's VALIDATION_PREVIEW."""

import pytest
from sqlalchemy import select

from app.amazon import validation as v
from app.amazon.fake import FakeAmazonClient as F
from app.db.models import Brand, Product
from tests.amazon.helpers import US, connect

SCHEMA = {
    "type": "object",
    "required": ["item_name", "brand"],
    "properties": {
        "item_name": {"type": "array", "items": {"properties": {"value": {"type": "string", "maxLength": 200}}}},
        "brand": {"type": "array", "items": {"properties": {"value": {"type": "string"}}}},
        "bullet_point": {"type": "array", "items": {"properties": {"value": {"type": "string", "maxLength": 500}}}},
        "product_description": {"type": "array",
                                "items": {"properties": {"value": {"type": "string", "maxLength": 2000}}}},
    },
}


def attrs(**kw):
    out = {}
    for key, value in kw.items():
        out[key] = [{"value": v_, "marketplace_id": US} for v_ in (value if isinstance(value, list) else [value])]
    return out


def base(**kw):
    fields = {"item_name": "Skyzz Mouth Tape 60ct Gentle Hypoallergenic", "brand": "Skyzz"}
    fields.update(kw)
    return attrs(**fields)


@pytest.fixture
def account(db, owner):
    return connect(db)


def check(db, attributes, **kw):
    return v.validate(db, attributes=attributes, schema=SCHEMA, marketplace_id=US, **kw)


# ---------- schema ----------
def test_missing_required_field_is_a_plain_english_error(db, account):
    result = check(db, attrs(brand="Skyzz"))
    assert not result.ok
    assert "Amazon requires the title for this product type." in [f.message for f in result.errors]


def test_valid_attributes_pass(db, account):
    assert check(db, base()).ok


# ---------- title ----------
def test_title_length_uses_the_schema_limit(db, account):
    result = check(db, base(item_name="Skyzz " + "tape " * 60))
    assert any("Amazon allows 200 here" in f.message for f in result.errors)


@pytest.mark.parametrize("title,expected", [
    ("Skyzz Mouth Tape!! Best Seller", "promotional"),
    ("Skyzz Mouth Tape $9.99 {deal}", "doesn't allow in titles"),
])
def test_title_rejects_banned_characters_and_promotional_wording(db, account, title, expected):
    result = check(db, base(item_name=title))
    assert any(expected in f.message for f in result.errors), [f.message for f in result.errors]


def test_repeated_words_warn_but_do_not_block(db, account):
    result = check(db, base(item_name="Tape Tape Tape Skyzz Mouth Strips for the Mouth"))
    assert result.ok and any("repeats tape" in f.message for f in result.warnings)


def test_brand_words_in_the_title_are_not_counted_as_repeats(db, account):
    result = check(db, base(item_name="Skyzz Skyzz Skyzz Mouth Tape", brand="Skyzz"))
    assert not [f for f in result.warnings if "repeats" in f.message]


# ---------- bullets and description ----------
def test_bullets_and_description_limits_and_contact_details(db, account):
    result = check(db, base(bullet_point=["Fine", "Call us on 804-555-1212"],
                            product_description="Visit www.kingsdistro.com for more"))
    messages = " ".join(f.message for f in result.errors)
    assert "Bullet 2 contains a link, email address or phone number" in messages
    assert "The description contains a link" in messages


def test_description_html_is_rejected(db, account):
    result = check(db, base(product_description="<script>alert(1)</script> Great tape"))
    assert any("HTML Amazon doesn't allow" in f.message for f in result.errors)


def test_long_bullet_is_an_error(db, account):
    result = check(db, base(bullet_point="x" * 501))
    assert any("Bullet 1 is 501 characters" in f.message for f in result.errors)


# ---------- claims ----------
@pytest.mark.parametrize("text,label", [
    ("Cures sleep apnea disease overnight", "medical claim"),
    ("FDA approved for daily use", "FDA approval claim"),
    ("Antibacterial coating kills germs", "antibacterial"),
    ("Non-toxic adhesive", "non-toxic"),
    ("Eco-friendly and biodegradable", "environmental claim"),
    ("Contains CBD extract", "CBD/THC"),
    ("100% safe for children", "absolute safety claim"),
])
def test_restricted_claims_warn(db, account, text, label):
    result = check(db, base(bullet_point=text))
    assert any(label in f.message for f in result.warnings), [f.message for f in result.warnings]
    assert result.ok, "claims warn, they don't block"


# ---------- search terms ----------
def test_search_terms_byte_limit_asins_and_other_brands(db, account):
    result = check(db, base(generic_keyword="x" * 260))
    assert any("Amazon's limit is 250" in f.message for f in result.errors)
    result = check(db, base(generic_keyword="sleep strips B0ABCDE123"))
    assert any("contain an ASIN" in f.message for f in result.errors)
    result = check(db, base(generic_keyword="like nike tape"))
    assert any("another brand (nike)" in f.message for f in result.errors)


def test_search_terms_repeating_the_title_only_warn(db, account):
    result = check(db, base(item_name="Skyzz Mouth Tape", generic_keyword="mouth tape sleep strips"))
    assert result.ok and any("repeat words already in the title" in f.message for f in result.warnings)


# ---------- identifiers ----------
def test_barcode_check_digit_and_missing_barcode(db, account):
    result = check(db, base(externally_assigned_product_identifier="036000291453"), creating=True)
    assert any("fails its check digit" in f.message for f in result.errors)
    result = check(db, base(externally_assigned_product_identifier="036000291452"), creating=True)
    assert not [f for f in result.errors if "check digit" in f.message]
    result = check(db, base(), creating=True)
    assert any("no barcode" in f.message for f in result.errors)
    result = check(db, base(), creating=True, exemption_confirmed=True)
    assert not [f for f in result.errors if "barcode" in f.message]


def test_identifiers_are_not_checked_when_editing(db, account):
    assert not [f for f in check(db, base()).errors if "barcode" in f.message]


# ---------- brand ----------
def test_new_listing_under_an_unknown_brand_is_blocked_and_needs_pin(db, account):
    result = check(db, base(brand="Sony"), creating=True, exemption_confirmed=True)
    blocked = [f for f in result.errors if "brand-registered brands" in f.message]
    assert blocked and blocked[0].requires_pin


def test_known_but_not_own_brand_warns(db, account):
    db.add(Brand(name="Pellon", brand_registry=False, owned_by_company=False))
    db.commit()
    result = check(db, base(brand="Pellon"), creating=True, exemption_confirmed=True)
    assert result.ok and any(f.requires_pin for f in result.warnings)


def test_own_brands_pass(db, account):
    for brand in ("Skyzz", "Vibezy"):
        assert check(db, base(brand=brand), creating=True, exemption_confirmed=True).ok


# ---------- price ----------
def test_price_below_cost_needs_pin(db, account):
    product = Product(internal_name="Mouth tape", unit_cost=6.00)
    db.add(product)
    db.commit()
    result = check(db, base(), price=4.99, currency="USD", product=product)
    below = [f for f in result.errors if "below what the unit costs" in f.message]
    assert below and below[0].requires_pin


def test_unknown_cost_warns_instead_of_silently_skipping(db, account):
    result = check(db, base(), price=4.99, currency="USD")
    assert result.ok and any("don't know what this product costs" in f.message for f in result.warnings)


def test_currency_must_match_the_marketplace(db, account):
    result = check(db, base(), price=24.99, currency="CAD")
    assert any("US sells in USD" in f.message for f in result.errors)


@pytest.mark.parametrize("old,new,pin,warns", [(24.99, 20.49, False, True), (24.99, 21.99, False, False),
                                               (24.99, 12.99, True, True), (10.0, 20.0, True, True)])
def test_price_change_thresholds(db, account, old, new, pin, warns):
    result = check(db, base(), price=new, currency="USD", old_price=old)
    changes = [f for f in result.warnings if "price change" in f.message]
    assert bool(changes) is warns
    assert any(f.requires_pin for f in changes) is pin


# ---------- variations ----------
def test_variation_family_checks(db, account):
    children = [{"sku": "A", "attributes": attrs(size="Small")},
                {"sku": "B", "attributes": attrs(size="Small")},
                {"sku": "C", "attributes": attrs(size="")}]
    result = v.Result()
    v.check_variations(result, theme="SIZE", children=children, supported_themes=["SIZE", "COLOR"])
    messages = " ".join(f.message for f in result.errors)
    assert "have the same SIZE" in messages and "missing a SIZE value" in messages

    result = v.Result()
    v.check_variations(result, theme="FLAVOUR", children=[], supported_themes=["SIZE", "COLOR"])
    assert any("isn't a variation theme Amazon supports" in f.message for f in result.errors)

    result = v.Result()
    v.check_variations(result, theme="SIZE", children=[], parent_buyable=True)
    assert any("parent of a variation family must not be buyable" in f.message for f in result.errors)


# ---------- Amazon preview ----------
def test_preview_errors_block_and_warnings_do_not(db, account):
    F.expect(r"/listings/2021-08-01/items/", {"status": "INVALID", "issues": [
        {"code": "90220", "message": "Main image is required", "severity": "ERROR",
         "attributeNames": ["main_product_image_locator"]},
        {"code": "88110", "message": "Consider adding a bullet point", "severity": "WARNING"}]})
    from app.amazon.api import client

    result, raw = v.run_preview(client(), seller_id="S", sku="SKZ-001", marketplace_id=US,
                                body={"productType": "X", "patches": []})
    assert not result.ok
    assert "Amazon says: Main image is required (main product image locator)" in [f.message for f in result.errors]
    assert len(result.warnings) == 1 and raw["status"] == "INVALID"
    assert F.calls[-1]["params"]["mode"] == "VALIDATION_PREVIEW"


def test_preview_with_no_issues_passes(db, account):
    F.expect(r"/listings", {"status": "VALID", "issues": []})
    from app.amazon.api import client

    result, _ = v.run_preview(client(), seller_id="S", sku="SKZ-001", marketplace_id=US, body={}, method="PUT")
    assert result.ok and not result.findings
    assert F.calls[-1]["method"] == "PUT"


def test_result_serialises_for_storage(db, account):
    result = check(db, base(item_name="Skyzz Tape!! Free Shipping", bullet_point="Eco-friendly"))
    data = result.as_dict()
    assert data["ok"] is False and data["errors"] and data["warnings"]
    assert all(isinstance(m, str) for m in data["errors"] + data["warnings"])
