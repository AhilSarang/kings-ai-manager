"""Reading the owner's shipment message and checking it before Amazon sees anything (brief 12.1–12.2).

Nothing here guesses. A missing box weight is a question, never an assumption.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon import accounts, inventory
from app.amazon import listings as listings_svc
from app.amazon import marketplaces as mkt
from app.db.models import AmazonListing, FbaInboundRequest, Product, ShipFromAddress
from app.services import settings_store
from app.services.common import ServiceError, utcnow

log = logging.getLogger(__name__)

# Amazon's small-parcel box limits (config so they can be corrected without a code change)
BOX_MAX_SIDE_IN = 25.0
BOX_TEAM_LIFT_LB = 50.0
BOX_MAX_WEIGHT_LB = 50.0
MONTHS_OF_COVER_WARN = 12

ASIN_RE = re.compile(r"\bB0[A-Z0-9]{8}\b")
QTY_RE = re.compile(r"(\d[\d,]*)\s*(?:units?|pcs?|pieces?|ct\b)", re.I)
BOXES_RE = re.compile(r"(\d+)\s*(?:box(?:es)?|cartons?|cases?)", re.I)   # "boxes?" never matched "1 box"
PER_BOX_RE = re.compile(r"(\d+)\s*(?:per|/)\s*(?:box|carton|case)", re.I)
SEP = r"(?:[x×*]|by)"      # 18x14x11, 18 × 14 × 11, 18*14*11, 18 by 14 by 11
DIMS_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*{SEP}\s*(\d+(?:\.\d+)?)\s*{SEP}\s*(\d+(?:\.\d+)?)\s*"
                     r"(?:in(?:ch(?:es)?)?)?",
                     re.I)
WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:lbs?|pounds?)\b", re.I)
PALLET_RE = re.compile(r"\b(pallets?|ltl|freight)\b", re.I)


class ShipmentError(ServiceError):
    pass


@dataclass
class ParsedItem:
    asin: str | None = None
    sku: str | None = None
    quantity: int | None = None
    boxes: int | None = None
    units_per_box: int | None = None
    dimensions: tuple[float, float, float] | None = None
    weight_lb: float | None = None
    expiration_date: date | None = None      # Amazon demands one for dated goods (FBA_INB_0180)
    lot_code: str | None = None


@dataclass
class ParsedShipment:
    items: list[ParsedItem] = field(default_factory=list)
    ready_date: date | None = None
    shipping_mode: str | None = None
    questions: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.questions and bool(self.items)


def _int(text: str) -> int:
    return int(text.replace(",", ""))


def parse(text: str) -> ParsedShipment:
    """One line per product, or one product across the whole message."""
    shipment = ParsedShipment()
    body = text or ""
    shipment.shipping_mode = "ltl_pallet" if PALLET_RE.search(body) else None
    shipment.ready_date = _ready_date(body)

    segments = _segments(body)
    for segment in segments:
        item = ParsedItem()
        asin = ASIN_RE.search(segment)
        item.asin = asin.group(0) if asin else None
        sku = re.search(r"\bsku[:\s]+([\w.\-/]+)", segment, re.I)
        item.sku = sku.group(1) if sku else None
        qty = QTY_RE.search(segment)
        item.quantity = _int(qty.group(1)) if qty else None
        boxes = BOXES_RE.search(segment)
        item.boxes = _int(boxes.group(1)) if boxes else None
        per_box = PER_BOX_RE.search(segment)
        item.units_per_box = _int(per_box.group(1)) if per_box else None
        dims = DIMS_RE.search(segment)
        item.dimensions = tuple(float(d) for d in dims.groups()) if dims else None
        expiry = EXPIRY_RE.search(segment)
        item.expiration_date = _date_in(expiry.group(1)) if expiry else None
        lot = LOT_RE.search(segment)
        item.lot_code = lot.group(1) if lot else None
        weight = WEIGHT_RE.search(segment)
        item.weight_lb = float(weight.group(1)) if weight else None
        if item.asin or item.sku:
            shipment.items.append(item)

    if len(shipment.items) == 1:
        _fill_from_whole_message(shipment.items[0], body)

    for item in shipment.items:
        ask_for_missing(shipment, item)
    if not shipment.items:
        shipment.questions.append("Which product? Send the ASIN or SKU, the quantity, the number of boxes, "
                                  "the box size and the box weight.")
    return shipment


def ask_for_missing(shipment: ParsedShipment, item: ParsedItem) -> None:
    """Everything Amazon needs about a box that the owner hasn't told us. Never guessed, always asked."""
    label = item.asin or item.sku or "that product"
    if item.quantity is None:
        shipment.questions.append(f"How many units of {label}?")
    if item.boxes is None:
        shipment.questions.append(f"How many boxes for {label}?")
    if item.dimensions is None:
        shipment.questions.append(f"What size are the boxes for {label}? (length x width x height, inches)")
    if item.weight_lb is None:
        shipment.questions.append(f"How much does each box of {label} weigh, in pounds? Amazon needs it and "
                                  "I won't guess.")
    if item.units_per_box is None and item.quantity and item.boxes and item.quantity % item.boxes != 0:
        shipment.questions.append(f"{item.quantity} units of {label} don't divide evenly into {item.boxes} "
                                  "boxes. How many units are in each box?")


