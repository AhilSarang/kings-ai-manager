"""Confirming a shipment and everything after it (brief 12.5–12.8).

Two approvals, because Amazon only quotes carriers once a placement option is confirmed (D80):
  1. placement — destinations and placement fees
  2. transportation — carrier and its cost
Neither is ever executed without an approval whose payload matches what is about to be confirmed.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon import accounts, api
from app.amazon import fba_intake as intake
from app.amazon import fba_plan
from app.amazon.errors import AmazonAuthError, AmazonError, AmazonOperationFailed, AmazonRetryable
from app.db.models import FbaInboundRequest, FbaPlacementOption, FbaShipment, File
from app.jobs import queue
from app.services import approvals, audit, files as files_svc, settings_store
from app.services.common import Actor, utcnow
from app.services.formatting import fmt_dt

log = logging.getLogger(__name__)
AGENT = Actor.agent("amazon")
BASE = fba_plan.BASE


STALE_MARKER = "asking Amazon for fresh options"


def _client():
    return api.client()


def _threshold(session: Session) -> float:
    return float(settings_store.get(session, "fba_pin_required_above_usd", 250) or 250)


# ---------------- approval 1: placement ----------------
PENDING_CARRIER_KEY = "fba_pending_carrier_question"


def choose_option(session: Session, request: FbaInboundRequest, option_number: int, *,
                  carrier_choice: str | None, user) -> str:
    if request.status != "options_ready":
        raise intake.ShipmentError(f"{request.ref} is {request.status.replace('_', ' ')}, so there's no option "
                                   "to pick right now.")
    options = {o.option_number: o for o in fba_plan.options_of(session, request)}
    option = options.get(option_number)
    if option is None:
        raise intake.ShipmentError(f"{request.ref} has options {', '.join(str(n) for n in sorted(options))}. "
                                   f"Which one?")
    if option.expires_at and option.expires_at <= utcnow():
        return regenerate(session, request, reason="those options expired")
    choice = (carrier_choice or settings_store.get(session, "fba_default_carrier_choice", "ask") or "ask").lower()
    if choice in ("ask", "", None):
        # Remembered, so "own carrier" on its own is an answer to this question and not a lost message.
        settings_store.set_value(session, PENDING_CARRIER_KEY,
                                 {"ref": request.ref, "option": option_number}, Actor.user(user.id) if user else None)
        return (f"Which carrier for {request.ref}?\n"
                f"• Amazon's carrier: SHIP {request.ref} OPTION {option_number} PARTNERED\n"
                f"• Your own freight: SHIP {request.ref} OPTION {option_number} OWN CARRIER\n"
                "Or just reply \"partnered\" or \"own carrier\".")
    settings_store.set_value(session, PENDING_CARRIER_KEY, None, Actor.user(user.id) if user else None)
    fees = option.placement_fees or {}
    amount, currency = float(fees.get("amount") or 0), fees.get("currency", "USD")
    payload = {
        "request_ref": request.ref, "inbound_plan_id": request.inbound_plan_id,
        "placement_option_id": option.amazon_placement_option_id, "option_number": option.option_number,
        "placement_fee": amount, "currency": currency, "carrier_choice": choice,
        "shipments": option.shipments_preview, "ready_date": str(request.ready_to_ship_date),
    }
    approval = approvals.request_approval(
        session, action_type="fba_confirm_shipment", payload=payload,
        summary=_placement_summary(session, request, option, choice),
        risk_level="critical" if amount > _threshold(session) else "high",
        requested_by_agent_key="amazon", notify=False)
    request.approval_id = approval.id
    request.selected_placement_option_id = option.amazon_placement_option_id
    request.selected_transport_choice = "partnered" if choice.startswith("partner") else "own_carrier"
    request.estimated_fees = {"placement": amount, "currency": currency}
    request.status = "pending_approval"
    session.flush()
    return approvals.approval_message(approval)


def _placement_summary(session: Session, request: FbaInboundRequest, option: FbaPlacementOption,
                       choice: str) -> str:
    items = fba_plan.items_of(session, request)
    boxes = fba_plan.boxes_of(session, request)
    fees = option.placement_fees or {}
    amount, currency = float(fees.get("amount") or 0), fees.get("currency", "USD")
    lines = [f"📦 CONFIRM SHIPMENT {request.ref} — step 1 of 2: warehouses",
             f"Option {option.option_number}: {option.shipment_count} shipment(s)"]
    for item in items:
        lines.append(f"• {item.msku}: {item.quantity} units")
    lines.append(f"• {sum(b.box_count for b in boxes)} boxes, ready {request.ready_to_ship_date:%a %b %d}")
    for shipment in option.shipments_preview or []:
        where = ", ".join(x for x in [shipment.get("city"), shipment.get("state")] if x)
        lines.append(f"→ {shipment.get('fc')} ({where}): {shipment.get('boxes')} boxes, "
                     f"{shipment.get('units')} units")
    lines.append(f"Placement fee: {currency} {amount:,.2f}")
    lines.append(f"Carrier: {'Amazon partnered carrier' if choice.startswith('partner') else 'your own carrier'}")
    lines.append("Confirming commits Kings Distro to the placement fee and creates the shipments. "
                 "Carrier costs are quoted next, and you approve those separately before anything ships.")
    return "\n".join(lines)


def regenerate(session: Session, request: FbaInboundRequest, *, reason: str) -> str:
    """Options expired: never confirm a stale one, build fresh options instead."""
    if request.approval_id:
        from app.db.models import Approval

        approval = session.get(Approval, request.approval_id)
        if approval is not None and approval.status == "pending":
            approvals.cancel(session, approval, AGENT, "placement options expired")
    for option in fba_plan.options_of(session, request):
        session.delete(option)
    request.status = "prechecks_passed"
    request.approval_id = None
    session.flush()
    queue.enqueue(session, "fba_build_plan", {"request_ref": request.ref},
                  idempotency_key=f"fba_rebuild:{request.ref}:{utcnow():%Y%m%d%H%M}")
    return (f"⏰ {request.ref}: {reason}, so nothing was confirmed. I'm asking Amazon for fresh options now and "
            "will send them over.")


def _placement_failed(session: Session, payload: dict, approval, error: str) -> None:
    """Runs in its own transaction, so this is where stale options get rebuilt."""
    request = session.execute(select(FbaInboundRequest)
                              .where(FbaInboundRequest.ref == payload["request_ref"])).scalar_one_or_none()
    if request is None:
        return
    request.error = error[:500]
    if STALE_MARKER in error:
        regenerate(session, request, reason="those options were no longer valid")
    elif request.status in ("pending_approval", "confirming"):
        request.status = "options_ready"


@approvals.executor("fba_confirm_shipment", on_failure=_placement_failed)
def execute_placement(session: Session, payload: dict, approval) -> dict:
    """Confirm the placement option, then ask Amazon to quote carriers for the new shipments."""
    from app.jobs.handlers.amazon import amazon_paused

    request = session.execute(select(FbaInboundRequest).where(
        FbaInboundRequest.ref == payload["request_ref"]).with_for_update(of=FbaInboundRequest)).scalar_one()
    if request.status != "pending_approval" or request.approval_id != approval.id:
        raise intake.ShipmentError(f"{request.ref} is no longer waiting for approval; nothing was confirmed.")
    if amazon_paused(session):
        raise intake.ShipmentError("Amazon is paused, so nothing was confirmed. Send AMAZON RESUME and approve "
                                   "again.")
    accounts.require(session)
    client = _client()
    plan = payload["inbound_plan_id"]

    # Freshness: the option must still be offered, with the same fee.
    fresh = {o.get("placementOptionId"): o for o in
             (client.get(f"{BASE}/inboundPlans/{plan}/placementOptions",
                         operation="listPlacementOptions").get("placementOptions") or [])}
    current = fresh.get(payload["placement_option_id"])
    if current is None or (current.get("status") or "").upper() == "EXPIRED":
        raise intake.ShipmentError("that option expired before it was confirmed, so nothing was confirmed. "
                                   f"I'm {STALE_MARKER} now.")
    fee_now = sum(float((f.get("value") or {}).get("amount") or 0) for f in (current.get("fees") or []))
    if abs(fee_now - float(payload["placement_fee"])) > 0.01:
        raise intake.ShipmentError(f"Amazon changed the placement fee to {fee_now:,.2f}, so nothing was "
                                   f"confirmed. I'm {STALE_MARKER} now.")

    request.status = "confirming"
    session.flush()
    _await(client, client.post(
        f"{BASE}/inboundPlans/{plan}/placementOptions/{payload['placement_option_id']}/confirmation",
        operation="confirmPlacementOption"))

    shipments = _store_shipments(session, request, client, plan, payload)
    request.confirmed_fees = {"placement": float(payload["placement_fee"]), "currency": payload["currency"]}
    audit.record(session, AGENT, "fba_placement_confirmed", target_type="fba_request", target_id=request.ref,
                 details={"option": payload["placement_option_id"], "shipments": [s.amazon_shipment_id
                                                                                  for s in shipments]})
    queue.enqueue(session, "fba_transport_options", {"request_ref": request.ref}, priority=15,
                  idempotency_key=f"fba_transport:{request.ref}")
    return {"summary": f"{request.ref}: warehouses confirmed, {len(shipments)} shipment(s) created. Asking "
                       "Amazon for carrier prices now; I'll send them for your approval.",
            "complete_task": False}


def _await(client, response: dict[str, Any]) -> dict[str, Any]:
    operation_id = response.get("operationId")
    return client.await_operation(operation_id) if operation_id else {}


def _store_shipments(session: Session, request: FbaInboundRequest, client, plan: str,
                     payload: dict) -> list[FbaShipment]:
    rows = []
    for preview in payload.get("shipments") or []:
        shipment_id = preview.get("shipment_id")
        detail = client.get(f"{BASE}/inboundPlans/{plan}/shipments/{shipment_id}", operation="getShipment")
        row = FbaShipment(
            request_id=request.id, amazon_shipment_id=shipment_id,
            shipment_confirmation_id=detail.get("shipmentConfirmationId"),
            destination_fc=preview.get("fc"),
            destination_address={k: preview.get(k) for k in ("city", "state", "postal_code")},
            status=detail.get("status") or "WORKING",
            # The preview doesn't always carry the counts; Amazon's own shipment record does.
            units_expected=preview.get("units") or (detail.get("summary") or {}).get("unitCount") or 0,
            transport_mode=request.shipping_mode)
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


# ---------------- approval 2: transportation ----------------
TRANSPORT_PAGE_SIZE = 20     # Amazon's maximum for this endpoint


def _transport_options(client, plan: str, placement_option_id: str) -> list[dict]:
    """Every carrier option for a confirmed placement.

    Amazon refuses this read unless the placement option (or a shipment id) is named, and it caps
    the page at 20, so both are handled here rather than at the call site.
    """
    options, token = [], None
    while True:
        params = {"placementOptionId": placement_option_id, "pageSize": TRANSPORT_PAGE_SIZE}
        if token:
            params["paginationToken"] = token
        page = client.get(f"{BASE}/inboundPlans/{plan}/transportationOptions", params=params,
                          operation="listTransportationOptions")
        options += page.get("transportationOptions") or []
        token = (page.get("pagination") or {}).get("nextToken")
        if not token:
            return options


def _window_start(request: FbaInboundRequest) -> str:
    """08:00 UTC on the day the boxes leave — or very soon, if that hour is already behind us.

    Amazon rejects a ready-to-ship window that starts in the past, which is every same-day shipment
    after 4am Eastern.
    """
    from datetime import UTC, datetime, time, timedelta

    start = datetime.combine(request.ready_to_ship_date, time(8, 0), tzinfo=UTC)
    return max(start, utcnow() + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_transport_options(session: Session, request: FbaInboundRequest) -> str:
    """After placement is confirmed, Amazon can price carriers."""
    client = _client()
    plan = request.inbound_plan_id
    shipments = shipments_of(session, request)
    body = {"placementOptionId": request.selected_placement_option_id,
            "shipmentTransportationConfigurations": [
                {"shipmentId": s.amazon_shipment_id,
                 "readyToShipWindow": {"start": _window_start(request)},
                 "contactInformation": {"email": request.ship_from.email, "name": request.ship_from.name,
                                        "phoneNumber": request.ship_from.phone}}
                for s in shipments]}
    _await(client, client.post(f"{BASE}/inboundPlans/{plan}/transportationOptions", json=body,
                               operation="generateTransportationOptions"))
    options = _transport_options(client, plan, request.selected_placement_option_id)
    wanted = "AMAZON_PARTNERED_CARRIER" if request.selected_transport_choice == "partnered" else "USE_YOUR_OWN"
    quotes = []
    for option in options:
        solution = (option.get("shippingSolution") or "").upper()
        if wanted not in solution and solution:
            continue
        quote = option.get("quote") or {}
        quotes.append({"transportation_option_id": option.get("transportationOptionId"),
                       "shipment_id": option.get("shipmentId"),
                       "carrier": ((option.get("carrier") or {}).get("name")
                                   or (option.get("carrier") or {}).get("alphaCode") or "carrier"),
                       "mode": option.get("shippingMode"),
                       "amount": float((quote.get("cost") or {}).get("amount") or 0),
                       "currency": (quote.get("cost") or {}).get("code", "USD"),
                       "solution": solution})
    option_row = session.execute(select(FbaPlacementOption).where(
        FbaPlacementOption.request_id == request.id,
        FbaPlacementOption.amazon_placement_option_id == request.selected_placement_option_id)
    ).scalar_one_or_none()
    if option_row is not None:
        option_row.transport_quotes = quotes
    session.flush()
    if not quotes:
        return (f"{request.ref}: Amazon didn't offer a "
                f"{'partnered carrier' if wanted.startswith('AMAZON') else 'own-carrier'} option for these "
                "shipments. Reply \"SHIP " + request.ref + " OWN CARRIER\" to arrange it yourself, or tell me "
                "to cancel.")
    return quotes_message(session, request, quotes)


def _by_shipment(quotes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for quote in quotes:
        grouped.setdefault(quote["shipment_id"], []).append(quote)
    return grouped


def carrier_names(quotes: list[dict[str, Any]]) -> list[str]:
    """The carriers offered for every shipment, so a choice covers the whole request."""
    grouped = _by_shipment(quotes)
    sets = [{q["carrier"].strip() for q in rows} for rows in grouped.values()]
    common = set.intersection(*sets) if sets else set()
    return sorted(common, key=str.lower)


def _own_carrier(quotes: list[dict[str, Any]]) -> bool:
    return all("OWN" in (q.get("solution") or "").upper() for q in quotes)


def quotes_message(session: Session, request: FbaInboundRequest, quotes: list[dict[str, Any]],
                   chosen: list[dict[str, Any]] | None = None) -> str:
    """What Amazon is charging (partnered), or who can collect it (own carrier)."""
    shown = chosen if chosen is not None else quotes
    total = sum(q["amount"] for q in shown)
    currency = (shown or quotes)[0]["currency"] if (shown or quotes) else "USD"
    placement = float((request.confirmed_fees or {}).get("placement") or 0)
    header = "carrier" if _own_carrier(quotes) else "carrier prices"
    lines = [f"🚚 {request.ref} — step 2 of 2: {header}"]
    shipments = {s.amazon_shipment_id: s for s in shipments_of(session, request)}

    if chosen is None and _own_carrier(quotes) and len(quotes) > len(_by_shipment(quotes)):
        # Own carrier: Amazon lists everyone it can hand boxes to, all at no cost to it.
        for shipment_id, rows in _by_shipment(quotes).items():
            where = shipments[shipment_id].destination_fc if shipment_id in shipments else shipment_id
            units = shipments[shipment_id].units_expected if shipment_id in shipments else 0
            lines.append(f"• {where}: {len(rows)} carriers available" + (f" ({units} units)" if units else ""))
        names = carrier_names(quotes)
        lines.append("")
        lines.append("You're arranging the freight, so Amazon isn't quoting a price — tell me who you've "
                     "booked and I'll register it:")
        lines += [f"  {i}. {name}" for i, name in enumerate(names[:12], start=1)]
        if len(names) > 12:
            lines.append(f"  …and {len(names) - 12} more — name any of them.")
        lines.append(f"Reply \"SHIP {request.ref} CARRIER <name>\", or \"CANCEL {request.ref}\".")
        return "\n".join(lines)

    for quote in shown:
        shipment = shipments.get(quote["shipment_id"])
        where = shipment.destination_fc if shipment else quote["shipment_id"]
        cost = ("you pay them directly" if "OWN" in (quote.get("solution") or "").upper()
                else f"{quote['currency']} {quote['amount']:,.2f}")
        lines.append(f"• {where}: {quote['carrier']} {quote['mode'] or ''} — {cost}")
    if not _own_carrier(shown):
        lines.append(f"Carrier total: {currency} {total:,.2f}")
    lines.append(f"Placement fee already confirmed: {currency} {placement:,.2f}")
    lines.append(f"Running total: {currency} {total + placement:,.2f}")
    if chosen is None:
        lines.append(f"Reply \"SHIP {request.ref} CARRIER\" to approve the carrier, or \"CANCEL {request.ref}\".")
    return "\n".join(lines)


def choose_carrier(session: Session, request: FbaInboundRequest, *, user, carrier: str | None = None) -> str:
    option_row = session.execute(select(FbaPlacementOption).where(
        FbaPlacementOption.request_id == request.id,
        FbaPlacementOption.amazon_placement_option_id == request.selected_placement_option_id)
    ).scalar_one_or_none()
    quotes = (option_row.transport_quotes if option_row else []) or []
    if not quotes:
        if request.status in ("confirming", "confirmed") and request.selected_placement_option_id:
            queue.enqueue(session, "fba_transport_options", {"request_ref": request.ref}, priority=15,
                          idempotency_key=f"fba_transport:{request.ref}:{utcnow():%Y%m%d%H%M}")
            return (f"{request.ref} has no carrier prices yet — I'm asking Amazon for them now and will send "
                    "them over.")
        raise intake.ShipmentError(f"{request.ref} has no carrier quotes yet. I'll send them as soon as Amazon "
                                   "returns them.")
    quotes = _pick_one_per_shipment(session, request, quotes, carrier)
    total = sum(q["amount"] for q in quotes)
    placement = float((request.confirmed_fees or {}).get("placement") or 0)
    payload = {"request_ref": request.ref, "inbound_plan_id": request.inbound_plan_id,
               "transportation_option_ids": [{"shipmentId": q["shipment_id"],
                                              "transportationOptionId": q["transportation_option_id"]}
                                             for q in quotes],
               "carrier_total": total, "currency": quotes[0]["currency"],
               "placement_fee": placement}
    usual = settings_store.get(session, DEFAULT_CARRIER_KEY, None)
    note = ("" if carrier or not usual else
            f"\n({usual} is your usual choice — reply SHIP {request.ref} CARRIER <name> for a different one.)")
    summary = (quotes_message(session, request, quotes, chosen=quotes) + note
               + ("\n\nApproving tells Amazon to expect these boxes from this carrier. You book and pay the "
                  "carrier yourself." if _own_carrier(quotes) else
                  "\n\nApproving books this carrier and commits Kings Distro to the cost above."))
    approval = approvals.request_approval(
        session, action_type="fba_confirm_transport", payload=payload, summary=summary,
        risk_level="critical" if (total + placement) > _threshold(session) else "high",
        requested_by_agent_key="amazon", notify=False)
    request.approval_id = approval.id
    request.estimated_fees = {**(request.estimated_fees or {}), "carrier": total}
    session.flush()
    return approvals.approval_message(approval)


DEFAULT_CARRIER_KEY = "fba_default_own_carrier"


def _matches(rows: list[dict[str, Any]], wanted: str) -> list[dict[str, Any]]:
    """Exact name first — "Other" must never pick "Brothers Transport"."""
    name = wanted.strip().lower()
    exact = [q for q in rows if q["carrier"].strip().lower() == name]
    return exact or [q for q in rows if name in q["carrier"].lower()]


def _pick_one_per_shipment(session: Session, request: FbaInboundRequest, quotes: list[dict[str, Any]],
                           carrier: str | None) -> list[dict[str, Any]]:
    """Exactly one carrier per shipment goes to Amazon — never the whole list it offered."""
    grouped = _by_shipment(quotes)
    if all(len(rows) == 1 for rows in grouped.values()):
        return [rows[0] for rows in grouped.values()]

    asked_for = carrier
    if not carrier:
        # the owner's usual choice, when Amazon offers it for every shipment.
        usual = settings_store.get(session, DEFAULT_CARRIER_KEY, None)
        if usual and all(_matches(rows, usual) for rows in grouped.values()):
            carrier = usual
    if not carrier:
        raise intake.ShipmentError(quotes_message(session, request, quotes))

    chosen = []
    for shipment_id, rows in grouped.items():
        matches = _matches(rows, carrier)
        if not matches:
            names = ", ".join(sorted({q["carrier"] for q in rows})[:8])
            raise intake.ShipmentError(f"Amazon doesn't offer \"{asked_for or carrier}\" for every shipment in "
                                       f"{request.ref}. For {shipment_id} it lists: {names}.")
        # Prefer the mode the owner's boxes are going as, then whatever is cheapest.
        mode = "FREIGHT_LTL" if request.shipping_mode == "ltl_pallet" else "GROUND_SMALL_PARCEL"
        matches.sort(key=lambda q: (0 if (q.get("mode") or "") == mode else 1, q["amount"]))
        chosen.append(matches[0])
    return chosen


def _transport_failed(session: Session, payload: dict, approval, error: str) -> None:
    request = session.execute(select(FbaInboundRequest)
                              .where(FbaInboundRequest.ref == payload["request_ref"])).scalar_one_or_none()
    if request is not None:
        request.error = error[:500]
        _alert(session, request, "shipment_problem", f"{request.ref}: {error}"[:400])


@approvals.executor("fba_confirm_transport", on_failure=_transport_failed)
def execute_transport(session: Session, payload: dict, approval) -> dict:
    """Confirm the carrier, then fetch the labels."""
    from app.jobs.handlers.amazon import amazon_paused

    request = session.execute(select(FbaInboundRequest).where(
        FbaInboundRequest.ref == payload["request_ref"]).with_for_update(of=FbaInboundRequest)).scalar_one()
    if amazon_paused(session):
        raise intake.ShipmentError("Amazon is paused, so the carrier wasn't booked.")
    client = _client()
    plan = payload["inbound_plan_id"]
    windows: list[str] = []
    try:
        _confirm_transport(client, plan, payload["transportation_option_ids"])
    except (AmazonError, AmazonOperationFailed) as exc:
        if "delivery window" not in str(exc).lower():
            raise intake.ShipmentError(f"Amazon couldn't book the carrier: {exc}. The warehouses are still "
                                       f"confirmed, so reply \"SHIP {request.ref} CARRIER\" to try "
                                       "again.") from exc
        # Some options need a delivery window agreed before the carrier can be confirmed. The windows
        # expire within the hour, so they are chosen and confirmed here rather than at approval time.
        for shipment in shipments_of(session, request):
            window = _confirm_delivery_window(client, plan, shipment, request.ready_to_ship_date,
                                              preference=request.delivery_window_pref)
            if window:
                windows.append(f"{shipment.destination_fc}: {window}")
        try:
            _confirm_transport(client, plan, payload["transportation_option_ids"])
        except (AmazonError, AmazonOperationFailed) as second:
            raise intake.ShipmentError(f"Amazon couldn't book the carrier: {second}. The warehouses are "
                                       f"still confirmed, so reply \"SHIP {request.ref} CARRIER\" to try "
                                       "again.") from second
    request.status = "confirmed"
    request.confirmed_at = utcnow()
    request.confirmed_fees = {**(request.confirmed_fees or {}), "carrier": payload["carrier_total"],
                              "currency": payload["currency"]}
    session.flush()
    audit.record(session, AGENT, "fba_transport_confirmed", target_type="fba_request", target_id=request.ref,
                 details={"total": payload["carrier_total"]})
    queue.enqueue(session, "fba_fetch_labels", {"request_ref": request.ref}, priority=15,
                  idempotency_key=f"fba_labels:{request.ref}")
    total = float(payload["carrier_total"]) + float(payload["placement_fee"])
    window_note = ("\nDelivery window agreed with Amazon — " + "; ".join(windows) if windows else "")
    return {"summary": f"{request.ref} is confirmed. Total committed: {payload['currency']} {total:,.2f}."
                       f"{window_note}\nFetching your labels now.", "complete_task": False}


def _confirm_transport(client, plan: str, selections: list[dict[str, Any]]) -> None:
    _await(client, client.post(f"{BASE}/inboundPlans/{plan}/transportationOptions/confirmation",
                               json={"transportationSelections": selections},
                               operation="confirmTransportationOptions"))


def _confirm_delivery_window(client, plan: str, shipment: FbaShipment, ready_date,
                             preference: str | None = None) -> str | None:
    """Agree when the boxes will arrive at the warehouse. Amazon won't book a carrier without it."""
    from datetime import datetime

    base = f"{BASE}/inboundPlans/{plan}/shipments/{shipment.amazon_shipment_id}/deliveryWindowOptions"
    try:
        _await(client, client.post(base, json={}, operation="generateDeliveryWindowOptions"))
    except (AmazonError, AmazonOperationFailed) as exc:
        log.info("delivery windows were already generated", extra={"error": str(exc)[:120]})
    options = (client.get(base, params={"pageSize": 20},
                          operation="listDeliveryWindowOptions").get("deliveryWindowOptions") or [])
    available = [o for o in options if (o.get("availabilityType") or "").upper() == "AVAILABLE"]
    if not available:
        return None

    def starts(option: dict[str, Any]) -> datetime:
        return datetime.fromisoformat((option.get("startDate") or "").replace("Z", "+00:00"))

    # The soonest window that doesn't start before the boxes are ready…
    ready = [o for o in available if starts(o).date() >= ready_date] or available
    chosen = min(ready, key=starts)
    # …unless the owner asked for a particular time on the form ("week of Oct 6", "after the 25th").
    wanted = intake._ready_date(preference or "")
    if wanted:
        later = [o for o in ready if starts(o).date() >= wanted]
        chosen = min(later or ready, key=starts)
    _await(client, client.post(f"{base}/{chosen['deliveryWindowOptionId']}/confirmation",
                               json={}, operation="confirmDeliveryWindowOptions"))
    shipment.delivery_window = {"start": chosen.get("startDate"), "end": chosen.get("endDate")}
    return f"{starts(chosen):%a %b %d} – {datetime.fromisoformat((chosen.get('endDate') or '').replace('Z', '+00:00')):%a %b %d}"


