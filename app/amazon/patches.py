"""Building JSON Patch bodies for the Listings Items API, and diffs the owner can read."""

from __future__ import annotations

from typing import Any

from app.amazon.validation import value_of, values_of

PRICE_ATTRIBUTE = "purchasable_offer"


def wrap(value: Any, marketplace_id: str) -> list[dict[str, Any]]:
    """Amazon attributes are lists of {value, marketplace_id}."""
    values = value if isinstance(value, list) else [value]
    out = []
    for v in values:
        if isinstance(v, dict) and ("value" in v or "marketplace_id" in v):
            out.append({**v, "marketplace_id": v.get("marketplace_id", marketplace_id)})
        else:
            out.append({"value": v, "marketplace_id": marketplace_id})
    return out


def price_attribute(price: float, currency: str, marketplace_id: str) -> list[dict[str, Any]]:
    return [{"marketplace_id": marketplace_id, "currency": currency,
             "our_price": [{"schedule": [{"value_with_tax": round(float(price), 2)}]}]}]


def price_of(attributes: dict[str, Any]) -> float | None:
    for offer in attributes.get(PRICE_ATTRIBUTE) or []:
        for our in (offer.get("our_price") or []):
            for entry in (our.get("schedule") or []):
                if entry.get("value_with_tax") is not None:
                    return float(entry["value_with_tax"])
    return None


def build_patches(changes: dict[str, Any], marketplace_id: str, *, current: dict[str, Any] | None = None
                  ) -> list[dict[str, Any]]:
    """One JSON Patch operation per attribute the owner asked to change — nothing else is touched."""
    patches = []
    for key, value in changes.items():
        path = f"/attributes/{key}"
        if value is None:
            patches.append({"op": "delete", "path": path})
            continue
        op = "replace" if (current or {}).get(key) is not None else "add"
        patches.append({"op": op, "path": path,
                        "value": value if key == PRICE_ATTRIBUTE else wrap(value, marketplace_id)})
    return patches


def patch_body(product_type: str, patches: list[dict[str, Any]]) -> dict[str, Any]:
    return {"productType": product_type, "patches": patches}


def put_body(product_type: str, attributes: dict[str, Any], *, requirements: str = "LISTING") -> dict[str, Any]:
    return {"productType": product_type, "requirements": requirements, "attributes": attributes}


def apply_patches(attributes: dict[str, Any], patches: list[dict[str, Any]]) -> dict[str, Any]:
    """What the listing will look like after Amazon applies the patch (for local validation)."""
    merged = {k: v for k, v in (attributes or {}).items()}
    for patch in patches:
        key = patch["path"].split("/attributes/")[-1]
        if patch["op"] == "delete":
            merged.pop(key, None)
        else:
            merged[key] = patch["value"]
    return merged


def _readable(attributes: dict[str, Any], key: str) -> str:
    if key == PRICE_ATTRIBUTE:
        price = price_of(attributes)
        return f"{price:,.2f}" if price is not None else "(none)"
    values = [str(v) for v in values_of(attributes, key) if v not in (None, "")]
    if not values:
        return "(none)"
    return " | ".join(values) if len(values) > 1 else values[0]


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    """Field-by-field change list, values as the owner would read them."""
    out = []
    for key in sorted(set(before) | set(after)):
        b, a = _readable(before, key), _readable(after, key)
        if b != a:
            out.append({"field": key, "before": b, "after": a})
    return out


def touched_fields(patches: list[dict[str, Any]]) -> list[str]:
    return sorted({p["path"].split("/attributes/")[-1] for p in patches})


def unchanged_since(before: dict[str, Any], now: dict[str, Any], fields: list[str]) -> list[str]:
    """Which of these fields somebody else changed while the approval was waiting."""
    return [f for f in fields if _readable(before, f) != _readable(now, f)]
