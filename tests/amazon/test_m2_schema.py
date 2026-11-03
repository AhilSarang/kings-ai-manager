"""P3 M2: tables, seeds, settings and the action registry with tiers and PIN rules."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.actions.registry import ACTIONS, Tier, get_spec
from app.db.models import (
    AmazonAccount,
    AmazonAlert,
    AmazonChangeRequest,
    AmazonListing,
    AmazonListingVersion,
    Brand,
    FbaBox,
    FbaInboundRequest,
    Product,
    Setting,
    ShipFromAddress,
)
from app.services import settings_store


@pytest.fixture
def account(db):
    a = AmazonAccount(seller_id="A2SPK7LLKUKRL", region="na", status="active",
                      enabled_marketplaces=["US"], write_mode="validation_only")
    db.add(a)
    db.commit()
    return a


def listing(db, account, sku="SKZ-001", **kw):
    row = AmazonListing(account_id=account.id, marketplace_id="ATVPDKIKX0DER", sku=sku, **kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------- seeds ----------
def test_brands_seeded(db):
    brands = {b.name: b for b in db.execute(select(Brand)).scalars()}
    assert set(brands) == {"Skyzz", "Vibezy"}
    assert all(b.brand_registry and b.owned_by_company for b in brands.values())


def test_default_ship_from_address_seeded_without_contact_details(db):
    a = db.execute(select(ShipFromAddress).where(ShipFromAddress.is_default.is_(True))).scalar_one()
    assert (a.city, a.state_or_province, a.postal_code, a.country_code) == ("Richmond", "VA", "23228", "US")
    assert a.address_line1 == "9012 Hermitage Rd"
    # Amazon requires both; the owner supplies them in setup, so they must start empty.
    assert a.phone is None and a.email is None


def test_amazon_settings_seeded_with_brief_defaults(db):
    expected = {"amazon_price_change_warn_pct": 15, "amazon_price_change_pin_pct": 40,
                "amazon_low_stock_days_cover": 21, "amazon_sales_drop_alert_pct": 40,
                "amazon_sync_interval_minutes": 60, "fba_default_shipping_mode": "small_parcel",
                "fba_default_label_owner": "SELLER", "fba_default_carrier_choice": "ask",
                "fba_pin_required_above_usd": 250, "amazon_paused": False}
    for key, value in expected.items():
        assert settings_store.get(db, key) == value, key
    assert db.scalar(select(func.count()).select_from(Setting).where(Setting.key.like("fba_%"))) >= 6


# ---------- refs and constraints ----------
def test_refs_increment_with_their_prefixes(db, account):
    assert listing(db, account, sku="A").ref == "L-1"
    assert listing(db, account, sku="B").ref == "L-2"
    cr = AmazonChangeRequest(marketplace_id="ATVPDKIKX0DER", operation="patch_attributes")
    alert = AmazonAlert(type="low_stock", severity="medium")
    req = FbaInboundRequest(marketplace_id="ATVPDKIKX0DER")
    db.add_all([cr, alert, req])
    db.commit()
    assert (cr.ref, alert.ref, req.ref) == ("LC-1", "AZ-1", "FB-1")


def test_sku_is_unique_per_account_and_marketplace(db, account):
    listing(db, account, sku="DUP")
    with pytest.raises(IntegrityError):
        listing(db, account, sku="DUP")
    db.rollback()
    # the same SKU in another marketplace is fine (US and Canada listings coexist)
    row = AmazonListing(account_id=account.id, marketplace_id="A2EUQ1WTGCTBG2", sku="DUP")
    db.add(row)
    db.commit()
    assert row.ref.startswith("L-")


@pytest.mark.parametrize("model,field,bad", [
    (AmazonChangeRequest, "status", "whatever"),
    (AmazonChangeRequest, "operation", "delete_everything"),
    (AmazonAlert, "type", "made_up"),
    (FbaInboundRequest, "status", "shipped_maybe"),
    (FbaInboundRequest, "shipping_mode", "drone"),
    (FbaInboundRequest, "label_owner", "NOBODY"),
])
def test_check_constraints_reject_unknown_values(db, model, field, bad):
    kw = {"marketplace_id": "ATVPDKIKX0DER"} if model is not AmazonAlert else {}
    if model is AmazonChangeRequest:
        kw.setdefault("operation", "patch_attributes")
    if model is AmazonAlert:
        kw.setdefault("type", "low_stock")
    kw[field] = bad
    db.add(model(**kw))
    with pytest.raises((IntegrityError, DBAPIError)):
        db.commit()
    db.rollback()


def test_listing_version_history_and_current_pointer(db, account):
    row = listing(db, account)
    v1 = AmazonListingVersion(listing_id=row.id, version_number=1, source="amazon_sync",
                              attributes={"title": "First"})
    db.add(v1)
    db.commit()
    row.current_version_id = v1.id
    db.commit()
    v2 = AmazonListingVersion(listing_id=row.id, version_number=2, source="submitted_change",
                              attributes={"title": "Second"})
    db.add(v2)
    db.commit()
    with pytest.raises(IntegrityError):  # version numbers are unique per listing
        db.add(AmazonListingVersion(listing_id=row.id, version_number=2, source="revert", attributes={}))
        db.commit()
    db.rollback()
    assert db.get(AmazonListing, row.id).current_version_id == v1.id


def test_product_case_pack_and_cost_are_optional(db):
    brand = db.execute(select(Brand).where(Brand.name == "Vibezy")).scalar_one()
    p = Product(internal_name="Mouth tape 60ct", brand_id=brand.id)
    db.add(p)
    db.commit()
    assert p.unit_cost is None and p.units_per_case is None and p.cost_currency == "USD"
    p.unit_cost, p.units_per_case = 4.20, 60
    p.case_dimensions, p.case_weight_lb = {"l": 18, "w": 14, "h": 12}, 32
    db.commit()
    assert float(db.get(Product, p.id).unit_cost) == 4.20


def test_boxes_carry_contents_and_group_counts(db, account):
    req = FbaInboundRequest(marketplace_id="ATVPDKIKX0DER")
    db.add(req)
    db.commit()
    box = FbaBox(request_id=req.id, box_group=1, box_count=8, length_in=18, width_in=14, height_in=12,
                 weight_lb=32, contents=[{"msku": "SKZ-001", "quantity_per_box": 60}])
    db.add(box)
    db.commit()
    assert db.get(FbaBox, box.id).contents[0]["quantity_per_box"] == 60


# ---------- action registry ----------
def test_reads_are_tier_zero():
    for t in ("amazon_read_listing", "amazon_search_catalog", "amazon_fetch_schema",
              "amazon_validation_preview", "fba_list_options"):
        assert get_spec(t).tier == Tier.READ, t


def test_drafting_and_planning_are_tier_one():
    for t in ("amazon_build_change_request", "amazon_import_images", "fba_create_inbound_plan",
              "fba_set_packing_information", "fba_generate_options"):
        assert get_spec(t).tier == Tier.INTERNAL, t


def test_every_write_to_amazon_needs_approval():
    writes = ("amazon_patch_attributes", "amazon_create_offer_existing_asin", "amazon_update_price",
              "amazon_add_variation_child", "amazon_revert", "amazon_create_new_asin",
              "amazon_create_variation_family", "amazon_remove_variation_child", "amazon_full_replace",
              "amazon_change_brand_or_product_type", "amazon_delete_listing", "fba_confirm_shipment",
              "fba_update_tracking", "fba_cancel_confirmed_plan")
    for t in writes:
        assert get_spec(t).tier == Tier.EXTERNAL, t


def test_pin_protected_actions_are_critical():
    # critical risk is what makes the approval message demand the PIN (Phase 1 approvals).
    for t in ("amazon_create_new_asin", "amazon_delete_listing", "amazon_full_replace",
              "amazon_create_variation_family", "amazon_remove_variation_child",
              "amazon_change_brand_or_product_type", "fba_cancel_confirmed_plan"):
        assert get_spec(t).min_risk == "critical", t
    assert get_spec("amazon_patch_attributes").min_risk == "high"
    assert get_spec("fba_confirm_shipment").min_risk == "high"


def test_no_amazon_action_is_prohibited_or_missing():
    amazon = [t for t in ACTIONS if t.startswith(("amazon_", "fba_"))]
    assert len(amazon) >= 30
    assert not [t for t in amazon if ACTIONS[t].tier == Tier.PROHIBITED]


def test_amazon_agent_can_request_tier_three(db):
    from app.actions.registry import check_permitted
    from app.services import agents as agents_svc

    agent = agents_svc.get_by_key(db, "amazon")
    assert agent.permissions["max_tier"] == 3
    check_permitted("amazon_delete_listing", agent.permissions)
    check_permitted("fba_confirm_shipment", agent.permissions)
