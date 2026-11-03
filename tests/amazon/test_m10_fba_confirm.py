"""P3 M10: the two approvals, labels, tracking, receiving and cancelling a confirmed shipment."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.amazon import fba_confirm, fba_intake as intake, fba_plan
from app.amazon import listings as listings_svc
from app.amazon.fake import FakeAmazonClient as F
from app.db.models import AmazonAlert, AmazonListing, Approval, FbaInboundRequest, FbaShipment, Product, ShipFromAddress
from app.llm.fake import FakeLLMProvider
from app.services import users
from tests.amazon.helpers import connect, expect_listings, listing_item
from tests.amazon.test_m9_fba_plan import MESSAGE, plan_responses
from tests.conftest import run_jobs
from tests.helpers import post, text_payload

PIN = "4321"


@pytest.fixture
def client(db, owner):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def ready(db, owner):
    from app.services.common import utcnow

    owner.last_inbound_at = utcnow()
    owner.approval_pin_hash = users.hash_pin(PIN)
    db.commit()
    connect(db, write_mode="live")
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


@pytest.fixture
def request_with_options(db, ready):
    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed), instructions=MESSAGE)
    db.commit()
    plan_responses()
    fba_plan.build_plan(db, request)
    db.commit()
    return request


def say(client, sent, text, *llm):
    FakeLLMProvider.script(*llm)
    before = len(sent)
    assert post(client, text_payload(text)).status_code == 200
    run_jobs(limit=200)
    return "\n---\n".join(s.get("text", f"[{s['kind']}]") for s in sent[before:])


def approval_of(db):
    db.expire_all()
    return db.execute(select(Approval).order_by(Approval.created_at.desc())).scalars().first()


def placement_confirm_responses(*, fee=0.0):
    """listPlacementOptions (freshness), confirmation, then shipment details."""
    F.expect(r"/placementOptions$", {"placementOptions": [
        {"placementOptionId": "PL1", "status": "OFFERED",
         "fees": [{"value": {"amount": fee, "code": "USD"}}], "shipmentIds": ["SH1"]}]})
    F.expect(r"/placementOptions/PL1/confirmation", {"operationId": "OPC"})
    F.expect(r"/shipments/SH1", {"shipmentConfirmationId": "FBA15ABCDEF", "status": "WORKING",
                                 "summary": {"boxCount": 8, "unitCount": 480}})


def transport_responses(cost=118.4, solution="AMAZON_PARTNERED_CARRIER"):
    F.expect(r"/transportationOptions$", {"operationId": "OPT"})
    F.expect(r"/transportationOptions$", {"transportationOptions": [
        {"transportationOptionId": "TO1", "shipmentId": "SH1", "shippingSolution": solution,
         "shippingMode": "GROUND_SMALL_PARCEL", "carrier": {"name": "UPS"},
         "quote": {"cost": {"amount": cost, "code": "USD"}}}]})


def label_responses():
    F.expect(r"/labels", {"labels": [{"type": "box", "downloadURL": "https://labels.test/box.pdf"}]})


@pytest.fixture
def no_downloads(monkeypatch):
    monkeypatch.setattr(fba_confirm, "_download", lambda url: b"%PDF-1.4 fake label")


# ---------- picking an option ----------
def test_option_choice_asks_which_carrier_when_the_setting_says_ask(db, request_with_options, owner):
    out = fba_confirm.choose_option(db, request_with_options, 1, carrier_choice=None, user=owner)
    assert "Which carrier for FB-1?" in out and "PARTNERED" in out and "OWN CARRIER" in out
    assert db.execute(select(Approval)).first() is None


def test_a_bare_own_carrier_answers_the_question_that_was_asked(client, db, sent, request_with_options, owner):
    """The owner replied "own carrier" three ways and got "I don't have a shipment waiting on answers"."""
    out = fba_confirm.choose_option(db, request_with_options, 1, carrier_choice=None, user=owner)
    db.commit()
    assert "SHIP FB-1 OPTION 1 OWN CARRIER" in out and 'reply "partnered" or "own carrier"' in out

    out = say(client, sent, "own carrier")          # no command prefix, no model needed
    assert "APPROVE" in out and "step 1 of 2: warehouses" in out
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().selected_transport_choice == "own_carrier"


def test_the_model_route_takes_a_bare_carrier_answer_too(client, db, sent, request_with_options, owner):
    fba_confirm.choose_option(db, request_with_options, 1, carrier_choice=None, user=owner)
    db.commit()
    out = say(client, sent, "im doing it myself with my own guy",
              {"actions": [{"type": "fba_provide_info", "params": {"answers": "own carrier"}}],
               "reply_to_owner": ""})
    assert "APPROVE" in out
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().selected_transport_choice == "own_carrier"


def test_own_carrier_goes_straight_to_the_one_approval(client, db, sent, request_with_options, owner):
    """Amazon quotes nothing on own freight, so there's no price list to read and nothing to retype."""
    from app.services import settings_store
    from app.services.common import SYSTEM

    settings_store.set_value(db, fba_confirm.DEFAULT_CARRIER_KEY, "Other", SYSTEM)
    db.commit()
    confirmed_own_carrier(db, request_with_options, owner, carriers=("UPS", "Other"))
    text = "\n".join(s.get("text", "") for s in sent)
    assert "carriers available" not in text, "no menu when his usual carrier is on the list"
    assert "step 2 of 2" in text and "Other" in text and "APPROVE" in text
    assert "Other GROUND_SMALL_PARCEL — you pay them directly" in text
    assert "Carrier total" not in text, "Amazon quoted nothing, so there is no carrier total to show"
    db.expire_all()
    a = db.execute(select(Approval).where(Approval.action_type == "fba_confirm_transport")).scalar_one()
    assert len(a.action_payload["transportation_option_ids"]) == 1