# ---------------- labels ----------------
def fetch_labels(session: Session, request: FbaInboundRequest) -> list[dict[str, Any]]:
    """Box labels for every box, plus the carrier label or bill of lading when Amazon has one."""
    client = _client()
    page_type = settings_store.get(session, "fba_label_page_type", "PackageLabel_Thermal_NonPCP")
    out = []
    for shipment in shipments_of(session, request):
        params = {"pageType": page_type, "labelType": "BARCODE_2D",
                  "shipmentId": shipment.amazon_shipment_id}
        response = client.get(f"{BASE}/inboundPlans/{request.inbound_plan_id}/shipments/"
                              f"{shipment.amazon_shipment_id}/labels", params=params, operation="getLabels")
        for label in response.get("labels") or response.get("documentDownloads") or []:
            url = label.get("downloadURL") or label.get("url")
            if not url:
                continue
            data = _download(url)
            if data is None:
                continue
            stored = files_svc.save_file(session, data, mime_type="application/pdf", source="agent", actor=AGENT,
                                         original_name=f"{request.ref}-{shipment.destination_fc}-"
                                                       f"{label.get('type', 'labels')}.pdf")
            kind = (label.get("type") or "box").lower()
            if "bill" in kind or "bol" in kind:
                shipment.bill_of_lading_file_id = stored.id
            elif "carrier" in kind or "ship" in kind:
                shipment.carrier_label_file_id = stored.id
            else:
                shipment.box_label_file_id = stored.id
            out.append({"shipment": shipment, "file": stored, "kind": kind})
    session.flush()
    return out