def _segments(body: str) -> list[str]:
    """One segment per product: lines first, then split again where a line names several ASINs."""
    out = []
    for line in re.split(r"[\n;]+", body):
        if not (ASIN_RE.search(line) or re.search(r"\bsku\b", line, re.I)):
            continue
        starts = [m.start() for m in ASIN_RE.finditer(line)]
        if len(starts) <= 1:
            out.append(line)
            continue
        bounds = starts + [len(line)]
        out += [line[bounds[i]:bounds[i + 1]] for i in range(len(starts))]
    return out or ([body] if ASIN_RE.search(body) else [])


def _date_in(text: str) -> date | None:
    """A date on its own, with no "today"/"Monday" guessing — an expiry is never relative."""
    return _ready_date(text) if re.search(r"\d", text or "") else None


def _fill_from_whole_message(item: ParsedItem, body: str) -> None:
    """With a single product, an answer like "32 lb each" on its own line still belongs to it."""
    if item.quantity is None and (found := QTY_RE.search(body)):
        item.quantity = _int(found.group(1))
    if item.boxes is None and (found := BOXES_RE.search(body)):
        item.boxes = _int(found.group(1))
    if item.units_per_box is None and (found := PER_BOX_RE.search(body)):
        item.units_per_box = _int(found.group(1))
    if item.dimensions is None and (found := DIMS_RE.search(body)):
        item.dimensions = tuple(float(d) for d in found.groups())
    if item.weight_lb is None and (found := WEIGHT_RE.search(body)):
        item.weight_lb = float(found.group(1))
    if item.expiration_date is None and (found := EXPIRY_RE.search(body)):
        item.expiration_date = _date_in(found.group(1))
    if item.lot_code is None and (found := LOT_RE.search(body)):
        item.lot_code = found.group(1)


EXPIRY_RE = re.compile(r"\b(?:exp(?:iry|ires?|iration)?|best\s*before|bb)\b[^\n]{0,20}?"
                       r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|"
                       r"[a-z]{3,9}\.?\s+\d{1,2},?\s*\d{2,4}|\d{1,2}\s+[a-z]{3,9}\.?\s*\d{2,4})", re.I)
LOT_RE = re.compile(r"\blot\b[^\n]{0,10}?([A-Z0-9][\w.-]{2,30})", re.I)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _ready_date(text: str) -> date | None:
    today = utcnow().date()
    lowered = (text or "").lower()
    if "today" in lowered:
        return today
    if "tomorrow" in lowered:
        return today + timedelta(days=1)
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, day in enumerate(weekdays):
        if re.search(rf"\b{day}\b", lowered):
            ahead = (i - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text or "")
    if iso:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    # "sep 25", "25 september", "september 25th", with the year taken literally when he gives one
    names = "|".join(MONTHS)
    month = day = year = None
    first = re.search(rf"\b({names})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?\b", lowered)
    if first:
        month, day, year = first.group(1), int(first.group(2)), first.group(3)
    else:
        second = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({names})[a-z]*\.?(?:,?\s*(\d{{4}}))?\b", lowered)
        if second:
            month, day, year = second.group(2), int(second.group(1)), second.group(3)
    if month:
        number = MONTHS.index(month[:3]) + 1
        if year:
            return date(int(year), number, day)
        return date(today.year + (1 if (number, day) < (today.month, today.day) else 0), number, day)

    # "9/25" or "9/25/2026", read the American way because that is how the owner writes them
    slash = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text or "")
    if slash:
        number, day = int(slash.group(1)), int(slash.group(2))
        year = int(slash.group(3) or 0)
        year = (2000 + year if year and year < 100 else year) or (
            today.year + (1 if (number, day) < (today.month, today.day) else 0))
        return date(year, number, day)
    return None


def next_business_day(from_date: date | None = None) -> date:
    day = (from_date or utcnow().date()) + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


# ---------------- pre-checks ----------------
@dataclass
class Check:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    resolved: list[dict[str, Any]] = field(default_factory=list)
    prep_needed: list[str] = field(default_factory=list)   # SKUs the prep question is about

    @property
    def ok(self) -> bool:
        return not self.errors and not self.questions