def test_short_replies_answer_the_options_question(client, db, sent, request_with_options, owner):
    out = say(client, sent, "option 1 own carrier")
    assert "step 1 of 2: warehouses" in out and "APPROVE" in out
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().selected_transport_choice == "own_carrier"


def test_a_forgotten_question_falls_back_to_the_open_shipment(client, db, sent, request_with_options, owner):
    """After a deploy the remembered question is gone; the shipment waiting for one is not."""
    from app.services import settings_store
    from app.services.common import SYSTEM

    fba_confirm.choose_option(db, request_with_options, 1, carrier_choice=None, user=owner)
    settings_store.set_value(db, fba_confirm.PENDING_CARRIER_KEY, None, SYSTEM)   # as a restart would
    db.commit()
    out = say(client, sent, "own carrier")
    assert "Which option for FB-1?" in out and "I'll use your own carrier" in out
    assert "Option 1: 1 warehouse" in out and "Option 2: 2 warehouses" in out
    assert "No shipment is waiting" not in out


def test_a_carrier_answer_with_nothing_waiting_says_so(client, db, sent, ready):
    assert "No shipment is waiting on a carrier choice" in say(client, sent, "own carrier")


def test_a_denied_operation_does_not_take_the_agent_down(client, db, sent, request_with_options, owner):
    """A 403 on labels paused listing sync, alerts and everything else. It shouldn't."""
    from app.amazon import accounts
    from app.amazon.errors import AmazonAuthError

    a = at_the_carrier_approval(db, request_with_options, owner, client, sent)
    F.expect(r"/transportationOptions/confirmation", {"operationId": "OPC"})
    F.expect(r"/labels", AmazonAuthError("getLabels: Access to requested resource is denied. [Unauthorized] "
                                         "— the app may be missing a role.", missing_role=True))
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "won't let this app download the labels" in out
    assert "Print them in Seller Central" in out and "FBA15ABCDEF" in out
    db.expire_all()
    assert accounts.get_primary(db).status == "active", "the connection is fine; one operation is not"
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "confirmed"


def test_a_real_auth_failure_still_stops_everything(db, ready, sent):
    from app.amazon import accounts
    from app.amazon.errors import AmazonAuthError
    from app.jobs.handlers.amazon import _auth_failed

    _auth_failed(db, AmazonAuthError("Login with Amazon refused the refresh token"))
    db.expire_all()
    assert accounts.get_primary(db).status == "auth_error"


def test_cancelling_a_shipment_with_fees_behind_it_needs_the_pin(db, request_with_options, owner):
    """Placement confirmed but not yet "confirmed": the fee is real, so this can't be a one-liner."""
    fba_confirm.choose_option(db, request_with_options, 1, carrier_choice="partnered", user=owner)
    db.commit()
    a = approval_of(db)
    placement_confirm_responses()
    transport_responses()
    from app.services import approvals

    approvals.approve(db, owner, a.ref, a.confirmation_code)
    db.commit()
    run_jobs(limit=200)
    db.expire_all()
    request = fba_plan.get_by_ref(db, "FB-1")
    request.confirmed_fees = {"placement": 37.68, "currency": "USD"}    # as a paid placement leaves it
    db.commit()
    assert request.status == "confirming" and fba_plan.committed_fees(request) == 37.68
    with pytest.raises(Exception, match="needs your PIN"):
        fba_plan.cancel_plan(db, request, reason="changed my mind")