def _download(url: str) -> bytes | None:
    import httpx

    try:
        response = httpx.get(url, timeout=60, follow_redirects=True)
        return response.content if response.status_code == 200 else None
    except httpx.HTTPError as exc:
        log.warning("label download failed", extra={"error": str(exc)[:150]})
        return None


def labels_message(session: Session, request: FbaInboundRequest, labels: list[dict[str, Any]]) -> str:
    lines = [f"🏷️ {request.ref} labels"]
    for shipment in shipments_of(session, request):
        boxes = sum(b.box_count for b in fba_plan.boxes_of(session, request))
        lines.append(f"• {shipment.destination_fc} ({shipment.shipment_confirmation_id or 'pending'}): "
                     f"{shipment.units_expected} units")
    lines.append("Stick one box label on each box, covering any old barcodes.")
    if request.selected_transport_choice == "own_carrier":
        lines.append(f"You're using your own carrier, so send me the tracking numbers when you have them: "
                     f"\"TRACKING {request.ref} 1Z… 1Z…\" (one per box).")
    else:
        lines.append("Amazon's carrier will collect or accept drop-off using the carrier label.")
    lines.append(f"Ready to ship: {request.ready_to_ship_date:%a %b %d}")
    return "\n".join(lines)


# ---------------- tracking, status and receiving ----------------
def shipments_of(session: Session, request: FbaInboundRequest) -> list[FbaShipment]:
    return list(session.execute(select(FbaShipment).where(FbaShipment.request_id == request.id)).scalars())


