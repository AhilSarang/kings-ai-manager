"""Building the inbound plan in Amazon and showing the owner the placement options (brief 12.3–12.4).

Everything here is Tier 1: a draft plan costs nothing, and abandoned drafts are cancelled again.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon import accounts, api
from app.amazon import fba_intake as intake
from app.amazon import marketplaces as mkt
from app.amazon.errors import AmazonError, AmazonOperationFailed, AmazonRetryable
from app.db.models import FbaBox, FbaInboundItem, FbaInboundRequest, FbaPlacementOption, Product, ShipFromAddress
from app.jobs import queue
from app.services import audit, settings_store
from app.services.common import SYSTEM, Actor, NotFound, utcnow
from app.services.formatting import fmt_dt

log = logging.getLogger(__name__)
AGENT = Actor.agent("amazon")
BASE = "/inbound/fba/2024-03-20"
ABANDON_HOURS = 72
REMIND_BEFORE_EXPIRY_MINUTES = 30

PREP_CATEGORIES = {
    "no prep": "NONE", "none": "NONE", "poly bag": "POLY_BAGGING", "polybag": "POLY_BAGGING",
    "bubble wrap": "BUBBLE_WRAPPING", "labeling only": "LABELING", "labelling only": "LABELING",
    "taping": "TAPING", "opaque bag": "OPAQUE_BAGGING", "black shrink wrap": "BLACK_SHRINKWRAP",
    "hanger removal": "HANG_GARMENT", "set creation": "SET_CREATION", "fragile": "FRAGILE",
}


def normalize_ref(ref: str) -> str:
    ref = (ref or "").strip().upper().replace(" ", "")
    return ref if ref.startswith("FB-") else f"FB-{ref.lstrip('FB-')}"


def get_by_ref(session: Session, ref: str) -> FbaInboundRequest:
    row = session.execute(select(FbaInboundRequest)
                          .where(FbaInboundRequest.ref == normalize_ref(ref))).scalar_one_or_none()
    if row is None:
        raise NotFound(f"I can't find shipment {normalize_ref(ref)}.")
    return row


def prep_code(words: str) -> str | None:
    text = (words or "").strip().lower()
    for phrase, code in PREP_CATEGORIES.items():
        if phrase in text:
            return code
    return None


def create_request(session: Session, *, parsed: intake.ParsedShipment, check: intake.Check,
                   marketplace_code: str = "US", instructions: str = "",
                   source_message_id=None) -> FbaInboundRequest:
    """Store what the owner asked for. Nothing has been sent to Amazon at this point."""
    mp = mkt.by_code(marketplace_code)
    address = session.execute(select(ShipFromAddress).where(ShipFromAddress.is_default.is_(True))).scalar_one()
    mode = parsed.shipping_mode or settings_store.get(session, "fba_default_shipping_mode", "small_parcel")
    request = FbaInboundRequest(
        marketplace_id=mp.marketplace_id, ship_from_address_id=address.id, owner_instructions=instructions,
        source_message_id=source_message_id, shipping_mode=mode,
        ready_to_ship_date=parsed.ready_date or intake.next_business_day(),
        label_owner=settings_store.get(session, "fba_default_label_owner", "SELLER"),
        prep_owner="SELLER", status="prechecks_passed")
    session.add(request)
    session.flush()
    for row in check.resolved:
        listing, item = row["listing"], row["item"]
        session.add(FbaInboundItem(request_id=request.id, listing_id=listing.id, asin=listing.asin,
                                   msku=listing.sku, fnsku=listing.fnsku, quantity=item.quantity,
                                   prep_category=row.get("prep_category"),
                                   label_owner=row.get("label_owner"), prep_owner=row.get("prep_owner"),
                                   expiration_date=item.expiration_date,
                                   manufacturing_lot_code=item.lot_code))
        session.add(FbaBox(request_id=request.id, box_group=len(check.resolved), box_count=item.boxes,
                           length_in=item.dimensions[0], width_in=item.dimensions[1],
                           height_in=item.dimensions[2], weight_lb=item.weight_lb,
                           contents=[{"msku": listing.sku, "quantity_per_box": row["units_per_box"]}]))
    session.flush()
    return request


def items_of(session: Session, request: FbaInboundRequest) -> list[FbaInboundItem]:
    return list(session.execute(select(FbaInboundItem).where(FbaInboundItem.request_id == request.id)).scalars())


def boxes_of(session: Session, request: FbaInboundRequest) -> list[FbaBox]:
    return list(session.execute(select(FbaBox).where(FbaBox.request_id == request.id)
                                .order_by(FbaBox.box_group)).scalars())


def _address_payload(address: ShipFromAddress) -> dict[str, Any]:
    return {"name": address.name, "addressLine1": address.address_line1,
            "addressLine2": address.address_line2 or "", "city": address.city,
            "stateOrProvinceCode": address.state_or_province, "postalCode": address.postal_code,
            "countryCode": address.country_code, "phoneNumber": address.phone, "email": address.email,
            "companyName": address.company or address.name}


def build_plan(session: Session, request: FbaInboundRequest) -> FbaInboundRequest:
    """createInboundPlan → packing option → packing information → placement options → transport-less quotes.

    Every write here is asynchronous: Amazon returns an operationId and we wait for it.
    """
    account = accounts.require(session)
    client = api.client()
    request.status = "building_plan"
    session.flush()

    address = session.get(ShipFromAddress, request.ship_from_address_id)
    items = items_of(session, request)
    body = {
        "destinationMarketplaces": [request.marketplace_id],
        "sourceAddress": _address_payload(address),
        "name": f"{request.ref} {utcnow():%Y-%m-%d}",
        "items": [{"msku": i.msku, "quantity": i.quantity,
                    "labelOwner": i.label_owner or request.label_owner,
                    "prepOwner": i.prep_owner or request.prep_owner,
                    **({"expiration": f"{i.expiration_date:%Y-%m-%d}"} if i.expiration_date else {}),
                    **({"manufacturingLotCode": i.manufacturing_lot_code}
                       if i.manufacturing_lot_code else {}),
                    **({"prepDetails": {"prepCategory": i.prep_category}} if i.prep_category else {})}
                  for i in items],
    }
    created = client.post(f"{BASE}/inboundPlans", json=body, operation="createInboundPlan")
    request.inbound_plan_id = created.get("inboundPlanId")
    _await(client, created)

    packing = _packing_option(session, client, request)
    request.packing_option_id = packing
    _set_packing_information(session, client, request)
    _generate_placement_options(session, client, request)
    return request


def _await(client, response: dict[str, Any]) -> dict[str, Any]:
    operation_id = response.get("operationId")
    return client.await_operation(operation_id) if operation_id else {}


def _packing_option(session: Session, client, request: FbaInboundRequest) -> str:
    plan = request.inbound_plan_id
    _await(client, client.post(f"{BASE}/inboundPlans/{plan}/packingOptions",
                               operation="generatePackingOptions"))
    options = client.get(f"{BASE}/inboundPlans/{plan}/packingOptions",
                         operation="listPackingOptions").get("packingOptions") or []
    if not options:
        raise AmazonError("Amazon didn't offer a packing option for this shipment.")
    chosen = options[0]["packingOptionId"]
    _await(client, client.post(f"{BASE}/inboundPlans/{plan}/packingOptions/{chosen}/confirmation",
                               operation="confirmPackingOption"))
    return chosen


def _set_packing_information(session: Session, client, request: FbaInboundRequest) -> None:
    boxes = boxes_of(session, request)
    groups = client.get(f"{BASE}/inboundPlans/{request.inbound_plan_id}/packingOptions",
                        operation="listPackingOptions").get("packingOptions") or []
    owners = {i.msku: (i.label_owner or request.label_owner, i.prep_owner or request.prep_owner)
              for i in items_of(session, request)}
    group_id = None
    for option in groups:
        if option.get("packingOptionId") == request.packing_option_id:
            group_id = (option.get("packingGroups") or [None])[0]
    payload = {"packageGroupings": [{
        "packingGroupId": group_id,
        "boxes": [{"quantity": box.box_count, "weight": {"unit": "LB", "value": float(box.weight_lb)},
                   "dimensions": {"unitOfMeasurement": "IN", "length": float(box.length_in),
                                  "width": float(box.width_in), "height": float(box.height_in)},
                   "contentInformationSource": "BOX_CONTENT_PROVIDED",
                   "items": [{"msku": c["msku"], "quantity": c["quantity_per_box"],
                              "labelOwner": owners.get(c["msku"], (request.label_owner, request.prep_owner))[0],
                              "prepOwner": owners.get(c["msku"], (request.label_owner, request.prep_owner))[1]}
                             for c in (box.contents or [])]}
                  for box in boxes]}]}
    _await(client, client.post(f"{BASE}/inboundPlans/{request.inbound_plan_id}/packingInformation",
                               json=payload, operation="setPackingInformation"))


def _generate_placement_options(session: Session, client, request: FbaInboundRequest) -> None:
    plan = request.inbound_plan_id
    _await(client, client.post(f"{BASE}/inboundPlans/{plan}/placementOptions",
                               operation="generatePlacementOptions"))
    options = client.get(f"{BASE}/inboundPlans/{plan}/placementOptions",
                         operation="listPlacementOptions").get("placementOptions") or []
    if not options:
        raise AmazonError("Amazon didn't return any warehouse options for this shipment.")
    earliest = None
    for number, option in enumerate(_cheapest_first(options), start=1):
        fees = option.get("fees") or []
        total = sum(float((f.get("value") or {}).get("amount") or 0) for f in fees)
        currency = next((((f.get("value") or {}).get("code")) for f in fees), "USD")
        shipments = []
        for shipment_id in option.get("shipmentIds") or []:
            detail = client.get(f"{BASE}/inboundPlans/{plan}/shipments/{shipment_id}",
                                operation="getShipment")
            destination = (detail.get("destination") or {}).get("address") or {}
            shipments.append({
                "shipment_id": shipment_id,
                "fc": (detail.get("destination") or {}).get("warehouseId") or destination.get("name"),
                "city": destination.get("city"), "state": destination.get("stateOrProvinceCode"),
                "postal_code": destination.get("postalCode"),
                "boxes": (detail.get("summary") or {}).get("boxCount"),
                "units": (detail.get("summary") or {}).get("unitCount")})
        expires = option.get("expiration")
        row = FbaPlacementOption(
            request_id=request.id, amazon_placement_option_id=option["placementOptionId"],
            option_number=number, status=option.get("status"), expires_at=_parse_time(expires),
            placement_fees={"amount": total, "currency": currency, "detail": fees},
            discounts={"list": option.get("discounts") or []},
            shipment_count=len(shipments), shipments_preview=shipments,
            transport_quotes=[],  # Amazon only quotes carriers after the placement is confirmed (D80)
            total_estimated_cost=total)
        session.add(row)
        if row.expires_at and (earliest is None or row.expires_at < earliest):
            earliest = row.expires_at
    request.expires_at = earliest
    request.status = "options_ready"
    session.flush()
    _schedule_reminders(session, request)
    audit.record(session, AGENT, "fba_placement_options_ready", target_type="fba_request", target_id=request.ref,
                 details={"options": len(options), "plan": plan})


def _cheapest_first(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def total(option):
        return sum(float((f.get("value") or {}).get("amount") or 0) for f in (option.get("fees") or []))

    return sorted(options, key=total)


def _parse_time(value: str | None):
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _schedule_reminders(session: Session, request: FbaInboundRequest) -> None:
    if request.expires_at:
        remind_at = request.expires_at - timedelta(minutes=REMIND_BEFORE_EXPIRY_MINUTES)
        if remind_at > utcnow():
            queue.enqueue(session, "fba_options_reminder", {"request_ref": request.ref}, run_after=remind_at,
                          idempotency_key=f"fba_remind:{request.ref}")
    queue.enqueue(session, "fba_abandon_check", {"request_ref": request.ref},
                  run_after=utcnow() + timedelta(hours=ABANDON_HOURS),
                  idempotency_key=f"fba_abandon:{request.ref}")


def options_of(session: Session, request: FbaInboundRequest) -> list[FbaPlacementOption]:
    return list(session.execute(select(FbaPlacementOption)
                                .where(FbaPlacementOption.request_id == request.id)
                                .order_by(FbaPlacementOption.option_number)).scalars())


PLACEMENT_PREFERENCE_KEY = "fba_placement_preference"


def preferred_option(session: Session, request: FbaInboundRequest) -> FbaPlacementOption | None:
    """The owner's rule: take the split Amazon charges least for — usually the one across most warehouses.

    It only pre-picks; the fee, the destinations and the carrier still go to him for approval.
    """
    if (settings_store.get(session, PLACEMENT_PREFERENCE_KEY, "lowest_fee") or "ask") == "ask":
        return None
    options = options_of(session, request)
    if len(options) < 2:
        return None
    return min(options, key=lambda o: (float((o.placement_fees or {}).get("amount") or 0),
                                       -(o.shipment_count or 1)))


def options_message(session: Session, request: FbaInboundRequest) -> str:
    """Every option Amazon returned, cheapest first, with destinations and fees."""
    options = options_of(session, request)
    picked = preferred_option(session, request)
    items = items_of(session, request)
    boxes = boxes_of(session, request)
    address = session.get(ShipFromAddress, request.ship_from_address_id)
    units = sum(i.quantity for i in items)
    box_count = sum(b.box_count for b in boxes)
    mp = mkt.by_id(request.marketplace_id)
    lines = [f"📦 FBA SHIPMENT {request.ref} — {units} units, {box_count} boxes ({mp.code})",
             f"From: {address.company or address.name}, {address.city} {address.state_or_province}   "
             f"Ready: {request.ready_to_ship_date:%a %b %d}", ""]
    for option in options:
        currency = (option.placement_fees or {}).get("currency", "USD")
        fee = float((option.placement_fees or {}).get("amount") or 0)
        label = "1 shipment" if option.shipment_count == 1 else f"{option.shipment_count} shipments"
        mine = "   ⬅ my pick" if picked is not None and option.id == picked.id else ""
        lines.append(f"Option {option.option_number} — {label}{mine}")
        for shipment in option.shipments_preview or []:
            where = ", ".join(x for x in [shipment.get("city"), shipment.get("state")] if x)
            lines.append(f"  • {shipment.get('fc') or '?'}{f' ({where})' if where else ''}: "
                         f"{shipment.get('boxes') or '?'} boxes, {shipment.get('units') or '?'} units")
        lines.append(f"  Placement fee: {currency} {fee:,.2f}"
                     + ("  (no placement fee)" if fee == 0 else ""))
        lines.append("")
    if request.expires_at:
        lines.append(f"Options expire: {fmt_dt(request.expires_at)}")
    if picked is not None:
        fee = float((picked.placement_fees or {}).get("amount") or 0)
        currency = (picked.placement_fees or {}).get("currency", "USD")
        warehouses = "1 warehouse" if picked.shipment_count == 1 else f"{picked.shipment_count} warehouses"
        lines.append(f"✅ I've picked Option {picked.option_number} — {warehouses}, "
                     + ("no placement fee" if fee == 0 else f"{currency} {fee:,.2f} placement fee")
                     + " (the cheapest Amazon offered).")
        lines.append(f"Reply: SHIP {request.ref} OPTION {picked.option_number} PARTNERED   (Amazon's carrier)")
        lines.append(f"   or: SHIP {request.ref} OPTION {picked.option_number} OWN CARRIER")
        lines.append(f"   or: SHIP {request.ref} OPTION <number>   for a different set of warehouses")
    else:
        lines.append(f"Reply: SHIP {request.ref} OPTION <number>")
    lines.append(f"   or: CANCEL {request.ref}")
    lines.append("Carrier prices come from Amazon after the warehouse choice is confirmed, and I'll ask you to "
                 "approve those separately before anything ships.")
    return "\n".join(lines)


def committed_fees(request: FbaInboundRequest) -> float:
    return sum(float(v) for k, v in (request.confirmed_fees or {}).items() if k != "currency")


def cancel_plan(session: Session, request: FbaInboundRequest, *, reason: str, told_owner: bool = False) -> str:
    """Before anything is committed this is free, so abandoned drafts are always cleaned up."""
    if request.status in ("confirmed", "in_transit", "receiving", "closed"):
        raise intake.ShipmentError(f"{request.ref} is already confirmed with Amazon. Cancelling it now may "
                                   "incur fees, so that needs a separate approval.")
    if committed_fees(request) > 0:
        # A confirmed placement with a real fee behind it: that is the owner's money, so it takes the PIN
        # even though the shipment hasn't reached "confirmed" yet.
        raise intake.ShipmentError(f"{request.ref} already committed "
                                   f"{(request.confirmed_fees or {}).get('currency', 'USD')} "
                                   f"{committed_fees(request):,.2f} in fees, so cancelling it needs your PIN.")
    if request.inbound_plan_id:
        try:
            api.client().put(f"{BASE}/inboundPlans/{request.inbound_plan_id}/cancellation",
                             operation="cancelInboundPlan")
        except (AmazonError, AmazonRetryable, AmazonOperationFailed) as exc:
            log.warning("could not cancel inbound plan", extra={"error": str(exc)[:150]})
    request.status = "cancelled"
    request.cancelled_at = utcnow()
    request.error = reason
    _withdraw_approval(session, request, reason)
    session.flush()
    audit.record(session, AGENT, "fba_plan_cancelled", target_type="fba_request", target_id=request.ref,
                 details={"reason": reason})
    return f"🗑️ {request.ref} cancelled ({reason}). No fees were committed."


def _withdraw_approval(session: Session, request: FbaInboundRequest, reason: str) -> None:
    """An approval for a cancelled shipment must never be approvable afterwards."""
    from app.db.models import Approval
    from app.services import approvals

    if not request.approval_id:
        return
    approval = session.get(Approval, request.approval_id)
    if approval is not None and approval.status == "pending":
        approvals.cancel(session, approval, AGENT, f"{request.ref} was cancelled ({reason})")


def set_ready_date(session: Session, request: FbaInboundRequest, words: str) -> str:
    """The owner's date, not mine. Changing it after placement re-asks Amazon for carrier options."""
    from app.services.common import utcnow

    wanted = intake._ready_date(words)
    if wanted is None:
        return ("I couldn't read a date in that. Try \"Monday\", \"tomorrow\", \"Sep 25\" or \"2026-09-25\".")
    if wanted < utcnow().date():
        return f"{wanted:%a %b %d} is in the past. Which day are the boxes actually leaving?"
    if request.status in ("in_transit", "receiving", "closed", "closed_with_discrepancy", "cancelled"):
        return f"{request.ref} is already {request.status.replace('_', ' ')}, so the date can't be changed here."

    request.ready_to_ship_date = wanted
    session.flush()
    audit.record(session, SYSTEM, "fba_ready_date_changed", target_type="fba_request", target_id=request.ref,
                 details={"date": str(wanted)})
    if request.status in ("confirming", "confirmed") and request.selected_placement_option_id:
        # The carrier options were quoted against the old window, so they have to be asked again.
        queue.enqueue(session, "fba_transport_options", {"request_ref": request.ref}, priority=15,
                      idempotency_key=f"fba_transport:{request.ref}:{wanted}")
        return (f"📅 {request.ref} now leaves {wanted:%a %b %d}. The carrier options were quoted for the old "
                "date, so I'm asking Amazon for them again and will send them over.")
    return f"📅 {request.ref} now leaves {wanted:%a %b %d}."