def test_cancelling_a_free_shipment_withdraws_its_approval(db, request_with_options, owner):
    from app.db.models import Approval

    fba_confirm.choose_option(db, request_with_options, 1, carrier_choice="partnered", user=owner)
    db.commit()
    a = approval_of(db)
    assert a.status == "pending"
    out = fba_plan.cancel_plan(db, fba_plan.get_by_ref(db, "FB-1"), reason="you cancelled it")
    db.commit()
    assert "cancelled" in out and "No fees were committed" in out
    db.expire_all()
    assert db.get(Approval, a.id).status == "rejected", "it must not stay approvable"


def window_options(*windows, availability="AVAILABLE"):
    return {"deliveryWindowOptions": [
        {"deliveryWindowOptionId": f"DW{i}", "availabilityType": availability,
         "startDate": f"{start}T00:00Z", "endDate": f"{end}T23:59Z"}
        for i, (start, end) in enumerate(windows, start=1)]}


def at_the_carrier_approval(db, request, owner, client, sent):
    """Placement approved and quoted, sitting on the second approval."""
    from app.services import approvals

    fba_confirm.choose_option(db, request, 1, carrier_choice="partnered", user=owner)
    db.commit()
    a = approval_of(db)
    placement_confirm_responses()
    transport_responses()
    approvals.approve(db, owner, a.ref, a.confirmation_code)
    db.commit()
    run_jobs(limit=200)
    db.expire_all()
    fba_confirm.choose_carrier(db, fba_plan.get_by_ref(db, request.ref), user=owner)
    db.commit()
    return approval_of(db)


def test_a_delivery_window_is_agreed_when_amazon_demands_one(client, db, sent, request_with_options, owner):
    """Amazon refused the carrier: "requires confirmed delivery windows for shipment …"."""
    from app.amazon.errors import AmazonError
    from app.db.models import FbaShipment

    a = at_the_carrier_approval(db, request_with_options, owner, client, sent)
    F.expect(r"/transportationOptions/confirmation", AmazonError(
        "confirmTransportationOptions: ERROR: … requires confirmed delivery windows for shipment SH1"))
    F.expect(r"/deliveryWindowOptions$", {"operationId": "OPDW"})
    from datetime import timedelta

    from app.services.common import utcnow

    today = utcnow().date()
    far, wanted, too_early = today + timedelta(days=30), today + timedelta(days=4), today - timedelta(days=1)
    F.expect(r"/deliveryWindowOptions$", window_options(
        (f"{far:%Y-%m-%d}", f"{far + timedelta(days=6):%Y-%m-%d}"),
        (f"{wanted:%Y-%m-%d}", f"{wanted + timedelta(days=6):%Y-%m-%d}"),
        (f"{too_early:%Y-%m-%d}", f"{today:%Y-%m-%d}")))
    F.expect(r"/deliveryWindowOptions/DW2/confirmation", {"operationId": "OPDWC"})
    F.expect(r"/transportationOptions/confirmation", {"operationId": "OPC2"})
    F.expect(r"/labels", {"labels": []})

    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "is confirmed" in out
    assert (f"Delivery window agreed with Amazon — RIC2: {wanted:%a %b %d} – "
            f"{wanted + timedelta(days=6):%a %b %d}") in out, out
    db.expire_all()
    shipment = db.execute(select(FbaShipment)).scalar_one()
    assert shipment.delivery_window == {"start": f"{wanted:%Y-%m-%d}T00:00Z",
                                        "end": f"{wanted + timedelta(days=6):%Y-%m-%d}T23:59Z"}
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "confirmed"


def test_a_carrier_refusal_that_is_not_about_windows_is_reported_as_is(client, db, sent,
                                                                      request_with_options, owner):
    from app.amazon.errors import AmazonError

    a = at_the_carrier_approval(db, request_with_options, owner, client, sent)
    F.expect(r"/transportationOptions/confirmation", AmazonError("ERROR: that option has expired"))
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "Amazon couldn't book the carrier" in out and "that option has expired" in out
    assert not F.calls_to("/deliveryWindowOptions"), "windows are only touched when Amazon asks for them"