def request_tracking(session: Session, request: FbaInboundRequest, numbers: list[str], *, user) -> str:
    if request.status not in ("confirmed", "in_transit"):
        raise intake.ShipmentError(f"{request.ref} is {request.status.replace('_', ' ')}; tracking numbers "
                                   "belong on a confirmed shipment.")
    shipments = shipments_of(session, request)
    payload = {"request_ref": request.ref, "inbound_plan_id": request.inbound_plan_id,
               "numbers": numbers, "shipment_ids": [s.amazon_shipment_id for s in shipments]}
    approval = approvals.request_approval(
        session, action_type="fba_update_tracking", payload=payload,
        summary=f"📮 Send {len(numbers)} tracking number(s) for {request.ref} to Amazon:\n"
                + "\n".join(f"• {n}" for n in numbers[:20]),
        risk_level="medium", requested_by_agent_key="amazon", notify=False)
    return approvals.approval_message(approval)


@approvals.executor("fba_update_tracking")
def execute_tracking(session: Session, payload: dict, approval) -> dict:
    request = session.execute(select(FbaInboundRequest)
                              .where(FbaInboundRequest.ref == payload["request_ref"])).scalar_one()
    client = _client()
    numbers = payload["numbers"]
    for shipment_id in payload["shipment_ids"]:
        body = {"trackingDetails": {"spdTrackingDetail": {
            "spdTrackingItems": [{"trackingId": n} for n in numbers]}}}
        _await(client, client.put(f"{BASE}/inboundPlans/{payload['inbound_plan_id']}/shipments/"
                                  f"{shipment_id}/trackingDetails", json=body,
                                  operation="updateShipmentTrackingDetails"))
    for shipment in shipments_of(session, request):
        shipment.tracking_numbers = numbers
    request.status = "in_transit"
    session.flush()
    return {"summary": f"{request.ref}: {len(numbers)} tracking number(s) sent to Amazon.",
            "complete_task": False}