def set_prep(session: Session, listing_sku: str, words: str) -> str:
    """Save the prep category on the product so we only ask once."""
    from app.db.models import AmazonListing

    code = prep_code(words)
    if code is None:
        return ("I didn't recognise that prep. Reply \"no prep\", \"poly bag\", \"bubble wrap\", "
                "\"labeling only\" or \"taping\".")
    listing = session.execute(select(AmazonListing).where(AmazonListing.sku == listing_sku)).scalars().first()
    if listing is None:
        return f"I don't have a listing for {listing_sku}."
    product = session.get(Product, listing.product_id) if listing.product_id else None
    if product is None:
        product = Product(internal_name=listing.title_cached or listing.sku, brand_id=listing.brand_id)
        session.add(product)
        session.flush()
        listing.product_id = product.id
    product.prep_category = code
    session.flush()
    return f"Noted: {listing_sku} needs {words.strip()}."


# ---------------- the WhatsApp flow ----------------
PENDING_KEY = "fba_pending_shipment"


def start(session: Session, *, user, details: str, marketplace_code: str = "US", message_id=None) -> str:
    """Parse, pre-check, then queue the plan build. Questions come back in one message."""
    from app.amazon import fba_form

    window_pref = prep_words = ""
    if fba_form.looks_like_form(details):
        parsed, window_pref, prep_words = fba_form.parse(details)
    else:
        parsed = intake.parse(details)
        if not parsed.items:
            return fba_form.FORM          # nothing to go on: send the form rather than a list of questions
    check = intake.precheck(session, parsed, marketplace_code=marketplace_code)
    if prep_words:
        for row in check.resolved:
            set_prep(session, row["listing"].sku, prep_words)
        check = intake.precheck(session, parsed, marketplace_code=marketplace_code)
    if parsed.questions or check.questions:
        settings_store.set_value(session, PENDING_KEY,
                                 {"details": details, "marketplace": marketplace_code,
                                  "prep_for": check.prep_needed,
                                  "message_id": str(message_id) if message_id else None},
                                 Actor.user(user.id))
        return intake.questions_message(check, parsed)
    if check.errors:
        return intake.problems_message(check)
    request = create_request(session, parsed=parsed, check=check, marketplace_code=marketplace_code,
                             instructions=details, source_message_id=message_id)
    request.delivery_window_pref = window_pref or None
    settings_store.set_value(session, PENDING_KEY, None, Actor.user(user.id))
    queue.enqueue(session, "fba_build_plan", {"request_ref": request.ref}, priority=20,
                  idempotency_key=f"fba_build:{request.ref}")
    return intake.summary_message(request, check)


