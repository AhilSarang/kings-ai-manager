"""P3 M9: shipment intake, pre-checks, plan building and the placement options message."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.amazon import fba_intake as intake
from app.amazon import fba_plan
from app.amazon import listings as listings_svc
from app.amazon.fake import FakeAmazonClient as F
from app.services import settings_store
from app.db.models import (
    AmazonListing,
    FbaBox,
    FbaInboundItem,
    FbaInboundRequest,
    FbaPlacementOption,
    Product,
    ShipFromAddress,
)
from app.llm.fake import FakeLLMProvider
from tests.amazon.helpers import US, connect, expect_listings, listing_item, sales
from tests.conftest import run_jobs
from tests.helpers import post, text_payload

MESSAGE = ("Create FBA shipment: ASIN B0SKYZZ001, 480 units, 8 boxes, 60 per box, 18x14x12 in, "
           "32 lb each, ready Monday.")


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
def ready(db, account):
    """A synced FBA listing, a product with prep set, and a complete ship-from address."""
    expect_listings([listing_item()])
    listings_svc.sync_all(db)
    listing = db.execute(select(AmazonListing)).scalar_one()
    product = Product(internal_name="Mouth tape", prep_category="NONE")
    db.add(product)
    db.flush()
    listing.product_id = product.id
    address = db.execute(select(ShipFromAddress)).scalar_one()
    address.phone, address.email = "+13469011127", "ahil@kingsdistro.com"
    db.commit()
    return listing


def say(client, sent, text, *llm):
    FakeLLMProvider.script(*llm)
    before = len(sent)
    assert post(client, text_payload(text)).status_code == 200
    run_jobs(limit=100)
    return "\n---\n".join(s.get("text", f"[{s['kind']}]") for s in sent[before:])


def act(type_, **params):
    return {"actions": [{"type": type_, "params": params}], "reply_to_owner": ""}


def _in_days(days: int) -> str:
    """Amazon's timestamps, relative to today — hardcoded dates rot the moment the calendar moves."""
    from app.services.common import utcnow

    return f"{utcnow() + timedelta(days=days):%Y-%m-%dT%H:%M:%SZ}"


def plan_responses(*, options=2, fees=(0.0, 12.5)):
    """Queue the whole Amazon conversation for one plan build."""
    F.expect(r"/inboundPlans$", {"inboundPlanId": "PLAN1", "operationId": "OP1"})
    F.expect(r"/packingOptions$", {"operationId": "OP2"})
    F.expect(r"/packingOptions$", {"packingOptions": [{"packingOptionId": "PO1", "packingGroups": ["PG1"]}]})
    F.expect(r"/packingOptions/PO1/confirmation", {"operationId": "OP3"})
    F.expect(r"/packingOptions$", {"packingOptions": [{"packingOptionId": "PO1", "packingGroups": ["PG1"]}]})
    F.expect(r"/packingInformation", {"operationId": "OP4"})
    F.expect(r"/placementOptions$", {"operationId": "OP5"})
    placement = []
    for i in range(options):
        placement.append({"placementOptionId": f"PL{i+1}", "status": "OFFERED",
                          "expiration": _in_days(3),
                          "fees": [{"value": {"amount": fees[i], "code": "USD"}, "type": "FEE"}],
                          "shipmentIds": [f"SH{i+1}"] if i == 0 else [f"SH{i+1}a", f"SH{i+1}b"]})
    F.expect(r"/placementOptions$", {"placementOptions": placement})
    F.expect(r"/shipments/SH1", {"destination": {"warehouseId": "RIC2", "address": {
        "city": "Richmond", "stateOrProvinceCode": "VA", "postalCode": "23231"}},
        "summary": {"boxCount": 8, "unitCount": 480}})
    F.expect(r"/shipments/SH2a", {"destination": {"warehouseId": "ABE8", "address": {
        "city": "Florence", "stateOrProvinceCode": "NJ", "postalCode": "08518"}},
        "summary": {"boxCount": 5, "unitCount": 300}})
    F.expect(r"/shipments/SH2b", {"destination": {"warehouseId": "CLT2", "address": {
        "city": "Charlotte", "stateOrProvinceCode": "NC", "postalCode": "28273"}},
        "summary": {"boxCount": 3, "unitCount": 180}})