def sync_status(session: Session, request: FbaInboundRequest) -> str:
    """Status and received quantities, every 6 hours while a shipment is open."""
    client = _client()
    changed = []
    for shipment in shipments_of(session, request):
        detail = client.get(f"{BASE}/inboundPlans/{request.inbound_plan_id}/shipments/"
                            f"{shipment.amazon_shipment_id}", operation="getShipment")
        before = shipment.status
        shipment.status = detail.get("status") or shipment.status
        received = (detail.get("summary") or {}).get("receivedQuantity")
        if received is not None:
            shipment.units_received = int(received)
        shipment.last_synced_at = utcnow()
        if shipment.status != before:
            changed.append(f"{shipment.destination_fc}: {shipment.status.replace('_', ' ').lower()}")
    _roll_up(session, request)
    session.flush()
    return "; ".join(changed)


def _roll_up(session: Session, request: FbaInboundRequest) -> None:
    shipments = shipments_of(session, request)
    if not shipments:
        return
    statuses = {(s.status or "").upper() for s in shipments}
    if statuses <= {"CLOSED", "RECEIVING", "DELETED", "CANCELLED"} and "RECEIVING" not in statuses:
        expected = sum(s.units_expected for s in shipments)
        received = sum(s.units_received for s in shipments)
        request.status = "closed_with_discrepancy" if received < expected else "closed"
        if request.status == "closed_with_discrepancy":
            _discrepancy(session, request, expected, received)
    elif "RECEIVING" in statuses:
        request.status = "receiving"
    elif statuses & {"IN_TRANSIT", "SHIPPED"}:
        request.status = "in_transit"