CARRIER_ANSWER_RE = re.compile(r"\b(own\s*carrier|own|myself|my\s+carrier|partnered|amazon'?s?\s*carrier|"
                               r"amazon)\b", re.I)


def _carrier_answer(session: Session, *, user, answers: str) -> str | None:
    """"own carrier" on its own, when that is exactly what was asked."""
    from app.amazon import fba_confirm

    match = CARRIER_ANSWER_RE.search(answers or "")
    if not match:
        return None
    waiting = settings_store.get(session, fba_confirm.PENDING_CARRIER_KEY) or None
    if not waiting:
        # Nothing remembered (a restart, or the question predates this): fall back to the shipment
        # that is actually waiting, and ask which option rather than choosing one for him.
        open_ones = session.execute(select(FbaInboundRequest)
                                    .where(FbaInboundRequest.status == "options_ready")).scalars().all()
        if len(open_ones) != 1:
            return None
        request = open_ones[0]
        options = options_of(session, request)
        if len(options) == 1:
            waiting = {"ref": request.ref, "option": options[0].option_number}
        else:
            lines = [f"Which option for {request.ref}? I'll use your own carrier."]
            for option in options:
                fee = float((option.placement_fees or {}).get("amount") or 0)
                where = "1 warehouse" if option.shipment_count == 1 else f"{option.shipment_count} warehouses"
                lines.append(f"• Option {option.option_number}: {where}, "
                             f"{(option.placement_fees or {}).get('currency', 'USD')} {fee:,.2f} placement fee")
            lines.append(f"Reply \"SHIP {request.ref} OPTION <number> OWN CARRIER\".")
            return "\n".join(lines)
    word = match.group(1).lower()
    choice = "partnered" if word.startswith(("partner", "amazon")) else "own carrier"
    request = get_by_ref(session, waiting["ref"])
    return fba_confirm.choose_option(session, request, int(waiting["option"]),
                                     carrier_choice=choice, user=user)


