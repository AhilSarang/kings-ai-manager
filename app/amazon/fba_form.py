"""The shipment form (owner's design, 2026-09-18).

One message with every field, filled in and sent back, instead of a question at a time. The agent
does everything else — box contents go to Amazon through the API, so no spreadsheet is ever needed
and no manual-processing fee applies.
"""

from __future__ import annotations

import re

from app.amazon import fba_intake as intake

FORM = """📦 New FBA shipment — copy this, fill it in and send it back:

SKU or ASIN:
Units:
Boxes:
Units per box:
Box size (L x W x H, inches):
Box weight (lb):
Ship date:
Expiry date (dated goods only):
Delivery window:
Prep:

Leave the last two blank if you don't mind — I'll take the soonest window Amazon offers and ask it
what prep the SKU needs. Sending several products? Repeat the first six lines for each one."""

# label -> the field it fills
FIELDS = {
    "sku or asin": "sku", "sku": "sku", "asin": "sku", "sku and asin": "sku",
    "units": "quantity", "number of units": "quantity", "qty": "quantity", "quantity": "quantity",
    "boxes": "boxes", "number of boxes": "boxes", "box count": "boxes",
    "units per box": "units_per_box", "per box": "units_per_box",
    "box size": "dimensions", "box size (l x w x h, inches)": "dimensions", "box dimensions": "dimensions",
    "dimensions": "dimensions", "size": "dimensions",
    "box weight": "weight_lb", "box weight (lb)": "weight_lb", "weight": "weight_lb",
    "weight of box": "weight_lb",
    "ship date": "ready_date", "ready date": "ready_date", "date": "ready_date",
    "expiry date": "expiry", "expiry": "expiry", "expiration": "expiry", "expiration date": "expiry",
    "best before": "expiry", "lot": "lot", "lot code": "lot", "batch": "lot",
    "delivery window": "delivery_window", "window": "delivery_window",
    "prep": "prep", "prep needed": "prep", "prep needed (yes or no)": "prep",
}
BLANK = {"", "-", "n/a", "na", "none", "tbd", "?"}
LINE = re.compile(r"^\s*[•\-*]?\s*([A-Za-z][A-Za-z ()/,.]*?)\s*[:=]\s*(.*)$")


def looks_like_form(text: str) -> bool:
    """Two or more of our labels with values after them: this is a filled-in form."""
    return len(_lines(text)) >= 2


def _lines(text: str) -> list[tuple[str, str]]:
    out = []
    for line in (text or "").splitlines():
        match = LINE.match(line)
        if not match:
            continue
        label = re.sub(r"\s+", " ", match.group(1).strip().lower()).strip(" .")
        label = re.sub(r"\s*\([^)]*\)", "", label).strip()       # drop "(lb)", "(yes or no)"
        if label in FIELDS:
            out.append((FIELDS[label], match.group(2).strip()))
    return out


def parse(text: str) -> tuple[intake.ParsedShipment, str, str]:
    """A filled-in form → the same shipment the message parser produces, plus his two preferences."""
    shipment = intake.ParsedShipment()
    item = intake.ParsedItem()
    delivery_window, prep = "", ""

    for field, value in _lines(text):
        if value.lower() in BLANK:
            continue
        if field == "sku" and item.sku or field == "sku" and item.asin:
            shipment.items.append(item)                          # a new product block starts
            item = intake.ParsedItem()
        if field == "sku":
            asin = intake.ASIN_RE.search(value)
            item.asin = asin.group(0) if asin else None
            rest = intake.ASIN_RE.sub("", value).strip(" ,;/|")
            item.sku = rest or None
        elif field == "quantity":
            item.quantity = _number(value)
        elif field == "boxes":
            item.boxes = _number(value)
        elif field == "units_per_box":
            item.units_per_box = _number(value)
        elif field == "dimensions":
            dims = intake.DIMS_RE.search(value)
            item.dimensions = tuple(float(d) for d in dims.groups()) if dims else None
        elif field == "weight_lb":
            weight = re.search(r"(\d+(?:\.\d+)?)", value)
            item.weight_lb = float(weight.group(1)) if weight else None
        elif field == "ready_date":
            shipment.ready_date = intake._ready_date(value)
        elif field == "expiry":
            item.expiration_date = intake._date_in(value)
        elif field == "lot":
            item.lot_code = value
        elif field == "delivery_window":
            delivery_window = value
        elif field == "prep":
            prep = value

    if item.sku or item.asin or item.quantity:
        shipment.items.append(item)
    shipment.shipping_mode = "ltl_pallet" if intake.PALLET_RE.search(text or "") else None
    for entry in shipment.items:
        intake.ask_for_missing(shipment, entry)
    if not shipment.items:
        shipment.questions.append("The form came back empty. Send it again with the SKU, units and boxes "
                                  "filled in.")
    return shipment, delivery_window, prep


def _number(value: str) -> int | None:
    found = re.search(r"(\d[\d,]*)", value or "")
    return int(found.group(1).replace(",", "")) if found else None