def _discrepancy(session: Session, request: FbaInboundRequest, expected: int, received: int) -> None:
    from app.services.notify import notify_owner

    short = expected - received
    per_sku = []
    for item in fba_plan.items_of(session, request):
        per_sku.append(f"{item.msku}: sent {item.quantity}")
    case_text = (f"Shipment {request.ref} "
                 f"({', '.join(s.shipment_confirmation_id or '' for s in shipments_of(session, request))}) "
                 f"was received short by {short} units. We shipped {expected} units and Amazon recorded "
                 f"{received}. Please investigate and reimburse the missing units. Box contents were provided "
                 "at check-in and carrier tracking is available on request.")
    _alert(session, request, "receiving_discrepancy",
           f"{request.ref}: {short} units short ({received} of {expected} received)")
    notify_owner(session, f"📦⚠️ {request.ref}: Amazon received {received} of {expected} units — {short} short.\n"
                          + "\n".join(per_sku)
                          + "\n\nDraft case text for Seller Support:\n" + case_text, urgent=True,
                 key=f"fba_short:{request.ref}")


def _alert(session: Session, request: FbaInboundRequest, type_: str, details: str) -> None:
    from app.db.models import AmazonAlert

    session.add(AmazonAlert(type=type_, severity="high", marketplace_id=request.marketplace_id,
                            details={"message": details, "request": request.ref}, status="open",
                            alerted_at=utcnow()))
    session.flush()


