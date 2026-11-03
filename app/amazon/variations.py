"""Variation families and deletion (brief Section 11.3 and 11.4).

A family is one change request covering several SKUs: the parent (never buyable) and its children.
The owner approves once, and each SKU's result is reported separately after execution.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon import accounts, inventory
from app.amazon import listings as listings_svc
from app.amazon import marketplaces as mkt
from app.amazon import patches as patch_svc
from app.amazon import schemas
from app.amazon import validation as v
from app.db.models import AmazonChangeRequest, AmazonListing
from app.services.common import ServiceError
from app.services.formatting import truncate

log = logging.getLogger(__name__)

THEME_ATTRIBUTES = {
    "SIZE": ["size"], "COLOR": ["color"], "SIZE_COLOR": ["size", "color"], "COLOR_SIZE": ["color", "size"],
    "FLAVOR": ["flavor"], "SCENT": ["scent"], "COUNT": ["unit_count"], "STYLE": ["style"],
}
GRID_SEPARATORS = re.compile(r"\s*[|,;\t]\s*")


class VariationError(ServiceError):
    pass


def theme_attributes(theme: str) -> list[str]:
    key = (theme or "").strip().upper().replace("-", "_").replace(" ", "_")
    if key in THEME_ATTRIBUTES:
        return THEME_ATTRIBUTES[key]
    return [part.strip().lower() for part in re.split(r"[/_]", key) if part.strip()]


def parse_grid(text: str, theme: str) -> list[dict[str, Any]]:
    """Read "SKU | Small | 24.99" lines from the owner's message into children."""
    attributes = theme_attributes(theme)
    children: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-•*").strip()
        if not line or line.lower().startswith(("sku", "child")):
            continue
        parts = [p for p in GRID_SEPARATORS.split(line) if p]
        if len(parts) < 1 + len(attributes):
            continue
        child: dict[str, Any] = {"sku": parts[0], "values": {}}
        for i, attribute in enumerate(attributes, start=1):
            child["values"][attribute] = parts[i]
        price_part = parts[1 + len(attributes)] if len(parts) > 1 + len(attributes) else None
        if price_part:
            number = re.sub(r"[^\d.]", "", price_part)
            if number:
                child["price"] = float(number)
        children.append(child)
    return children


def _child_attributes(child: dict[str, Any], *, parent_sku: str, theme: str, marketplace_id: str,
                      base: dict[str, Any]) -> dict[str, Any]:
    attributes = {k: val for k, val in base.items() if k not in ("purchasable_offer",)}
    for attribute, value in (child.get("values") or {}).items():
        attributes[attribute] = patch_svc.wrap(value, marketplace_id)
    attributes["parentage_level"] = patch_svc.wrap("child", marketplace_id)
    attributes["child_parent_sku_relationship"] = [{"child_relationship_type": "variation",
                                                    "parent_sku": parent_sku,
                                                    "marketplace_id": marketplace_id}]
    attributes["variation_theme"] = [{"name": theme, "marketplace_id": marketplace_id}]
    if child.get("price") is not None:
        attributes["purchasable_offer"] = patch_svc.price_attribute(
            child["price"], mkt.by_id(marketplace_id).currency, marketplace_id)
    return attributes


def _parent_attributes(base: dict[str, Any], *, theme: str, marketplace_id: str) -> dict[str, Any]:
    attributes = {k: val for k, val in base.items() if k not in ("purchasable_offer",)}
    attributes["parentage_level"] = patch_svc.wrap("parent", marketplace_id)
    attributes["variation_theme"] = [{"name": theme, "marketplace_id": marketplace_id}]
    return attributes


def build_family(session: Session, *, parent_sku: str, theme: str, children: list[dict[str, Any]],
                 product_type: str, marketplace_code: str, base_attributes: dict[str, Any],
                 instructions: str = "", source_message_id=None) -> AmazonChangeRequest:
    """Parent plus children in one change request, validated as a family."""
    account = accounts.require(session)
    mp = mkt.by_code(marketplace_code)
    if len(children) < 2:
        raise VariationError("A variation family needs at least two children. Send me the list as "
                             "\"SKU | value | price\" lines.")
    schema = schemas.get_schema(session, marketplace_code, product_type)
    supported = _supported_themes(schema)

    batch = [{"sku": parent_sku, "role": "parent",
              "attributes": _parent_attributes(base_attributes, theme=theme, marketplace_id=mp.marketplace_id)}]
    for child in children:
        batch.append({"sku": child["sku"], "role": "child",
                      "attributes": _child_attributes(child, parent_sku=parent_sku, theme=theme,
                                                      marketplace_id=mp.marketplace_id, base=base_attributes)})

    result = v.Result()
    v.check_variations(result, theme=theme,
                       children=[{"sku": c["sku"], "attributes": c["attributes"]} for c in batch[1:]],
                       supported_themes=supported)
    for item in batch:
        one = v.validate(session, attributes=item["attributes"], schema=schema,
                         marketplace_id=mp.marketplace_id, creating=True,
                         price=patch_svc.price_of(item["attributes"]), currency=mp.currency,
                         exemption_confirmed=True, check_identifier=False)
        for finding in one.findings:
            result.add(finding.level, f"{item['sku']}: {finding.message}", finding.field, finding.requires_pin)

    listing = _placeholder_listing(session, account, mp.marketplace_id, parent_sku, product_type)
    request = AmazonChangeRequest(
        listing_id=listing.id, marketplace_id=mp.marketplace_id, operation="create_variation_family",
        owner_instructions=instructions, source_message_id=source_message_id,
        payload={"batch": batch, "product_type": product_type, "theme": theme, "sku": parent_sku},
        diff=[{"field": f"{item['role']} {item['sku']}",
               "before": "(new)", "after": _describe(item["attributes"], theme)} for item in batch],
        local_validation=result.as_dict(), warnings=[f.message for f in result.warnings],
        requires_pin=True, risk_level="critical",
        status="needs_info" if not result.ok else "ready_for_approval")
    session.add(request)
    session.flush()
    return request