# ---------- parsing ----------
def test_the_briefs_example_message_parses(db):
    parsed = intake.parse(MESSAGE)
    [item] = parsed.items
    assert item.asin == "B0SKYZZ001" and item.quantity == 480 and item.boxes == 8
    assert item.units_per_box == 60 and item.dimensions == (18.0, 14.0, 12.0) and item.weight_lb == 32.0
    assert parsed.complete


def test_two_products_in_one_message(db):
    parsed = intake.parse("Shipment: B0AAA00001 240 units in 4 boxes 16x12x10 26lb, "
                          "B0BBB00002 120 units in 2 boxes 20x16x12 38lb. Ship Monday.")
    assert [i.asin for i in parsed.items] == ["B0AAA00001", "B0BBB00002"]
    assert parsed.items[1].quantity == 120 and parsed.items[1].weight_lb == 38.0
    assert parsed.ready_date is not None


def test_missing_weight_is_a_question_not_a_guess(db):
    parsed = intake.parse("ASIN B0SKYZZ001, 480 units, 8 boxes, 18x14x12")
    assert not parsed.complete
    assert any("weigh" in q and "won't guess" in q for q in parsed.questions)
    assert all(i.weight_lb is None for i in parsed.items)


def test_quantity_that_does_not_divide_asks_for_units_per_box(db):
    parsed = intake.parse("ASIN B0SKYZZ001, 500 units, 8 boxes, 18x14x12 in, 32 lb")
    assert any("don't divide evenly" in q for q in parsed.questions)


def test_pallets_and_dates(db):
    parsed = intake.parse("Send 40 cases on pallets, ASIN B0SKYZZ001 600 units, 12 boxes 40x40x45 in 600 lb, "
                          "ready 2026-10-02")
    assert parsed.shipping_mode == "ltl_pallet" and parsed.ready_date.isoformat() == "2026-10-02"


def test_a_message_with_no_product_asks_which(db):
    parsed = intake.parse("send some stock to amazon")
    assert any("Which product?" in q for q in parsed.questions)


# ---------- pre-checks ----------
def test_prechecks_pass_on_a_complete_message(db, ready):
    check = intake.precheck(db, intake.parse(MESSAGE))
    assert check.ok and check.resolved[0]["units_per_box"] == 60
    assert check.resolved[0]["listing"].sku == "SKZ-001"


def test_asin_we_do_not_sell_is_refused(db, ready):
    check = intake.precheck(db, intake.parse("ASIN B0NOTOURS1, 10 units, 1 box, 10x10x10, 5 lb"))
    assert any("isn't one of our listings" in e for e in check.errors)


def test_non_fba_listing_is_refused_with_a_way_forward(db, ready, account):
    ready.fulfillment = "FBM"
    db.commit()
    check = intake.precheck(db, intake.parse(MESSAGE))
    assert any("not set to FBA" in e and "convert it to FBA" in e for e in check.errors)


def test_ambiguous_asin_asks_which_sku(db, ready, account):
    db.add(AmazonListing(account_id=ready.account_id, marketplace_id=US, sku="SKZ-001-B",
                         asin="B0SKYZZ001", fulfillment="FBA", buyable=True))
    db.commit()
    check = intake.precheck(db, intake.parse(MESSAGE))
    assert any("maps to 2 of our SKUs" in e for e in check.errors)


def test_box_limits(db, ready):
    over_side = intake.precheck(db, intake.parse("ASIN B0SKYZZ001, 10 units, 1 box, 30x14x12 in, 20 lb"))
    assert any("over Amazon's 25 in limit" in e for e in over_side.errors)
    over_weight = intake.precheck(db, intake.parse("ASIN B0SKYZZ001, 10 units, 1 box, 18x14x12 in, 60 lb"))
    assert any("over Amazon's 50 lb limit" in e for e in over_weight.errors)
    near = intake.precheck(db, intake.parse("ASIN B0SKYZZ001, 10 units, 1 box, 18x14x12 in, 48 lb, "
                                            "ready Monday"))
    assert near.ok and any("Team Lift" in w for w in near.warnings)