# ---------- own carrier: Amazon lists everyone, the owner picks one ----------
def own_carrier_quotes(shipment_ids=("SH1",), carriers=("UPS", "FEDEX GROUND (CORPORATE)", "ONTRAC")):
    return [{"transportationOptionId": f"TO-{sid}-{i}", "shipmentId": sid,
             "shippingSolution": "USE_YOUR_OWN_CARRIER", "shippingMode": "GROUND_SMALL_PARCEL",
             "carrier": {"name": name}, "quote": {"cost": {"amount": 0.0, "code": "USD"}}}
            for sid in shipment_ids for i, name in enumerate(carriers)]


def confirmed_own_carrier(db, request, owner, carriers=("UPS", "FEDEX GROUND (CORPORATE)", "ONTRAC")):
    """Get to the point the owner reached live: placement confirmed, a pile of own-carrier options stored."""
    fba_confirm.choose_option(db, request, 1, carrier_choice="own carrier", user=owner)
    db.commit()
    a = approval_of(db)
    placement_confirm_responses()
    F.expect(r"/transportationOptions$", {"operationId": "OPT"})
    F.expect(r"/transportationOptions$", {"transportationOptions": own_carrier_quotes(carriers=carriers)})
    from app.services import approvals

    approvals.approve(db, owner, a.ref, a.confirmation_code)
    db.commit()
    run_jobs(limit=200)
    db.expire_all()
    return fba_plan.get_by_ref(db, request.ref)


def test_own_carrier_asks_which_one_instead_of_booking_them_all(db, request_with_options, owner, sent):
    request = confirmed_own_carrier(db, request_with_options, owner)
    text = "\n".join(s.get("text", "") for s in sent)
    assert "1 carriers available" not in text and "3 carriers available" in text
    assert "you've booked" in text and "SHIP FB-1 CARRIER <name>" in text
    assert "USD 0.00" not in text, "Amazon isn't quoting zero; the owner pays the carrier"

    with pytest.raises(Exception, match="carriers available"):     # no name, no booking
        fba_confirm.choose_carrier(db, request, user=owner)
    assert db.execute(select(Approval).where(Approval.action_type == "fba_confirm_transport")).first() is None


def test_naming_the_carrier_books_exactly_one_option_per_shipment(db, request_with_options, owner, sent):
    request = confirmed_own_carrier(db, request_with_options, owner)
    message = fba_confirm.choose_carrier(db, request, user=owner, carrier="ontrac")
    db.commit()
    a = approval_of(db)
    assert a.action_type == "fba_confirm_transport"
    assert len(a.action_payload["transportation_option_ids"]) == 1, "one per shipment, not the whole list"
    assert a.action_payload["transportation_option_ids"][0]["transportationOptionId"] == "TO-SH1-2"
    assert "ONTRAC" in a.summary and "you pay them directly" in a.summary
    assert "book and pay the carrier yourself" in a.summary
    assert f"APPROVE {a.ref}" in message


def test_the_usual_carrier_is_picked_without_asking(db, request_with_options, owner, sent):
    from app.services import settings_store
    from app.services.common import SYSTEM

    settings_store.set_value(db, fba_confirm.DEFAULT_CARRIER_KEY, "Other", SYSTEM)
    db.commit()
    request = confirmed_own_carrier(db, request_with_options, owner,
                                    carriers=("UPS", "Other", "BROTHERS TRANSPORT"))
    message = fba_confirm.choose_carrier(db, request, user=owner)      # no name given
    a = approval_of(db)
    assert len(a.action_payload["transportation_option_ids"]) == 1
    assert a.action_payload["transportation_option_ids"][0]["transportationOptionId"] == "TO-SH1-1"
    assert "Other" in a.summary and "your usual choice" in a.summary
    assert "BROTHERS" not in a.summary, "an exact name must not match a longer one"
    assert f"APPROVE {a.ref}" in message


def test_the_usual_carrier_steps_aside_when_amazon_does_not_offer_it(db, request_with_options, owner, sent):
    from app.services import settings_store
    from app.services.common import SYSTEM

    settings_store.set_value(db, fba_confirm.DEFAULT_CARRIER_KEY, "Other", SYSTEM)
    db.commit()
    request = confirmed_own_carrier(db, request_with_options, owner, carriers=("UPS", "ONTRAC", "FEDEX"))
    with pytest.raises(Exception, match="carriers available"):
        fba_confirm.choose_carrier(db, request, user=owner)