def awaiting_option(session: Session) -> FbaInboundRequest | None:
    """The shipment whose options are on the table, when there is exactly one."""
    open_ones = session.execute(select(FbaInboundRequest)
                                .where(FbaInboundRequest.status == "options_ready")).scalars().all()
    return open_ones[0] if len(open_ones) == 1 else None


def answer(session: Session, *, user, answers: str, message_id=None) -> str:
    """The owner answered the questions: add his words to the original message and try again."""
    carrier = _carrier_answer(session, user=user, answers=answers)
    if carrier is not None:
        return carrier
    pending = settings_store.get(session, PENDING_KEY)
    if not pending:
        return "I don't have a shipment waiting on answers. Tell me what you're sending and I'll start one."
    hints = _prep_answers(answers)
    if not hints and prep_code(answers):
        # He answered the question as it was asked ("no prep"), so it belongs to the SKU we asked about.
        hints = [(sku, answers.strip()) for sku in (pending.get("prep_for") or [])]
    prep_note = ""
    for sku_hint, words in hints:
        prep_note = set_prep(session, sku_hint, words) + "\n"
    combined = f"{pending['details']}\n{answers}"
    return prep_note + start(session, user=user, details=combined,
                             marketplace_code=pending.get("marketplace", "US"), message_id=message_id)