def _describe(attributes: dict[str, Any], theme: str) -> str:
    bits = [str(v.value_of(attributes, a)) for a in theme_attributes(theme) if v.value_of(attributes, a)]
    price = patch_svc.price_of(attributes)
    if price is not None:
        bits.append(f"{price:,.2f}")
    return " · ".join(bits) or "(parent)"


def _supported_themes(schema: dict[str, Any]) -> list[str] | None:
    prop = ((schema.get("properties") or {}).get("variation_theme") or {})
    items = prop.get("items") or {}
    name = (items.get("properties") or {}).get("name") or {}
    return name.get("enum") or None


def _placeholder_listing(session: Session, account, marketplace_id: str, sku: str,
                         product_type: str) -> AmazonListing:
    existing = session.execute(select(AmazonListing).where(
        AmazonListing.account_id == account.id, AmazonListing.marketplace_id == marketplace_id,
        AmazonListing.sku == sku)).scalar_one_or_none()
    if existing:
        return existing
    listing = AmazonListing(account_id=account.id, marketplace_id=marketplace_id, sku=sku,
                            product_type=product_type, parentage="parent", buyable=False,
                            status_cached="draft", currency=mkt.by_id(marketplace_id).currency)
    session.add(listing)
    session.flush()
    return listing


# ---------------- add and remove children ----------------
def add_child(session: Session, parent: AmazonListing, child: dict[str, Any], *, theme: str,
              base_attributes: dict[str, Any], instructions: str = "") -> AmazonChangeRequest:
    from app.amazon import changes as changes_svc

    account = accounts.require(session)
    attributes = _child_attributes(child, parent_sku=parent.sku, theme=theme,
                                   marketplace_id=parent.marketplace_id, base=base_attributes)
    listing = _placeholder_listing(session, account, parent.marketplace_id, child["sku"],
                                   parent.product_type or "PRODUCT")
    listing.parentage, listing.parent_listing_id, listing.variation_theme = "child", parent.id, theme
    request = AmazonChangeRequest(
        listing_id=listing.id, marketplace_id=parent.marketplace_id, operation="add_variation_child",
        owner_instructions=instructions,
        payload={"attributes": attributes, "product_type": parent.product_type or "PRODUCT",
                 "sku": child["sku"], "requirements": "LISTING"},
        diff=[{"field": f"child {child['sku']}", "before": "(new)", "after": _describe(attributes, theme)}],
        local_validation={"ok": True, "errors": [], "warnings": []}, requires_pin=False, risk_level="high",
        status="ready_for_approval")
    session.add(request)
    session.flush()
    return request


def remove_child(session: Session, child: AmazonListing, *, instructions: str = "") -> AmazonChangeRequest:
    """Detach only: the listing stays, and deleting it is a separate, PIN-protected decision."""
    from app.amazon import changes as changes_svc

    patches = [{"op": "delete", "path": "/attributes/child_parent_sku_relationship"}]
    request = AmazonChangeRequest(
        listing_id=child.id, marketplace_id=child.marketplace_id, operation="remove_variation_child",
        owner_instructions=instructions,
        payload={"patches": patches, "product_type": child.product_type or "PRODUCT", "sku": child.sku},
        diff=[{"field": "variation family", "before": f"child of {child.parent_listing_id or 'a parent'}",
               "after": "standalone listing (not deleted)"}],
        local_validation={"ok": True, "errors": [], "warnings": []},
        warnings=["Removing a child detaches it from the family. The listing and its stock stay on Amazon."],
        requires_pin=True, risk_level="critical", status="ready_for_approval")
    session.add(request)
    session.flush()
    return request


# ---------------- deletion ----------------
def build_delete(session: Session, listing: AmazonListing, *, instructions: str = "") -> AmazonChangeRequest:
    """Deleting needs the PIN and the SKU typed back, and warns about stock that stays at Amazon."""
    snap = inventory.for_sku(session, listing.sku)
    warnings = []
    if snap and (snap.fulfillable_qty or snap.inbound_qty or snap.reserved_qty):
        warnings.append(f"{listing.sku} still has {snap.fulfillable_qty} sellable, {snap.inbound_qty} inbound "
                        f"and {snap.reserved_qty} reserved units at Amazon. Deleting the listing does not remove "
                        "that inventory — it becomes stranded and keeps costing storage.")
    warnings.append("I'll keep the last version, so the listing can be recreated from it later.")
    request = AmazonChangeRequest(
        listing_id=listing.id, marketplace_id=listing.marketplace_id, operation="delete_listing",
        owner_instructions=instructions,
        payload={"delete": True, "sku": listing.sku, "product_type": listing.product_type,
                 "confirm_text": listing.sku},
        diff=[{"field": "listing", "before": truncate(listing.title_cached or listing.sku, 120),
               "after": "deleted from Amazon"}],
        local_validation={"ok": True, "errors": [], "warnings": warnings}, warnings=warnings,
        requires_pin=True, risk_level="critical", status="ready_for_approval")
    session.add(request)
    session.flush()
    return request