def test_a_carrier_amazon_does_not_offer_is_refused(db, request_with_options, owner, sent):
    request = confirmed_own_carrier(db, request_with_options, owner)
    with pytest.raises(Exception, match="doesn't offer"):
        fba_confirm.choose_carrier(db, request, user=owner, carrier="Royal Mail")


def test_the_carrier_read_names_the_placement_option(db, request_with_options, owner):
    """Amazon refuses listTransportationOptions outright without it — it cost us a live shipment."""
    fba_confirm.choose_option(db, request_with_options, 1, carrier_choice="partnered", user=owner)
    db.commit()
    a = approval_of(db)
    placement_confirm_responses()
    transport_responses()
    from app.services import approvals

    approvals.approve(db, owner, a.ref, a.confirmation_code)
    db.commit()
    run_jobs(limit=200)
    reads = [c for c in F.calls_to(r"/transportationOptions$") if c["method"] == "GET"]
    assert reads, "the options are never read"
    assert reads[-1]["params"].get("placementOptionId") == "PL1"
    assert reads[-1]["params"]["pageSize"] <= 20, "Amazon rejects anything larger"


def test_placement_approval_shows_destinations_and_fees(db, request_with_options, owner):
    message = fba_confirm.choose_option(db, request_with_options, 1, carrier_choice="partnered", user=owner)
    a = approval_of(db)
    assert "step 1 of 2: warehouses" in a.summary and "RIC2 (Richmond, VA): 8 boxes, 480 units" in a.summary
    assert "Placement fee: USD 0.00" in a.summary
    assert "Carrier costs are quoted next" in a.summary
    assert a.risk_level == "high" and f"APPROVE {a.ref}" in message
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "pending_approval"


def test_expensive_placement_needs_the_pin(db, ready, owner):
    parsed = intake.parse(MESSAGE)
    request = fba_plan.create_request(db, parsed=parsed, check=intake.precheck(db, parsed))
    db.commit()
    plan_responses(fees=(300.0, 450.0))
    fba_plan.build_plan(db, request)
    db.commit()
    fba_confirm.choose_option(db, request, 1, carrier_choice="partnered", user=owner)
    assert approval_of(db).risk_level == "critical"


def test_unknown_option_number_lists_the_real_ones(db, request_with_options, owner):
    with pytest.raises(intake.ShipmentError, match="has options 1, 2"):
        fba_confirm.choose_option(db, request_with_options, 9, carrier_choice="partnered", user=owner)


# ---------- executing the placement ----------
def test_ok_never_confirms_only_the_approval_code_does(client, db, sent, request_with_options, owner):
    fba_confirm.choose_option(db, request_with_options, 1, carrier_choice="partnered", user=owner)
    db.commit()
    for word in ("ok", "yes", "go ahead", "confirm"):
        assert "Nothing was approved" in say(client, sent, word)
    assert not [c for c in F.calls if "placementOptions/PL1/confirmation" in c["path"]]


def test_full_two_step_confirmation_with_labels(client, db, sent, request_with_options, owner, no_downloads):
    fba_confirm.choose_option(db, request_with_options, 1, carrier_choice="partnered", user=owner)
    db.commit()
    a = approval_of(db)
    placement_confirm_responses()
    transport_responses()
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "warehouses confirmed, 1 shipment(s) created" in out
    assert "step 2 of 2: carrier prices" in out and "UPS" in out and "USD 118.40" in out
    assert "Running total: USD 118.40" in out
    db.expire_all()
    shipment = db.execute(select(FbaShipment)).scalar_one()
    assert shipment.shipment_confirmation_id == "FBA15ABCDEF"

    out = say(client, sent, "SHIP FB-1 CARRIER")
    second = approval_of(db)
    assert second.action_type == "fba_confirm_transport"
    F.expect(r"/transportationOptions/confirmation", {"operationId": "OPTC"})
    label_responses()
    out = say(client, sent, f"APPROVE {second.ref} {second.confirmation_code}")
    assert "FB-1 is confirmed" in out and "Total committed: USD 118.40" in out
    assert "🏷️ FB-1 labels" in out and "Stick one box label on each box" in out
    assert any(s.get("kind") == "document" for s in sent), "labels are sent as a document"
    db.expire_all()
    request = db.execute(select(FbaInboundRequest)).scalar_one()
    assert request.status == "confirmed" and request.confirmed_fees["carrier"] == 118.4
    assert db.execute(select(FbaShipment)).scalar_one().box_label_file_id is not None