def _prep_answers(answers: str) -> list[tuple[str, str]]:
    """"SKZ-001 needs poly bag" → (SKZ-001, poly bag)."""
    import re as _re

    out = []
    for line in (answers or "").splitlines():
        match = _re.match(r"\s*([\w.\-/]+)\s*(?:needs|:|-)\s*(.+)", line)
        if match and prep_code(match.group(2)):
            out.append((match.group(1), match.group(2)))
    return out


def detail_message(session: Session, request: FbaInboundRequest) -> str:
    items = items_of(session, request)
    boxes = boxes_of(session, request)
    status_words = {
        "drafting": "being drafted", "needs_info": "waiting on your answers",
        "prechecks_passed": "checked, asking Amazon for options", "building_plan": "asking Amazon for options",
        "options_ready": "waiting for you to pick an option", "pending_approval": "waiting for your approval",
        "confirming": "being confirmed with Amazon", "confirmed": "confirmed",
        "in_transit": "in transit", "receiving": "being received", "closed": "closed",
        "closed_with_discrepancy": "closed with a shortage", "expired": "expired",
        "cancelled": "cancelled", "failed": "failed",
    }
    lines = [f"📦 *{request.ref}* — {status_words.get(request.status, request.status)}",
             f"Ready to ship: {request.ready_to_ship_date:%a %b %d} · "
             f"{'boxes' if request.shipping_mode == 'small_parcel' else 'pallets'}"]
    for item in items:
        lines.append(f"• {item.msku} ({item.asin or '—'}): {item.quantity} units")
    for box in boxes:
        contents = ", ".join(f"{c['quantity_per_box']}× {c['msku']}" for c in (box.contents or []))
        lines.append(f"• {box.box_count} boxes of {float(box.length_in):g}×{float(box.width_in):g}×"
                     f"{float(box.height_in):g} in, {float(box.weight_lb):g} lb — {contents}")
    if request.status == "options_ready":
        lines.append("")
        lines.append(options_message(session, request))
    if request.error:
        lines.append(f"⚠️ {request.error}")
    return "\n".join(lines)


def list_message(session: Session) -> str:
    open_statuses = ("prechecks_passed", "building_plan", "options_ready", "pending_approval", "confirming",
                     "confirmed", "in_transit", "receiving")
    rows = list(session.execute(select(FbaInboundRequest)
                                .where(FbaInboundRequest.status.in_(open_statuses))
                                .order_by(FbaInboundRequest.created_at.desc())).scalars())
    if not rows:
        return "No open FBA shipments. Tell me what you want to send and I'll start one."
    lines = ["📦 *Open FBA shipments*"]
    for request in rows:
        items = items_of(session, request)
        units = sum(i.quantity for i in items)
        lines.append(f"• {request.ref} — {units} units, {len(items)} SKU(s) — {request.status.replace('_', ' ')}"
                     + (f", ready {request.ready_to_ship_date:%b %d}" if request.ready_to_ship_date else ""))
    lines.append("\nReply with a ref (FB-2) for details.")
    return "\n".join(lines)