def test_missing_prep_is_asked_once_and_then_remembered(db, ready, account):
    ready.product_id = None
    db.commit()
    check = intake.precheck(db, intake.parse(MESSAGE))
    assert any("What prep does SKZ-001 need?" in q for q in check.questions)
    fba_plan.set_prep(db, "SKZ-001", "poly bag")
    db.commit()
    assert intake.precheck(db, intake.parse(MESSAGE)).ok


def test_ship_from_without_phone_or_email_asks(db, ready):
    address = db.execute(select(ShipFromAddress)).scalar_one()
    address.phone = None
    db.commit()
    check = intake.precheck(db, intake.parse(MESSAGE))
    assert any("phone number and email" in q for q in check.questions)


def test_too_much_stock_warns_but_does_not_block(db, ready):
    sales(db, "SKZ-001", units_per_day=1)
    check = intake.precheck(db, intake.parse(MESSAGE))
    assert check.ok and any("months of stock" in w for w in check.warnings)


# ---------- plan building ----------
def test_plan_build_walks_amazons_flow_and_stores_options(db, ready, sent):
    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed), instructions=MESSAGE)
    db.commit()
    plan_responses()
    fba_plan.build_plan(db, request)
    db.commit()

    calls = [c["operation"] for c in F.calls if c["operation"]]
    for operation in ("createInboundPlan", "generatePackingOptions", "confirmPackingOption",
                      "setPackingInformation", "generatePlacementOptions", "getShipment"):
        assert operation in calls, operation
    assert "generateTransportationOptions" not in calls, "carrier quotes come after placement is confirmed"

    packing = F.last_body("/packingInformation")["packageGroupings"][0]
    assert packing["boxes"][0]["quantity"] == 8
    assert packing["boxes"][0]["contentInformationSource"] == "BOX_CONTENT_PROVIDED"
    assert packing["boxes"][0]["items"][0] == {"msku": "SKZ-001", "quantity": 60, "labelOwner": "SELLER",
                                               "prepOwner": "SELLER"}
    assert packing["boxes"][0]["weight"] == {"unit": "LB", "value": 32.0}

    db.expire_all()
    request = db.execute(select(FbaInboundRequest)).scalar_one()
    assert request.status == "options_ready" and request.inbound_plan_id == "PLAN1"
    options = fba_plan.options_of(db, request)
    assert [o.option_number for o in options] == [1, 2]
    assert options[0].placement_fees["amount"] == 0.0, "cheapest first"
    assert options[1].shipment_count == 2
    assert options[0].shipments_preview[0]["fc"] == "RIC2"


def test_options_message_shows_every_destination_and_fee(db, ready, sent):
    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    db.commit()
    plan_responses()
    fba_plan.build_plan(db, request)
    text = fba_plan.options_message(db, request)
    assert "FBA SHIPMENT FB-1 — 480 units, 8 boxes (US)" in text
    assert "Option 1 — 1 shipment" in text and "RIC2 (Richmond, VA): 8 boxes, 480 units" in text
    assert "Option 2 — 2 shipments" in text and "ABE8 (Florence, NJ): 5 boxes, 300 units" in text
    assert "Placement fee: USD 0.00" in text and "USD 12.50" in text
    assert "SHIP FB-1 OPTION <number>" in text and "CANCEL FB-1" in text
    assert "Carrier prices come from Amazon after the warehouse choice is confirmed" in text


# ---------- what Amazon says each SKU needs ----------
def prep_detail(msku="SKZ-001", label="NONE_ONLY", prep="SELLER_ONLY", category="NONE",
                types=("ITEM_NO_PREP",)):
    return {"msku": msku, "labelOwnerConstraint": label, "prepOwnerConstraint": prep,
            "prepCategory": category, "prepTypes": list(types)}


def test_a_stickerless_sku_goes_up_as_label_owner_none(db, ready, sent):
    """Amazon rejected the whole plan when we assumed SELLER labels everything."""
    F.expect(r"/items/prepDetails", {"mskuPrepDetails": [prep_detail()]})
    parsed = intake.parse(MESSAGE)
    check = intake.precheck(db, parsed)
    assert check.ok and check.resolved[0]["label_owner"] == "NONE"
    assert check.resolved[0]["prep_owner"] == "SELLER"
    request = fba_plan.create_request(db, parsed=parsed, check=check)
    db.commit()
    plan_responses()
    fba_plan.build_plan(db, request)
    [item] = F.last_body(r"/inboundPlans$")["items"]
    assert item["labelOwner"] == "NONE" and item["prepOwner"] == "SELLER"
    packing = F.last_body(r"/packingInformation")["packageGroupings"][0]["boxes"][0]["items"][0]
    assert packing["labelOwner"] == "NONE"