def test_expired_option_regenerates_instead_of_confirming(client, db, sent, request_with_options, owner):
    fba_confirm.choose_option(db, request_with_options, 1, carrier_choice="partnered", user=owner)
    db.commit()
    a = approval_of(db)
    F.expect(r"/placementOptions$", {"placementOptions": [{"placementOptionId": "PL1", "status": "EXPIRED",
                                                           "fees": [], "shipmentIds": []}]})
    plan_responses()
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "expired before it was confirmed" in out
    assert not [c for c in F.calls if "placementOptions/PL1/confirmation" in c["path"]]
    assert "FBA SHIPMENT FB-1" in out, "fresh options are sent"


def test_changed_fee_stops_the_confirmation(client, db, sent, request_with_options, owner):
    fba_confirm.choose_option(db, request_with_options, 1, carrier_choice="partnered", user=owner)
    db.commit()
    a = approval_of(db)
    F.expect(r"/placementOptions$", {"placementOptions": [
        {"placementOptionId": "PL1", "status": "OFFERED",
         "fees": [{"value": {"amount": 95.0, "code": "USD"}}], "shipmentIds": ["SH1"]}]})
    plan_responses()
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "changed the placement fee to 95.00" in out
    assert not [c for c in F.calls if "placementOptions/PL1/confirmation" in c["path"]]


def test_paused_amazon_blocks_confirmation(client, db, sent, request_with_options, owner):
    from app.services import settings_store
    from app.services.common import SYSTEM

    fba_confirm.choose_option(db, request_with_options, 1, carrier_choice="partnered", user=owner)
    settings_store.set_value(db, "amazon_paused", True, SYSTEM)
    db.commit()
    a = approval_of(db)
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "Amazon is paused" in out
    assert not [c for c in F.calls if "placementOptions/PL1/confirmation" in c["path"]]


# ---------- carrier step ----------
def test_no_partnered_quote_offers_own_carrier(db, request_with_options, owner):
    request = request_with_options
    request.selected_placement_option_id = "PL1"
    request.selected_transport_choice = "partnered"
    request.confirmed_fees = {"placement": 0.0, "currency": "USD"}
    db.add(FbaShipment(request_id=request.id, amazon_shipment_id="SH1", destination_fc="RIC2",
                       units_expected=480))
    db.commit()
    transport_responses(solution="USE_YOUR_OWN")
    message = fba_confirm.fetch_transport_options(db, request)
    assert "didn't offer a partnered carrier option" in message and "OWN CARRIER" in message


def test_carrier_approval_shows_the_running_total(db, request_with_options, owner):
    request = request_with_options
    request.selected_placement_option_id = "PL1"
    request.selected_transport_choice = "partnered"
    request.confirmed_fees = {"placement": 12.5, "currency": "USD"}
    db.add(FbaShipment(request_id=request.id, amazon_shipment_id="SH1", destination_fc="RIC2",
                       units_expected=480))
    db.commit()
    transport_responses(cost=300.0)
    fba_confirm.fetch_transport_options(db, request)
    fba_confirm.choose_carrier(db, request, user=owner)
    a = approval_of(db)
    assert "Carrier total: USD 300.00" in a.summary and "Running total: USD 312.50" in a.summary
    assert a.risk_level == "critical", "over the PIN threshold"


# ---------- tracking ----------
def test_own_carrier_tracking_needs_approval_then_reaches_amazon(client, db, sent, request_with_options, owner):
    request = request_with_options
    request.status = "confirmed"
    request.selected_transport_choice = "own_carrier"
    db.add(FbaShipment(request_id=request.id, amazon_shipment_id="SH1", destination_fc="RIC2",
                       units_expected=480))
    db.commit()
    out = say(client, sent, "TRACKING FB-1 1Z999AA10123456784 1Z999AA10123456785")
    a = approval_of(db)
    assert a.action_type == "fba_update_tracking" and a.risk_level == "medium"
    assert "1Z999AA10123456784" in a.summary
    F.expect(r"/trackingDetails", {"operationId": "OPTR"})
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "2 tracking number(s) sent to Amazon" in out
    body = F.last_body("/trackingDetails")
    assert body["trackingDetails"]["spdTrackingDetail"]["spdTrackingItems"][0]["trackingId"].startswith("1Z999")
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "in_transit"