OWNER_FROM_CONSTRAINT = {"NONE_ONLY": "NONE", "SELLER_ONLY": "SELLER", "AMAZON_ONLY": "AMAZON"}


def prep_details(marketplace_id: str, mskus: list[str]) -> dict[str, dict[str, Any]]:
    """What Amazon says each SKU needs: who labels it, who preps it, and which prep.

    Assuming SELLER labels everything is wrong for stickerless SKUs and Amazon rejects the whole
    plan, so we ask instead of guessing. A failure here is not fatal — the caller falls back.
    """
    from app.amazon import api

    if not mskus:
        return {}
    try:
        response = api.client().get("/inbound/fba/2024-03-20/items/prepDetails",
                                    params={"marketplaceId": marketplace_id, "mskus": ",".join(mskus[:100])},
                                    operation="listPrepDetails")
    except Exception as exc:  # noqa: BLE001 — a hiccup here must not lose the shipment
        log.warning("could not read prep details", extra={"error": str(exc)[:160]})
        return {}
    return {d.get("msku"): d for d in (response.get("mskuPrepDetails") or []) if d.get("msku")}


def resolve_sku(session: Session, item: ParsedItem, marketplace_id: str) -> tuple[AmazonListing | None, str | None]:
    """ASIN → our FBA SKU. Ambiguity is a question, never a guess."""
    if item.sku:
        rows = listings_svc.find(session, sku=item.sku, marketplace_id=marketplace_id)
        return (rows[0], None) if rows else (None, f"I don't have a listing for SKU {item.sku}.")
    rows = [r for r in listings_svc.find(session, asin=item.asin, marketplace_id=marketplace_id)]
    if not rows:
        return None, (f"{item.asin} isn't one of our listings in this marketplace. If it should be, tell me to "
                      "add an offer for it first.")
    fba = [r for r in rows if r.fulfillment == "FBA"]
    if not fba:
        return None, (f"{item.asin} is listed but not set to FBA, so Amazon won't accept a shipment for it. "
                      "Tell me to convert it to FBA and I'll draft that change.")
    if len(fba) > 1:
        return None, (f"{item.asin} maps to {len(fba)} of our SKUs ({', '.join(r.sku for r in fba)}). "
                      "Which one are you sending?")
    return fba[0], None


def check_boxes(item: ParsedItem, shipping_mode: str) -> tuple[list[str], list[str]]:
    """Amazon's box rules. Over-limit boxes are blocked, team-lift boxes need the owner to know."""
    errors, warnings = [], []
    if shipping_mode != "small_parcel" or not item.dimensions:
        return errors, warnings
    longest = max(item.dimensions)
    if longest > BOX_MAX_SIDE_IN:
        errors.append(f"A box side of {longest:g} in is over Amazon's {BOX_MAX_SIDE_IN:g} in limit for standard "
                      "boxes. Split it into smaller boxes, or tell me it holds a single oversize unit.")
    if item.weight_lb and item.weight_lb > BOX_MAX_WEIGHT_LB:
        errors.append(f"A box at {item.weight_lb:g} lb is over Amazon's {BOX_MAX_WEIGHT_LB:g} lb limit unless it "
                      "holds one oversize item. Repack it lighter, or confirm it's a single oversize unit.")
    elif item.weight_lb and item.weight_lb > BOX_TEAM_LIFT_LB - 5:
        warnings.append(f"At {item.weight_lb:g} lb these boxes are close to the {BOX_TEAM_LIFT_LB:g} lb limit; "
                        "over it they need a \"Team Lift\" label.")
    return errors, warnings