def test_amazons_own_prep_answer_is_not_asked_for_again(db, ready, sent):
    ready.product_id = None            # we don't know the prep, but Amazon does
    db.commit()
    F.expect(r"/items/prepDetails", {"mskuPrepDetails": [prep_detail(category="NONE")]})
    check = intake.precheck(db, intake.parse(MESSAGE))
    assert check.ok, check.questions
    assert any("Amazon says SKZ-001 needs no prep" in n for n in check.notes)


def test_amazon_not_knowing_the_prep_still_asks_amit(db, ready, sent):
    ready.product_id = None
    db.commit()
    F.expect(r"/items/prepDetails", {"mskuPrepDetails": [prep_detail(category=None, types=())]})
    check = intake.precheck(db, intake.parse(MESSAGE))
    assert any("What prep does SKZ-001 need?" in q for q in check.questions)


def test_a_prep_details_failure_does_not_lose_the_shipment(db, ready, sent):
    from app.amazon.errors import AmazonError

    F.expect(r"/items/prepDetails", AmazonError("Amazon is having a moment"))
    check = intake.precheck(db, intake.parse(MESSAGE))
    assert check.ok and check.resolved[0]["label_owner"] is None    # falls back to the request default


def test_a_capacity_refusal_says_what_to_do_about_it(client, db, ready, sent):
    """Amazon's own text ends with "click the Storage Volume monitor", which is no use on WhatsApp."""
    from app.amazon.errors import AmazonError
    from app.jobs import queue

    F.expect(r"/items/prepDetails", {"mskuPrepDetails": [prep_detail()]})
    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    queue.enqueue(db, "fba_build_plan", {"request_ref": request.ref})
    F.expect(r"/inboundPlans$", AmazonError(
        "createInboundPlan: ERROR: This product exceeds the allowed volume for SORTABLE storage. Click on "
        "the Storage Volume monitor to view the maximum shipment volume. [FBA_INB_0132]"))
    db.commit()
    run_jobs(["fba_build_plan", "send_owner_message", "send_messages"])
    text = "\n".join(s.get("text", "") for s in sent)
    assert "exceeds the allowed volume" in text, "Amazon's own words are kept"
    assert "That's your FBA storage limit for sortable stock" in text
    assert "Capacity Monitor in Seller Central" in text
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "failed"


def test_the_agents_own_words_are_the_whole_reply(client, db, ready, sent):
    """The model's preamble used to sit on top and contradict it ("the Amazon agent isn't live yet")."""
    out = say(client, sent, "create an fba shipment: B0SKYZZ001, 100 units, 5 boxes, 20 per box",
              {"actions": [{"type": "fba_create_shipment",
                            "params": {"details": "B0SKYZZ001, 100 units, 5 boxes, 20 per box"}}],
               "reply_to_owner": "Logged it — though the Amazon agent isn't live yet."})
    assert "isn't live yet" not in out
    assert "What size are the boxes" in out


# ---------- the form (owner's design) ----------
def _a_date(days_ahead: int):
    """A real future date, and the words the owner would type for it."""
    from app.services.common import utcnow

    return (utcnow() + timedelta(days=days_ahead)).date()


SHIP_DATE = _a_date(7)
FILLED = f"""SKU or ASIN: B0SKYZZ001
Units: 480
Boxes: 8
Units per box: 60
Box size (L x W x H, inches): 18x14x12
Box weight (lb): 32
Ship date: {SHIP_DATE:%Y-%m-%d}
Delivery window: week of Oct 5
Prep: no prep"""


def test_a_vague_request_gets_the_form(client, db, sent, ready):
    out = say(client, sent, "new shipment")
    assert "New FBA shipment — copy this, fill it in" in out
    assert "Box weight (lb):" in out and "Delivery window:" in out
    assert db.execute(select(FbaInboundRequest)).first() is None

    # and so does a request with nothing to go on, however it is phrased
    out = say(client, sent, "i want to send stock to amazon",
              act("fba_create_shipment", details="i want to send stock to amazon"))
    assert "copy this, fill it in" in out


