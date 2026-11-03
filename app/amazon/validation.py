"""Listing validation (brief Section 7): every local check, then Amazon's own preview.

Nothing reaches Amazon before this passes. Messages are written for the owner, not for a developer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from app.amazon import marketplaces as mkt
from app.amazon.catalog import check_digit_ok, identifier_type
from app.db.models import AmazonListing, Brand, Product
from app.services import settings_store

OWN_BRANDS = ("skyzz", "vibezy")

# Amazon's title policy: these characters aren't allowed unless they're part of the brand name.
TITLE_BANNED_CHARS = set("!$?_{}^¬¦")
TITLE_MAX_DEFAULT = 200
BULLET_MAX_DEFAULT = 500
DESCRIPTION_MAX_DEFAULT = 2000
SEARCH_TERMS_MAX_BYTES = 250
SMALL_WORDS = {"a", "an", "the", "and", "or", "for", "with", "of", "in", "on", "to", "by", "at", "from"}

PROMOTIONAL = re.compile(
    r"\b(free shipping|best seller|bestseller|#1|top rated|sale|discount|cheap|lowest price|"
    r"money back|guarantee[d]?|limited time|new arrival|hot deal|buy now|order now)\b", re.I)
CONTACT_OR_URL = re.compile(r"(https?://|www\.|\b[\w.+-]+@[\w-]+\.[\w.]+\b|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b)", re.I)
DISALLOWED_HTML = re.compile(r"<\s*(script|iframe|style|img|a|form|input|object|embed)\b", re.I)

CLAIMS = [
    (re.compile(r"\b(cure|cures|treat|treats|heal|heals|prevent|prevents)\b.{0,30}"
                r"\b(disease|cancer|covid|flu|virus|infection|illness)\b", re.I),
     "medical claim (curing or treating a disease)"),
    (re.compile(r"\bfda[- ](approved|cleared|certified)\b", re.I), "FDA approval claim"),
    (re.compile(r"\b(antibacterial|antimicrobial|kills? (99[.\d]*%? of )?germs|sanitiz\w+|disinfect\w+)\b", re.I),
     "antibacterial or germ-killing claim"),
    (re.compile(r"\bnon[- ]?toxic\b", re.I), "\"non-toxic\" claim"),
    (re.compile(r"\b(eco[- ]?friendly|biodegradable|compostable|sustainable|carbon[- ]neutral|green product)\b", re.I),
     "environmental claim that may need substantiation"),
    (re.compile(r"\b(cbd|thc|cannabidiol|hemp extract)\b", re.I), "CBD/THC reference"),
    (re.compile(r"\b(better than|unlike|compared to|vs\.?)\s+[A-Z][\w']+", re.I), "comparison with another brand"),
    (re.compile(r"\b(100%|completely|totally)\s+(safe|effective|natural)\b", re.I), "absolute safety claim"),
]


@dataclass
class Finding:
    level: str          # "error" | "warning"
    message: str
    field: str | None = None
    requires_pin: bool = False


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, message: str, field_: str | None = None, requires_pin: bool = False) -> None:
        self.findings.append(Finding(level, message, field_, requires_pin))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def requires_pin(self) -> bool:
        return any(f.requires_pin for f in self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "requires_pin": self.requires_pin,
                "errors": [f.message for f in self.errors], "warnings": [f.message for f in self.warnings]}


# ---------------- helpers ----------------
def value_of(attributes: dict[str, Any], key: str) -> Any:
    """Amazon attributes are lists of {value: …}; give me the plain value."""
    raw = attributes.get(key)
    if isinstance(raw, list) and raw:
        first = raw[0]
        return first.get("value") if isinstance(first, dict) else first
    return raw


def values_of(attributes: dict[str, Any], key: str) -> list[Any]:
    raw = attributes.get(key)
    if not isinstance(raw, list):
        return [raw] if raw is not None else []
    return [v.get("value") if isinstance(v, dict) else v for v in raw]


def _max_length(schema: dict[str, Any], key: str, default: int) -> int:
    prop = ((schema.get("properties") or {}).get(key) or {})
    items = prop.get("items") or {}
    for place in (items.get("properties", {}).get("value", {}), items, prop):
        if isinstance(place, dict) and isinstance(place.get("maxLength"), int):
            return place["maxLength"]
    return default


# ---------------- individual checks ----------------
def check_schema(result: Result, schema: dict[str, Any], attributes: dict[str, Any]) -> None:
    if not schema:
        return
    validator = Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(attributes), key=lambda e: list(e.path))[:10]:
        field_ = ".".join(str(p) for p in error.path) or (error.validator_value if error.validator == "required"
                                                          else None)
        if error.validator == "required":
            missing = re.findall(r"'([^']+)' is a required property", error.message)
            name = missing[0] if missing else "a required field"
            result.add("error", f"Amazon requires {friendly(name)} for this product type.", name)
        else:
            result.add("error", f"{friendly(str(field_) or 'A field')} doesn't match what Amazon expects: "
                                f"{error.message[:160]}", str(field_))


def friendly(name: str) -> str:
    return (str(name).replace("_", " ")
            .replace("item name", "the title")
            .replace("bullet point", "bullet points")
            .replace("product description", "the description")
            .replace("list price", "the price"))


def check_title(result: Result, attributes: dict[str, Any], schema: dict[str, Any], brand: str | None) -> None:
    title = (value_of(attributes, "item_name") or "").strip()
    if not title:
        return
    limit = _max_length(schema, "item_name", TITLE_MAX_DEFAULT)
    if len(title) > limit:
        result.add("error", f"The title is {len(title)} characters; Amazon allows {limit} here.", "item_name")
    brand_words = set((brand or "").lower().split())
    bad = {c for c in title if c in TITLE_BANNED_CHARS}
    bad -= {c for c in bad if brand and c in brand}
    if bad:
        result.add("error", f"The title contains {' '.join(sorted(bad))}, which Amazon doesn't allow in titles.",
                   "item_name")
    if PROMOTIONAL.search(title):
        result.add("error", "The title contains promotional wording, which Amazon rejects "
                            f"(\"{PROMOTIONAL.search(title).group(0)}\").", "item_name")
    counts: dict[str, int] = {}
    for word in re.findall(r"[a-z0-9']+", title.lower()):
        if word in SMALL_WORDS or word in brand_words:
            continue
        counts[word] = counts.get(word, 0) + 1
    repeated = [w for w, n in counts.items() if n > 2]
    if repeated:
        result.add("warning", f"The title repeats {', '.join(repeated)} more than twice.", "item_name")


def check_bullets_and_description(result: Result, attributes: dict[str, Any], schema: dict[str, Any]) -> None:
    bullets = [b for b in values_of(attributes, "bullet_point") if b]
    limit = _max_length(schema, "bullet_point", BULLET_MAX_DEFAULT)
    for i, bullet in enumerate(bullets, start=1):
        if len(bullet) > limit:
            result.add("error", f"Bullet {i} is {len(bullet)} characters; the limit is {limit}.", "bullet_point")
        if CONTACT_OR_URL.search(bullet):
            result.add("error", f"Bullet {i} contains a link, email address or phone number, which Amazon "
                                "doesn't allow.", "bullet_point")
    description = value_of(attributes, "product_description") or ""
    dlimit = _max_length(schema, "product_description", DESCRIPTION_MAX_DEFAULT)
    if len(description) > dlimit:
        result.add("error", f"The description is {len(description)} characters; the limit is {dlimit}.",
                   "product_description")
    if DISALLOWED_HTML.search(description):
        result.add("error", "The description contains HTML Amazon doesn't allow (links, images or scripts).",
                   "product_description")
    if CONTACT_OR_URL.search(description):
        result.add("error", "The description contains a link, email address or phone number.",
                   "product_description")


def check_claims(result: Result, attributes: dict[str, Any]) -> None:
    text = " ".join(str(v) for key in ("item_name", "bullet_point", "product_description", "generic_keyword")
                    for v in values_of(attributes, key) if v)
    for pattern, label in CLAIMS:
        found = pattern.search(text)
        if found:
            result.add("warning", f"\"{found.group(0)}\" is a {label}; Amazon may ask you to prove it.")


def check_search_terms(result: Result, attributes: dict[str, Any], brand: str | None) -> None:
    terms = " ".join(str(v) for v in values_of(attributes, "generic_keyword") if v)
    if not terms:
        return
    size = len(terms.encode("utf-8"))
    if size > SEARCH_TERMS_MAX_BYTES:
        result.add("error", f"Search terms are {size} bytes; Amazon's limit is {SEARCH_TERMS_MAX_BYTES}.",
                   "generic_keyword")
    if re.search(r"\bB0[A-Z0-9]{8}\b", terms):
        result.add("error", "Search terms contain an ASIN, which Amazon doesn't allow.", "generic_keyword")
    title_words = {w for w in re.findall(r"[a-z0-9']+", (value_of(attributes, "item_name") or "").lower())
                   if w not in SMALL_WORDS}
    repeated = sorted(title_words & set(re.findall(r"[a-z0-9']+", terms.lower())))
    if repeated:
        result.add("warning", f"Search terms repeat words already in the title ({', '.join(repeated[:5])}); "
                              "that space is wasted.", "generic_keyword")
    for other in ("nike", "adidas", "apple", "samsung", "3m", "gucci", "disney"):
        if re.search(rf"\b{other}\b", terms, re.I):
            result.add("error", f"Search terms mention another brand ({other}).", "generic_keyword")


def check_identifiers(result: Result, attributes: dict[str, Any], *, exemption_confirmed: bool = False) -> None:
    codes = [str(v) for v in values_of(attributes, "externally_assigned_product_identifier") if v]
    codes += [str(v) for key in ("upc", "ean", "gtin") for v in values_of(attributes, key) if v]
    for code in codes:
        kind = identifier_type(code)
        if kind is None:
            result.add("error", f"\"{code}\" isn't a valid UPC, EAN or GTIN.", "externally_assigned_product_identifier")
        elif kind != "ASIN" and not check_digit_ok(code):
            result.add("error", f"The barcode {code} fails its check digit, so Amazon will reject it.",
                       "externally_assigned_product_identifier")
    if not codes and not exemption_confirmed:
        result.add("error", "This listing has no barcode (UPC/EAN/GTIN). Amazon needs one, or a GTIN exemption "
                            "you've confirmed for this brand and category.")


def check_brand(result: Result, session: Session, brand: str | None, *, creating: bool) -> None:
    if not creating:
        return
    name = (brand or "").strip()
    if not name:
        result.add("error", "A new listing needs a brand.", "brand")
        return
    if name.lower() not in OWN_BRANDS:
        known = session.query(Brand).filter(Brand.name.ilike(name)).first()
        result.add("warning" if known else "error",
                   f"\"{name}\" isn't one of your brand-registered brands (Skyzz, Vibezy). Creating listings "
                   "under someone else's brand needs their authorisation.", "brand", requires_pin=True)


def check_price(result: Result, session: Session, *, price: float | None, currency: str | None,
                marketplace_id: str, old_price: float | None = None, product: Product | None = None) -> None:
    if price is None:
        return
    mp = mkt.by_id(marketplace_id)
    if currency and currency != mp.currency:
        result.add("error", f"The price is in {currency} but {mp.code} sells in {mp.currency}.", "price")
    if price <= 0:
        result.add("error", "The price must be more than zero.", "price")
        return
    if product is not None and product.unit_cost is not None:
        cost = float(product.unit_cost)
        if price < cost:
            result.add("error", f"{mp.currency} {price:,.2f} is below what the unit costs you "
                                f"({product.cost_currency} {cost:,.2f}).", "price", requires_pin=True)
    elif product is None or product.unit_cost is None:
        result.add("warning", "I don't know what this product costs you, so I can't check the price against cost. "
                              "Tell me the cost and I'll check next time.", "price")
    if old_price:
        change = (price - float(old_price)) / float(old_price) * 100
        warn_pct = float(settings_store.get(session, "amazon_price_change_warn_pct", 15) or 15)
        pin_pct = float(settings_store.get(session, "amazon_price_change_pin_pct", 40) or 40)
        if abs(change) >= pin_pct:
            result.add("warning", f"That's a {change:+.0f}% price change (from {mp.currency} "
                                  f"{float(old_price):,.2f}).", "price", requires_pin=True)
        elif abs(change) >= warn_pct:
            result.add("warning", f"That's a {change:+.0f}% price change (from {mp.currency} "
                                  f"{float(old_price):,.2f}).", "price")


def check_variations(result: Result, *, theme: str | None, children: list[dict[str, Any]],
                     supported_themes: list[str] | None = None, parent_buyable: bool = False) -> None:
    if not theme:
        result.add("error", "A variation family needs a theme (for example SIZE or COLOR).", "variation_theme")
        return
    if supported_themes and theme.upper() not in [t.upper() for t in supported_themes]:
        result.add("error", f"\"{theme}\" isn't a variation theme Amazon supports for this product type. "
                            f"Supported: {', '.join(supported_themes[:6])}.", "variation_theme")
    seen: dict[tuple, str] = {}
    keys = [k.strip().lower() for k in re.split(r"[/_-]", theme) if k.strip()]
    for child in children:
        attrs = child.get("attributes") or {}
        combo = tuple(str(value_of(attrs, k) or "").strip().lower() for k in keys)
        if any(c == "" for c in combo):
            result.add("error", f"Child {child.get('sku') or '?'} is missing a {theme} value.", "variation_theme")
            continue
        if combo in seen:
            result.add("error", f"Children {seen[combo]} and {child.get('sku')} have the same {theme} "
                                f"({' / '.join(combo)}).", "variation_theme")
        seen[combo] = child.get("sku") or "?"
    if parent_buyable:
        result.add("error", "The parent of a variation family must not be buyable.", "parentage")


# ---------------- the whole thing ----------------
def validate(session: Session, *, attributes: dict[str, Any], schema: dict[str, Any], marketplace_id: str,
             creating: bool = False, listing: AmazonListing | None = None, product: Product | None = None,
             price: float | None = None, currency: str | None = None, old_price: float | None = None,
             brand: str | None = None, exemption_confirmed: bool = False,
             check_identifier: bool | None = None) -> Result:
    result = Result()
    brand = brand or value_of(attributes, "brand") or (listing.brand.name if listing and listing.brand else None)
    check_schema(result, schema, attributes)
    check_title(result, attributes, schema, brand)
    check_bullets_and_description(result, attributes, schema)
    check_claims(result, attributes)
    check_search_terms(result, attributes, brand)
    if check_identifier if check_identifier is not None else creating:
        check_identifiers(result, attributes, exemption_confirmed=exemption_confirmed)
    check_brand(result, session, brand, creating=creating)
    check_price(result, session, price=price, currency=currency, marketplace_id=marketplace_id,
                old_price=old_price, product=product or (listing.product if listing else None))
    return result


# ---------------- Amazon's own preview ----------------
def preview_issues_to_findings(issues: list[dict[str, Any]]) -> Result:
    """Turn Amazon's VALIDATION_PREVIEW issues into findings the owner can act on."""
    result = Result()
    for issue in issues or []:
        severity = (issue.get("severity") or "").upper()
        message = issue.get("message") or issue.get("code") or "Amazon reported a problem"
        fields = ", ".join(issue.get("attributeNames") or []) or None
        where = f" ({friendly(fields)})" if fields else ""
        result.add("error" if severity == "ERROR" else "warning", f"Amazon says: {message}{where}",
                   (issue.get("attributeNames") or [None])[0])
    return result


def run_preview(client, *, seller_id: str, sku: str, marketplace_id: str, body: dict[str, Any],
                method: str = "PATCH") -> tuple[Result, dict[str, Any]]:
    """Ask Amazon to check a change without making it. Errors here block approval."""
    path = f"/listings/2021-08-01/items/{seller_id}/{sku}"
    params = {"marketplaceIds": marketplace_id, "mode": "VALIDATION_PREVIEW"}
    call = client.patch if method.upper() == "PATCH" else client.put
    response = call(path, params=params, json=body, operation=f"{method.lower()}ListingsItem:preview")
    return preview_issues_to_findings(response.get("issues") or []), response