# ---------------- cancelling after confirmation ----------------
def request_cancel_confirmed(session: Session, request: FbaInboundRequest, *, user) -> str:
    payload = {"request_ref": request.ref, "inbound_plan_id": request.inbound_plan_id,
               "confirm_text": request.ref}
    approval = approvals.request_approval(
        session, action_type="fba_cancel_confirmed_plan", payload=payload,
        summary=f"🛑 Cancel confirmed shipment {request.ref}\n"
                f"Committed so far: {(request.confirmed_fees or {}).get('currency', 'USD')} "
                f"{sum(float(v) for k, v in (request.confirmed_fees or {}).items() if k != 'currency'):,.2f}\n"
                "Amazon may keep placement or carrier fees already charged, and a shipment that has already "
                "shipped may not be cancellable at all. I'll check its state first.",
        risk_level="critical", requested_by_agent_key="amazon", notify=False)
    return approvals.approval_message(approval)


@approvals.executor("fba_cancel_confirmed_plan")
def execute_cancel_confirmed(session: Session, payload: dict, approval) -> dict:
    request = session.execute(select(FbaInboundRequest)
                              .where(FbaInboundRequest.ref == payload["request_ref"])).scalar_one()
    client = _client()
    states = [(s.status or "").upper() for s in shipments_of(session, request)]
    if any(state in ("SHIPPED", "IN_TRANSIT", "RECEIVING", "CLOSED") for state in states):
        raise intake.ShipmentError(f"{request.ref} has already left ({', '.join(sorted(set(states)))}), so "
                                   "Amazon won't cancel it. Nothing was changed.")
    _await(client, client.put(f"{BASE}/inboundPlans/{payload['inbound_plan_id']}/cancellation",
                              operation="cancelInboundPlan"))
    request.status = "cancelled"
    request.cancelled_at = utcnow()
    session.flush()
    audit.record(session, AGENT, "fba_confirmed_plan_cancelled", target_type="fba_request",
                 target_id=request.ref)
    return {"summary": f"{request.ref} cancelled with Amazon. Any fees already charged stay charged; check "
                       "your next statement.", "complete_task": False}