def test_the_filled_form_builds_the_shipment_in_one_go(client, db, sent, ready, owner):
    from datetime import date

    plan_responses()
    out = say(client, sent, FILLED, act("fba_create_shipment", details=FILLED))
    assert "checks passed" in out and "480 units in 8 boxes of 60" in out
    assert f"Ready to leave: {SHIP_DATE:%a %b %d}" in out
    db.expire_all()
    request = db.execute(select(FbaInboundRequest)).scalar_one()
    assert request.ready_to_ship_date == SHIP_DATE
    assert request.delivery_window_pref == "week of Oct 5"
    assert request.status in ("prechecks_passed", "building_plan", "options_ready")


def test_the_form_asks_for_what_was_left_blank(db, ready):
    from app.amazon import fba_form

    parsed, window, prep = fba_form.parse("SKU or ASIN: B0SKYZZ001\nUnits: 480\nBoxes: 8\nBox size:\n"
                                          "Box weight (lb):\nShip date: Monday")
    assert any("What size are the boxes" in q for q in parsed.questions)
    assert any("weigh" in q for q in parsed.questions)
    assert parsed.items[0].quantity == 480 and window == "" and prep == ""


def test_two_products_on_one_form(db, ready):
    from app.amazon import fba_form

    parsed, _, _ = fba_form.parse(
        "SKU or ASIN: B0SKYZZ001\nUnits: 480\nBoxes: 8\nUnits per box: 60\n"
        "Box size: 18x14x12\nBox weight: 32\n"
        "SKU or ASIN: B0VIBEZY10\nUnits: 100\nBoxes: 5\nUnits per box: 20\n"
        "Box size: 12x12x10\nBox weight: 18\nShip date: Monday")
    assert [i.asin for i in parsed.items] == ["B0SKYZZ001", "B0VIBEZY10"]
    assert [i.quantity for i in parsed.items] == [480, 100]
    assert parsed.questions == []


def test_the_window_preference_picks_the_matching_window(db, ready):
    from datetime import date

    from app.amazon import fba_confirm

    soon, later = _a_date(4), _a_date(17)
    options = [{"deliveryWindowOptionId": "DW1", "availabilityType": "AVAILABLE",
                "startDate": f"{soon:%Y-%m-%d}T00:00Z", "endDate": f"{soon:%Y-%m-%d}T23:59Z"},
               {"deliveryWindowOptionId": "DW2", "availabilityType": "AVAILABLE",
                "startDate": f"{later:%Y-%m-%d}T00:00Z", "endDate": f"{later:%Y-%m-%d}T23:59Z"}]
    F.expect(r"/deliveryWindowOptions$", {"operationId": "OP"})
    F.expect(r"/deliveryWindowOptions$", {"deliveryWindowOptions": options})
    F.expect(r"/deliveryWindowOptions/DW2/confirmation", {"operationId": "OPC"})
    from app.db.models import FbaShipment

    shipment = FbaShipment(request_id=None, amazon_shipment_id="SH1", destination_fc="RIC2")
    text = fba_confirm._confirm_delivery_window(fba_confirm._client(), "PLAN1", shipment,
                                                _a_date(1), preference=f"{later:%Y-%m-%d}")
    assert f"{later:%a %b %d}" in text
    assert shipment.delivery_window["start"] == f"{later:%Y-%m-%d}T00:00Z"


def test_dated_goods_carry_their_expiry_to_amazon(db, ready, sent):
    """Amazon refused 3,500 units of wound dressing: "Expiration date required" [FBA_INB_0180]."""
    from datetime import date

    parsed = intake.parse("B0SKYZZ001, 480 units, 8 boxes, 60 per box, 18x14x12, 32 lb, ready Monday, "
                          "expiry March 1 2028, lot AB1234")
    assert parsed.items[0].expiration_date == date(2028, 3, 1), "the year he wrote, not one we worked out"
    assert parsed.items[0].lot_code == "AB1234"
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    db.commit()
    plan_responses()
    fba_plan.build_plan(db, request)
    [item] = F.last_body(r"/inboundPlans$")["items"]
    assert item["expiration"] == "2028-03-01" and item["manufacturingLotCode"] == "AB1234"