def precheck(session: Session, parsed: ParsedShipment, *, marketplace_code: str = "US") -> Check:
    """Everything that can be caught locally, before we create anything in Amazon."""
    check = Check()
    mp = mkt.by_code(marketplace_code)
    accounts.require(session)
    mode = parsed.shipping_mode or settings_store.get(session, "fba_default_shipping_mode", "small_parcel")

    if parsed.ready_date is None:
        check.questions.append("When will the boxes be ready to leave? Amazon holds you to this date, so tell "
                               "me a day (\"Monday\", \"next Tuesday\") or a date and I won't guess it.")

    address = session.execute(select(ShipFromAddress).where(ShipFromAddress.is_default.is_(True))
                              ).scalar_one_or_none()
    if address is None:
        check.errors.append("No ship-from address is set up.")
    elif not (address.phone and address.email):
        check.questions.append("Amazon needs a phone number and email address on the ship-from address. "
                               "What should I use for the Richmond warehouse?")

    resolved: list[tuple[ParsedItem, AmazonListing]] = []
    for item in parsed.items:
        listing, problem = resolve_sku(session, item, mp.marketplace_id)
        if problem:
            check.errors.append(problem)
            continue
        resolved.append((item, listing))
    details = prep_details(mp.marketplace_id, [row[1].sku for row in resolved])

    for item, listing in resolved:
        if not listing.buyable:
            check.warnings.append(f"{listing.sku} isn't buyable right now"
                                  + (f" ({listing.issues_cached[0].get('message')})" if listing.issues_cached
                                     else "") + ". Amazon may still receive the stock.")
        errors, warnings = check_boxes(item, mode)
        check.errors += errors
        check.warnings += warnings

        units_per_box = item.units_per_box
        if units_per_box is None and item.quantity and item.boxes and item.quantity % item.boxes == 0:
            units_per_box = item.quantity // item.boxes
        if units_per_box and item.boxes and item.quantity and units_per_box * item.boxes != item.quantity:
            check.errors.append(f"The arithmetic doesn't work for {listing.sku}: {item.boxes} boxes × "
                                f"{units_per_box} per box is {item.boxes * units_per_box}, not {item.quantity}.")

        product = session.get(Product, listing.product_id) if listing.product_id else None
        detail = details.get(listing.sku) or {}
        label_owner = OWNER_FROM_CONSTRAINT.get(detail.get("labelOwnerConstraint") or "")
        prep_owner = OWNER_FROM_CONSTRAINT.get(detail.get("prepOwnerConstraint") or "")
        amazon_prep = detail.get("prepCategory")          # Amazon often already knows; don't ask twice
        if amazon_prep and (product is None or not product.prep_category):
            check.notes.append(f"Amazon says {listing.sku} needs {_prep_words(detail)}.")
        if not amazon_prep and (product is None or not product.prep_category):
            check.prep_needed.append(listing.sku)
            check.questions.append(f"What prep does {listing.sku} need? Reply \"no prep\", \"poly bag\", "
                                   "\"bubble wrap\", \"labeling only\" or tell me what Amazon asks for.")
        cover = _months_of_cover(session, listing.sku, item.quantity or 0)
        if cover is not None and cover > MONTHS_OF_COVER_WARN:
            check.warnings.append(f"{item.quantity} units of {listing.sku} is about {cover:.0f} months of stock "
                                  "at the last 30 days' rate. That's a lot of storage fees.")
        check.resolved.append({"listing": listing, "item": item, "units_per_box": units_per_box,
                               "prep_category": (product.prep_category if product else None) or amazon_prep,
                               "label_owner": label_owner, "prep_owner": prep_owner})
    return check


def _prep_words(detail: dict[str, Any]) -> str:
    types = [t.replace("ITEM_", "").replace("_", " ").lower() for t in (detail.get("prepTypes") or [])]
    return ", ".join(types) or (detail.get("prepCategory") or "no prep").lower()


def _months_of_cover(session: Session, sku: str, incoming: int) -> float | None:
    velocity = inventory.daily_velocity(session, sku)
    if velocity <= 0:
        return None
    snap = inventory.for_sku(session, sku)
    on_hand = (snap.fulfillable_qty + snap.inbound_qty) if snap else 0
    return (on_hand + incoming) / velocity / 30


def questions_message(check: Check, parsed: ParsedShipment) -> str:
    lines = ["Before I can build this shipment I need:"]
    lines += [f"• {q}" for q in dict.fromkeys(parsed.questions + check.questions)]
    lines.append("Reply with the answers and I'll carry on.")
    return "\n".join(lines)


def problems_message(check: Check) -> str:
    lines = ["I can't send this to Amazon yet:"]
    lines += [f"⛔ {e}" for e in check.errors]
    if check.warnings:
        lines += [f"⚠️ {w}" for w in check.warnings]
    return "\n".join(lines)


def summary_message(request: FbaInboundRequest, check: Check) -> str:
    lines = [f"📦 Shipment {request.ref} — checks passed"]
    for row in check.resolved:
        item, listing = row["item"], row["listing"]
        lines.append(f"• {listing.sku} ({item.asin or listing.asin}): {item.quantity} units in {item.boxes} "
                     f"boxes of {row['units_per_box']}, {item.dimensions[0]:g}×{item.dimensions[1]:g}×"
                     f"{item.dimensions[2]:g} in, {item.weight_lb:g} lb each")
    lines.append(f"Ready to leave: {request.ready_to_ship_date:%a %b %d} · "
                 + ("boxes (small parcel)" if request.shipping_mode == "small_parcel" else "pallets (LTL)"))
    for note in check.notes:
        lines.append(note)
    for warning in check.warnings:
        lines.append(f"⚠️ {warning}")
    lines.append("Asking Amazon for warehouse options now; no fees are committed yet.")
    return "\n".join(lines)