def test_tracking_before_confirmation_is_refused(db, request_with_options, owner):
    with pytest.raises(intake.ShipmentError, match="belong on a confirmed shipment"):
        fba_confirm.request_tracking(db, request_with_options, ["1Z999"], user=owner)


# ---------- receiving ----------
def test_receiving_shortage_alerts_with_drafted_case_text(db, sent, request_with_options, owner):
    request = request_with_options
    request.status = "receiving"
    db.add(FbaShipment(request_id=request.id, amazon_shipment_id="SH1",
                       shipment_confirmation_id="FBA15ABCDEF", destination_fc="RIC2", units_expected=480,
                       status="RECEIVING"))
    db.commit()
    F.expect(r"/shipments/SH1", {"status": "CLOSED", "summary": {"receivedQuantity": 468}})
    fba_confirm.sync_status(db, request)
    db.commit()
    run_jobs(["send_owner_message", "send_messages"])
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "closed_with_discrepancy"
    alert = db.execute(select(AmazonAlert)).scalar_one()
    assert alert.type == "receiving_discrepancy" and "12 units short" in alert.details["message"]
    text = "\n".join(s.get("text", "") for s in sent)
    assert "Amazon received 468 of 480 units — 12 short" in text
    assert "Draft case text for Seller Support" in text and "FBA15ABCDEF" in text


def test_full_receipt_closes_cleanly(db, sent, request_with_options):
    request = request_with_options
    db.add(FbaShipment(request_id=request.id, amazon_shipment_id="SH1", destination_fc="RIC2",
                       units_expected=480, status="RECEIVING"))
    db.commit()
    F.expect(r"/shipments/SH1", {"status": "CLOSED", "summary": {"receivedQuantity": 480}})
    fba_confirm.sync_status(db, request)
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "closed"
    assert db.execute(select(AmazonAlert)).first() is None


# ---------- cancelling a confirmed shipment ----------
def test_cancelling_after_confirmation_needs_pin_and_checks_state(client, db, sent, request_with_options, owner):
    request = request_with_options
    request.status = "confirmed"
    request.confirmed_fees = {"placement": 12.5, "carrier": 118.4, "currency": "USD"}
    db.add(FbaShipment(request_id=request.id, amazon_shipment_id="SH1", destination_fc="RIC2",
                       units_expected=480, status="WORKING"))
    db.commit()
    out = say(client, sent, "CANCEL FB-1")
    a = approval_of(db)
    assert a.action_type == "fba_cancel_confirmed_plan" and a.risk_level == "critical"
    assert "Amazon may keep placement or carrier fees" in a.summary
    F.expect(r"/cancellation", {"operationId": "OPX"})
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code} {PIN} FB-1")
    assert "cancelled with Amazon" in out
    db.expire_all()
    assert db.execute(select(FbaInboundRequest)).scalar_one().status == "cancelled"


def test_a_shipment_already_in_transit_is_not_cancelled(client, db, sent, request_with_options, owner):
    request = request_with_options
    request.status = "in_transit"
    db.add(FbaShipment(request_id=request.id, amazon_shipment_id="SH1", destination_fc="RIC2",
                       units_expected=480, status="IN_TRANSIT"))
    db.commit()
    say(client, sent, "CANCEL FB-1")
    a = approval_of(db)
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code} {PIN} FB-1")
    assert "already left" in out and "Nothing was changed" in out
    assert not [c for c in F.calls if "cancellation" in c["path"]]


# ---------- labels on demand ----------
def test_labels_command_refetches(client, db, sent, request_with_options, no_downloads):
    request = request_with_options
    request.status = "confirmed"
    db.add(FbaShipment(request_id=request.id, amazon_shipment_id="SH1", destination_fc="RIC2",
                       units_expected=480))
    db.commit()
    label_responses()
    out = say(client, sent, "LABELS FB-1")
    assert "Fetching the FB-1 labels again" in out
    assert any(s.get("kind") == "document" for s in sent)


def test_labels_before_confirmation_say_so(client, db, sent, request_with_options):
    assert "isn't confirmed yet" in say(client, sent, "LABELS FB-1")