def test_an_undated_product_sends_no_expiry(db, ready, sent):
    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    db.commit()
    plan_responses()
    fba_plan.build_plan(db, request)
    [item] = F.last_body(r"/inboundPlans$")["items"]
    assert "expiration" not in item and "manufacturingLotCode" not in item


def test_amazon_asking_for_an_expiry_says_what_to_send():
    from app.amazon.errors import inbound_guidance

    advice = inbound_guidance("createInboundPlan: ERROR: Expiration date required There's an input error "
                              "with the resource 'H3-NKLT-J0KI'. [FBA_INB_0180]")
    assert "expiry 2028-03-01" in advice


def test_dates_the_way_amit_writes_them(db):
    """Relative to today, because a month and day with no year rolls forward (D98).

    "sep 25" in October means next September, not six days ago — so the expected year is computed
    here rather than written in, which is what stopped this test rotting the moment the calendar
    passed the date it was written on.
    """
    from datetime import date

    from app.amazon.fba_intake import _ready_date
    from app.services.common import utcnow

    today = utcnow().date()
    soon = today + timedelta(days=20)        # always later this year or early next
    expected = date(today.year + (1 if (soon.month, soon.day) < (today.month, today.day) else 0),
                    soon.month, soon.day)
    month = soon.strftime("%b").lower()
    assert _ready_date(f"ready {month} {soon.day}") == expected
    assert _ready_date(f"ready {soon.strftime('%B').lower()} {soon.day}th") == expected
    assert _ready_date(f"{soon.day} {soon.strftime('%B').lower()}") == expected
    assert _ready_date(f"leaving {soon.month}/{soon.day}") == expected
    assert _ready_date(f"{soon.month}/{soon.day}/{expected.year}") == expected
    assert _ready_date(soon.isoformat()) == soon, "an explicit year is taken literally"

    # A month and day already behind us means next year, not the past.
    gone = today - timedelta(days=20)
    rolled = _ready_date(f"{gone.strftime('%b').lower()} {gone.day}")
    assert rolled > today and (rolled.month, rolled.day) == (gone.month, gone.day)
    assert _ready_date("no date in here") is None


def test_the_ready_date_can_be_changed(client, db, sent, ready, owner):
    from datetime import date

    from app.amazon.fba_intake import _ready_date

    plan_responses()
    say(client, sent, MESSAGE, act("fba_create_shipment", details=MESSAGE))
    request = db.execute(select(FbaInboundRequest)).scalar_one()
    assert request.ready_to_ship_date == _ready_date("monday"), "MESSAGE says \"ready Monday\""

    wanted = _a_date(9)
    out = say(client, sent, f"READY FB-1 {wanted:%Y-%m-%d}")
    assert f"FB-1 now leaves {wanted:%a %b %d}" in out
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().ready_to_ship_date == wanted

    assert "in the past" in say(client, sent, "READY FB-1 2020-01-01")
    assert "couldn't read a date" in say(client, sent, "READY FB-1 whenever")

    out = say(client, sent, "FB-1 leaves next Tuesday",
              act("fba_set_ready_date", request_ref="FB-1", date="next Tuesday"))
    assert "now leaves Tue " in out, "a weekday he names is resolved to the next one"


def test_changing_the_date_after_placement_re_asks_for_carriers(db, ready, owner):
    from app.amazon import fba_confirm
    from app.db.models import Job

    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    request.status, request.selected_placement_option_id = "confirmed", "PL1"
    db.commit()
    out = fba_plan.set_ready_date(db, request, f"{_a_date(9):%Y-%m-%d}")
    db.commit()
    assert "asking Amazon for them again" in out
    jobs = db.execute(select(Job).where(Job.type == "fba_transport_options")).scalars().all()
    assert len(jobs) == 1 and jobs[0].payload["request_ref"] == "FB-1"


def test_one_box_counts_as_a_box(db):
    """"boxes?" needs the "e", so a single box was never counted and the question looped."""
    for text, boxes in [("1 box", 1), ("8 boxes", 8), ("1 carton", 1), ("2 cases", 2), ("1 case", 1)]:
        parsed = intake.parse(f"send 20 units of B0SKYZZ001, {text}, 20 per box, 18x14x12, 32 lb each")
        assert parsed.items[0].boxes == boxes, text


