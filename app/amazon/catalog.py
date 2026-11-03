"""Catalog lookups: the duplicate check before creating an ASIN, and searches by identifier."""

from __future__ import annotations

from typing import Any

from app.amazon import api
from app.amazon import marketplaces as mkt

IDENTIFIER_TYPES = {"UPC": 12, "EAN": 13, "GTIN": 14, "ISBN": 13, "ASIN": 10}
INCLUDED = "identifiers,summaries,attributes,productTypes,salesRanks"


def check_digit_ok(code: str) -> bool:
    """UPC/EAN/GTIN check digit (GS1 modulo 10)."""
    digits = [int(c) for c in (code or "").strip() if c.isdigit()]
    if len(digits) not in (8, 12, 13, 14) or len(digits) != len((code or "").strip()):
        return False
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))  # weights run right to left
    return (10 - total % 10) % 10 == check


def identifier_type(code: str) -> str | None:
    code = (code or "").strip()
    if not code.isdigit():
        return "ASIN" if len(code) == 10 else None
    return {12: "UPC", 13: "EAN", 14: "GTIN", 8: "GTIN"}.get(len(code))


def search_by_identifier(marketplace_code: str, code: str) -> list[dict[str, Any]]:
    mp = mkt.by_code(marketplace_code)
    kind = identifier_type(code)
    if kind is None:
        return []
    if kind == "ASIN":
        params = {"marketplaceIds": mp.marketplace_id, "identifiers": code, "identifiersType": "ASIN",
                  "includedData": INCLUDED}
    else:
        params = {"marketplaceIds": mp.marketplace_id, "identifiers": code.strip(),
                  "identifiersType": kind, "includedData": INCLUDED}
    resp = api.client().get("/catalog/2022-04-01/items", params=params, operation="searchCatalogItems")
    return resp.get("items") or []


def search_by_keywords(marketplace_code: str, keywords: str, *, brand: str | None = None,
                       limit: int = 10) -> list[dict[str, Any]]:
    mp = mkt.by_code(marketplace_code)
    params: dict[str, Any] = {"marketplaceIds": mp.marketplace_id, "keywords": keywords.strip(),
                              "includedData": INCLUDED, "pageSize": min(limit, 20)}
    if brand:
        params["brandNames"] = brand
    resp = api.client().get("/catalog/2022-04-01/items", params=params, operation="searchCatalogItems")
    return resp.get("items") or []


def title_of(item: dict[str, Any], marketplace_id: str) -> str:
    for summary in item.get("summaries") or []:
        if summary.get("marketplaceId") in (marketplace_id, None):
            return summary.get("itemName") or ""
    return ""