def test_dimensions_written_any_way_amit_writes_them(db):
    for text in ("18x14x12", "18 × 14 × 12", "18*14*12", "18 by 14 by 12", "18X14X12 in"):
        parsed = intake.parse(f"send 480 units of B0SKYZZ001, 8 boxes, 60 per box, {text}, 32 lb each")
        assert parsed.items[0].dimensions == (18.0, 14.0, 12.0), text
        assert parsed.questions == [], text


def test_a_bare_prep_answer_applies_to_the_sku_that_was_asked_about(db, ready, owner, sent):
    """He is told to reply "no prep", so replying exactly that has to work."""
    ready.product_id = None          # a SKU we've never shipped before, so prep is unknown
    db.commit()
    out = fba_plan.start(db, user=owner, details=MESSAGE)
    assert "What prep does SKZ-001 need?" in out
    db.commit()

    plan_responses()
    out = fba_plan.answer(db, user=owner, answers="no prep")
    assert "Noted: SKZ-001 needs no prep." in out
    assert "What prep" not in out, "the question must not come back"
    listing = db.execute(select(AmazonListing).where(AmazonListing.sku == "SKZ-001")).scalar_one()
    assert db.get(Product, listing.product_id).prep_category == "NONE"
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "prechecks_passed"


def test_the_cheapest_split_is_pre_picked(db, ready, sent):
    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    db.commit()
    plan_responses(fees=(14.0, 0.0))        # the two-warehouse split is the free one
    fba_plan.build_plan(db, request)
    text = fba_plan.options_message(db, request)
    assert "Option 1 — 2 shipments   ⬅ my pick" in text      # stored cheapest first
    assert "✅ I've picked Option 1 — 2 warehouses, no placement fee (the cheapest Amazon offered)." in text
    assert "SHIP FB-1 OPTION 1 PARTNERED" in text and "SHIP FB-1 OPTION 1 OWN CARRIER" in text
    assert "SHIP FB-1 OPTION <number>   for a different set of warehouses" in text


def test_a_tie_on_fee_goes_to_the_most_warehouses(db, ready, sent):
    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    db.commit()
    plan_responses(fees=(0.0, 0.0))
    fba_plan.build_plan(db, request)
    assert fba_plan.preferred_option(db, request).shipment_count == 2


def test_the_rule_can_be_turned_off(db, ready, sent):
    from app.services.common import SYSTEM

    settings_store.set_value(db, fba_plan.PLACEMENT_PREFERENCE_KEY, "ask", SYSTEM)
    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    db.commit()
    plan_responses()
    fba_plan.build_plan(db, request)
    text = fba_plan.options_message(db, request)
    assert fba_plan.preferred_option(db, request) is None
    assert "my pick" not in text and "SHIP FB-1 OPTION <number>" in text


def test_amazon_refusing_the_plan_is_reported_not_retried_forever(db, ready, sent):
    from app.amazon.errors import AmazonError
    from app.jobs import queue

    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    queue.enqueue(db, "fba_build_plan", {"request_ref": request.ref})
    db.commit()
    F.expect(r"/inboundPlans$", AmazonError("createInboundPlan: MSKU is not eligible for inbound [Invalid]"))
    run_jobs(["fba_build_plan", "send_owner_message", "send_messages"])
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "failed"
    assert any("wouldn't build this shipment" in s.get("text", "") for s in sent)


def test_operation_failure_surfaces_amazons_own_words(db, ready):
    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    db.commit()
    F.expect(r"/inboundPlans$", {"inboundPlanId": "PLAN1", "operationId": "OPBAD"})
    F.operation("OPBAD", [{"operationStatus": "FAILED", "operationProblems": [
        {"code": "BOX_WEIGHT", "message": "Box 3 exceeds the weight limit", "severity": "ERROR"}]}])
    from app.amazon.errors import AmazonOperationFailed

    with pytest.raises(AmazonOperationFailed, match="Box 3 exceeds the weight limit"):
        fba_plan.build_plan(db, request)


# ---------- reminders and abandonment ----------
def test_reminder_and_abandon_jobs_are_scheduled(db, ready):
    from app.db.models import Job

    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    db.commit()
    plan_responses()
    fba_plan.build_plan(db, request)
    db.commit()
    types = [j.type for j in db.execute(select(Job)).scalars()]
    assert "fba_options_reminder" in types and "fba_abandon_check" in types


def test_abandoned_draft_is_cancelled_and_amit_is_told(db, ready, sent):
    from app.jobs import queue

    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    request.status = "options_ready"
    request.inbound_plan_id = "PLAN1"
    queue.enqueue(db, "fba_abandon_check", {"request_ref": request.ref})
    db.commit()
    F.expect(r"/cancellation", {"operationId": "OPC"})
    run_jobs(["fba_abandon_check", "send_owner_message", "send_messages"])
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "cancelled"
    assert any("no reply for 72 hours" in s.get("text", "") for s in sent)


def test_cancel_before_confirmation_is_free(db, ready):
    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    request.inbound_plan_id = "PLAN1"
    db.commit()
    F.expect(r"/cancellation", {"operationId": "OPC"})
    message = fba_plan.cancel_plan(db, request, reason="you cancelled it")
    assert "No fees were committed" in message
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "cancelled"


# ---------- the WhatsApp flow ----------
def test_whole_flow_from_a_whatsapp_message(client, db, sent, ready):
    plan_responses()
    out = say(client, sent, MESSAGE, act("fba_create_shipment", details=MESSAGE))
    assert "Shipment FB-1 — checks passed" in out
    assert "SKZ-001 (B0SKYZZ001): 480 units in 8 boxes of 60, 18×14×12 in, 32 lb each" in out
    assert "no fees are committed yet" in out
    assert "FBA SHIPMENT FB-1" in out and "Option 1" in out
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "options_ready"
    assert db.execute(select(FbaInboundItem)).scalar_one().quantity == 480
    assert db.execute(select(FbaBox)).scalar_one().box_count == 8


def test_questions_then_answers_complete_the_shipment(client, db, sent, ready):
    out = say(client, sent, "FBA shipment for B0SKYZZ001, 480 units in 8 boxes, 18x14x12",
              act("fba_create_shipment", details="FBA shipment for B0SKYZZ001, 480 units in 8 boxes, 18x14x12"))
    assert "Before I can build this shipment I need" in out and "weigh" in out
    assert "ready to leave" in out.lower(), "the ship date is asked for, never assumed"
    assert db.execute(select(FbaInboundRequest)).first() is None
    plan_responses()
    out = say(client, sent, "32 lb each, ready Monday",
              act("fba_provide_info", answers="32 lb each, ready Monday"))
    assert "checks passed" in out and "FBA SHIPMENT FB-1" in out
    assert "Ready to leave: Mon" in out and "boxes (small parcel)" in out


def test_shipments_list_and_detail_commands(client, db, sent, ready):
    plan_responses()
    say(client, sent, MESSAGE, act("fba_create_shipment", details=MESSAGE))
    out = say(client, sent, "SHIPMENTS")
    assert "FB-1 — 480 units, 1 SKU(s) — options ready" in out
    out = say(client, sent, "FB-1")
    assert "*FB-1* — waiting for you to pick an option" in out
    assert "8 boxes of 18×14×12 in, 32 lb — 60× SKZ-001" in out
    assert "cancelled" in say(client, sent, "CANCEL FB-1")


def test_case_pack_and_ship_from_are_saved(client, db, sent, ready):
    out = say(client, sent, "Skyzz mouth tape case is 60 units, 18x14x12, 32 lb",
              act("fba_set_case_pack", sku="SKZ-001", units_per_case=60, length_in=18, width_in=14,
                  height_in=12, weight_lb=32))
    assert "Saved the case pack for SKZ-001: 60 units" in out
    db.expire_all()
    product = db.execute(select(Product).where(Product.units_per_case == 60)).scalar_one()
    assert product.case_dimensions["length_in"] == 18 and float(product.case_weight_lb) == 32

    out = say(client, sent, "use 804-555-0100 for shipping",
              act("fba_set_ship_from", phone="+18045550100"))
    assert "Ship-from address updated" in out and "+18045550100" in out
