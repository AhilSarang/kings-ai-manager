"""New listings (brief Section 6): the owner's message → duplicate check → product type → attributes → draft.

The LLM maps the owner's words onto Amazon's schema. It never invents facts, and every value it produces
goes through the same validation as an edit before anyone sees an approval.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon import accounts, catalog, documents as documents_svc, images as images_svc
from app.amazon import listings as listings_svc
from app.amazon import marketplaces as mkt
from app.amazon import patches as patch_svc
from app.amazon import schemas
from app.amazon import validation as v
from app.db.models import AmazonChangeRequest, AmazonListing, Brand
from app.llm import service as llm
from app.prompts import load as load_prompt
from app.services.common import ServiceError
from app.services.formatting import truncate

log = logging.getLogger(__name__)
PENDING_KEY = "amazon_pending_listing"
MAX_TYPE_OPTIONS = 12
LOW_CONFIDENCE = 0.6


class IntakeError(ServiceError):
    pass


class ProductTypeChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_type: str
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)
    alternatives: list[str] = Field(default_factory=list, max_length=2)


class MissingField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attribute: str
    question: str = Field(max_length=300)


class AttributeMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attributes: dict[str, Any]
    missing: list[MissingField] = Field(default_factory=list, max_length=25)
    notes: list[str] = Field(default_factory=list, max_length=10)


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


# ---------------- duplicate check ----------------
def duplicate_check(session: Session, marketplace_code: str, *, identifier: str | None,
                    keywords: str, brand: str | None) -> dict[str, Any]:
    """Brief 6.2 step 2: never create a second ASIN for something Amazon already has."""
    mp = mkt.by_code(marketplace_code)
    if identifier:
        if not catalog.check_digit_ok(identifier) and catalog.identifier_type(identifier) != "ASIN":
            raise IntakeError(f"The barcode {identifier} fails its check digit; please check it.")
        matches = catalog.search_by_identifier(marketplace_code, identifier)
        if matches:
            return {"found": True, "by": "barcode", "items": matches}
    if keywords:
        matches = catalog.search_by_keywords(marketplace_code, keywords, brand=brand, limit=5)
        exact = [m for m in matches
                 if catalog.title_of(m, mp.marketplace_id).strip().lower() == keywords.strip().lower()]
        if exact:
            return {"found": True, "by": "title", "items": exact}
    title_only = keywords.split(",")[0].split(" Price")[0].strip()[:60]
    ours = listings_svc.search(session, title_only) if title_only else []
    if ours:
        return {"found": True, "by": "our own listings", "items": [], "listings": ours}
    return {"found": False, "by": None, "items": []}


def duplicate_message(result: dict[str, Any], marketplace_code: str) -> str:
    mp = mkt.by_code(marketplace_code)
    if result.get("listings"):
        rows = "\n".join(f"• {row.ref} {row.sku} — {truncate(row.title_cached or '', 60)}"
                         for row in result["listings"][:5])
        return (f"You already have this listed:\n{rows}\n"
                "Tell me which one to edit, or say \"create it anyway\" if it really is a different product.")
    items = result.get("items") or []
    rows = "\n".join(f"• {i.get('asin')} — {truncate(catalog.title_of(i, mp.marketplace_id), 60)}"
                     for i in items[:5])
    return (f"Amazon already has this product (matched by {result['by']}):\n{rows}\n"
            "I can add our offer to the existing ASIN instead of creating a duplicate — say "
            "\"add an offer on <ASIN>\", or \"create it anyway\" if this really is a different product.")


# ---------------- product type ----------------
def choose_product_type(session: Session, marketplace_code: str, description: str) -> ProductTypeChoice:
    options = schemas.search_product_types(marketplace_code, description[:180])
    names = [o.get("name") for o in options if o.get("name")][:MAX_TYPE_OPTIONS]
    if not names:
        raise IntakeError("Amazon didn't suggest a product type for that description. Tell me the category "
                          "in a few words (for example \"mouth tape\" or \"lash serum\").")
    if len(names) == 1:
        return ProductTypeChoice(product_type=names[0], confidence=1.0, reason="Amazon returned only this one.")
    prompt = (f"<product>{_esc(description[:1500])}</product>\n"
              f"<marketplace>{marketplace_code}</marketplace>\n"
              "<product_types>\n" + "\n".join(f"- {_esc(n)}" for n in names) + "\n</product_types>")
    choice, _ = llm.complete_json(ProductTypeChoice, system=load_prompt("amazon_product_type"), prompt=prompt,
                                  purpose="amazon_product_type")
    if choice.product_type not in names:
        raise IntakeError(f"I couldn't match that to an Amazon product type. Amazon suggested: "
                          f"{', '.join(names[:3])}. Which one is it?")
    return choice


def product_type_question(choice: ProductTypeChoice) -> str:
    options = [choice.product_type, *choice.alternatives]
    return ("I'm not certain which Amazon product type this is. Which one?\n"
            + "\n".join(f"{i}. {name}" for i, name in enumerate(options, start=1))
            + "\nReply with the number, or the name.")


# ---------------- attribute mapping ----------------
def map_attributes(session: Session, *, description: str, product_type: str, schema: dict[str, Any],
                   marketplace_code: str, existing: dict[str, Any] | None = None,
                   documents: list[documents_svc.Document] | None = None) -> AttributeMapping:
    mp = mkt.by_code(marketplace_code)
    required = (schema.get("required") or [])[:40]
    properties = list((schema.get("properties") or {}).keys())[:120]
    parts = [f"<marketplace id=\"{mp.marketplace_id}\" locale=\"{mp.locale}\" currency=\"{mp.currency}\"/>",
             f"<product_type>{_esc(product_type)}</product_type>",
             "<required_attributes>\n" + "\n".join(f"- {_esc(r)}" for r in required) + "\n</required_attributes>",
             "<available_attributes>\n" + ", ".join(_esc(p) for p in properties) + "\n</available_attributes>",
             f"<owner_message>{_esc(description[:6000])}</owner_message>"]
    if existing:
        parts.append("<existing_asin_data>\n" + _esc(str(existing)[:2000]) + "\n</existing_asin_data>")
    block = documents_svc.prompt_block(documents or [])
    if block:
        parts.append(block)
    mapping, _ = llm.complete_json(AttributeMapping, system=load_prompt("amazon_listing"),
                                   prompt="\n\n".join(parts), purpose="amazon_listing_attributes")
    mapping.attributes = {k: patch_svc.wrap(val, mp.marketplace_id) if not isinstance(val, list) or
                          (val and isinstance(val[0], dict) and "value" not in val[0] and
                           "media_location" not in val[0]) else val
                          for k, val in mapping.attributes.items()}
    return mapping


def missing_message(missing: list[MissingField], ref: str | None = None) -> str:
    lines = ["I need a few more details before this can go to Amazon:"]
    lines += [f"• {m.question}" for m in missing]
    lines.append("Reply with the answers in one message and I'll finish the draft"
                 + (f" ({ref})." if ref else "."))
    return "\n".join(lines)


# ---------------- building the draft ----------------
def brand_of(session: Session, attributes: dict[str, Any]) -> Brand | None:
    name = v.value_of(attributes, "brand")
    if not name:
        return None
    return session.execute(select(Brand).where(Brand.name.ilike(str(name).strip()))).scalar_one_or_none()


def build_new_listing(session: Session, *, sku: str, marketplace_code: str, product_type: str,
                      attributes: dict[str, Any], instructions: str, operation: str = "create_new_asin",
                      asin: str | None = None, source_message_id=None,
                      exemption_confirmed: bool = False) -> AmazonChangeRequest:
    """A create is a change request like any other: validated, previewed, then approved."""
    from app.amazon import changes as changes_svc

    account = accounts.require(session)
    mp = mkt.by_code(marketplace_code)
    existing = listings_svc.find(session, sku=sku, marketplace_id=mp.marketplace_id)
    if existing:
        raise IntakeError(f"SKU {sku} already exists ({existing[0].ref}). Pick another SKU, or tell me to "
                          f"edit {existing[0].ref}.")
    listing = AmazonListing(account_id=account.id, marketplace_id=mp.marketplace_id, sku=sku, asin=asin,
                            product_type=product_type, brand_id=brand_of(session, attributes).id
                            if brand_of(session, attributes) else None,
                            currency=mp.currency, status_cached="draft", buyable=False)
    session.add(listing)
    session.flush()

    request = AmazonChangeRequest(
        listing_id=listing.id, marketplace_id=mp.marketplace_id, operation=operation,
        owner_instructions=instructions, source_message_id=source_message_id,
        payload={"attributes": attributes, "product_type": product_type, "sku": sku,
                 "requirements": "LISTING_OFFER_ONLY" if operation == "create_offer_existing_asin" else "LISTING"},
        diff=[{"field": k, "before": "(new listing)", "after": str(v.value_of(attributes, k))[:300]}
              for k in sorted(attributes)],
        status="validating")
    session.add(request)
    session.flush()

    schema = schemas.get_schema(session, marketplace_code, product_type)
    price = patch_svc.price_of(attributes)
    result = v.validate(session, attributes=attributes, schema=schema, marketplace_id=mp.marketplace_id,
                        creating=True, listing=listing, price=price, currency=mp.currency,
                        exemption_confirmed=exemption_confirmed)
    request.local_validation = result.as_dict()
    request.warnings = [f.message for f in result.warnings]
    request.requires_pin = True  # every new ASIN needs the PIN (brief Section 5)
    request.risk_level = "critical"
    if not result.ok:
        request.status = "needs_info"
        session.flush()
        return request

    preview = _preview_create(session, request, listing, attributes, product_type)
    if preview is not None and not preview.ok:
        request.status = "needs_info"
        request.warnings = request.warnings + [f.message for f in preview.errors]
    else:
        if preview is not None:
            request.warnings = request.warnings + [f.message for f in preview.warnings]
        request.status = "ready_for_approval"
    session.flush()
    return request


def _preview_create(session: Session, request: AmazonChangeRequest, listing: AmazonListing,
                    attributes: dict[str, Any], product_type: str):
    from app.amazon.api import client
    from app.amazon.errors import AmazonError, AmazonRetryable

    account = accounts.require(session)
    body = patch_svc.put_body(product_type, attributes,
                              requirements=request.payload.get("requirements", "LISTING"))
    try:
        result, raw = v.run_preview(client(), seller_id=account.seller_id, sku=listing.sku,
                                    marketplace_id=listing.marketplace_id, body=body, method="PUT")
    except (AmazonRetryable, AmazonError) as exc:
        request.amazon_preview_result = {"error": str(exc)[:300]}
        return None
    request.amazon_preview_result = {"status": raw.get("status"), "issues": raw.get("issues") or []}
    return result


def attach_images(session: Session, request: AmazonChangeRequest, text: str) -> str:
    """Import any Drive images in the owner's message and put them into the draft's attributes."""
    rows = images_svc.import_images(session, text=text, listing=request.listing, change_request=request)
    if not rows:
        return ""
    images_svc.host(session, [r for r in rows if r.status == "checked"])
    attributes = {**request.payload.get("attributes", {}),
                  **images_svc.attributes_for(rows, request.marketplace_id)}
    request.payload = {**request.payload, "attributes": attributes}
    session.flush()
    return images_svc.summary(rows)


# ---------------- the WhatsApp flow ----------------
def _pending(session: Session) -> dict[str, Any] | None:
    from app.services import settings_store

    return settings_store.get(session, PENDING_KEY) or None


def _remember(session: Session, data: dict[str, Any] | None, user) -> None:
    from app.services import settings_store
    from app.services.common import Actor

    settings_store.set_value(session, PENDING_KEY, data, Actor.user(user.id) if user else None)


def start(session: Session, *, user, sku: str, marketplace_code: str, details: str, asin: str | None = None,
          create_anyway: bool = False, message_id=None) -> str:
    """The owner described a product. Check for duplicates, pick a product type, map attributes, ask what's missing."""
    from app.amazon import changes as changes_svc
    from app.amazon import views

    accounts.require(session)
    identifier = _identifier_in(details)
    brand = _brand_in(session, details)
    title_guess = details.strip().splitlines()[0][:120]
    if not create_anyway and not asin:
        duplicates = duplicate_check(session, marketplace_code, identifier=identifier, keywords=title_guess,
                                     brand=brand.name if brand else None)
        if duplicates["found"]:
            _remember(session, {"sku": sku, "marketplace": marketplace_code, "details": details,
                                "stage": "duplicate", "message_id": str(message_id) if message_id else None}, user)
            return duplicate_message(duplicates, marketplace_code)

    choice = choose_product_type(session, marketplace_code, details)
    if choice.confidence < LOW_CONFIDENCE:
        _remember(session, {"sku": sku, "marketplace": marketplace_code, "details": details, "asin": asin,
                            "stage": "product_type", "options": [choice.product_type, *choice.alternatives],
                            "message_id": str(message_id) if message_id else None}, user)
        return product_type_question(choice)

    return _draft(session, user=user, sku=sku, marketplace_code=marketplace_code, details=details,
                  product_type=choice.product_type, asin=asin, message_id=message_id)


def _draft(session: Session, *, user, sku: str, marketplace_code: str, details: str, product_type: str,
           asin: str | None, message_id=None, extra_answers: str = "") -> str:
    from app.amazon import views

    schema = schemas.get_schema(session, marketplace_code, product_type)
    text = f"{details}\n{extra_answers}".strip()
    documents = documents_svc.load(session, text)
    document_note = documents_svc.warning(documents)
    mapping = map_attributes(session, description=text, product_type=product_type, schema=schema,
                             marketplace_code=marketplace_code, documents=documents)
    if mapping.missing:
        _remember(session, {"sku": sku, "marketplace": marketplace_code, "details": text, "asin": asin,
                            "product_type": product_type, "stage": "missing",
                            "questions": [m.model_dump() for m in mapping.missing],
                            "message_id": str(message_id) if message_id else None}, user)
        return "\n".join(x for x in (document_note, missing_message(mapping.missing)) if x)

    request = build_new_listing(session, sku=sku, marketplace_code=marketplace_code, product_type=product_type,
                                attributes=mapping.attributes,
                                instructions=text, asin=asin,
                                operation="create_offer_existing_asin" if asin else "create_new_asin",
                                source_message_id=message_id)
    image_note = attach_images(session, request, text)
    _remember(session, None, user)
    lines = [f"📝 Draft listing {request.ref} for {sku} ({product_type})"]
    if document_note:
        lines.append(document_note)
    if mapping.notes:
        lines += [f"• {n}" for n in mapping.notes]
    if image_note:
        lines.append(image_note)
    lines.append(views.change_detail(session, request))
    if request.status == "ready_for_approval":
        from app.amazon import changes as changes_svc

        lines.append(changes_svc.request_approval(session, request, user))
    return "\n".join(lines)


def answer(session: Session, *, user, answers: str, message_id=None) -> str:
    """The owner answered the questions from an earlier draft."""
    pending = _pending(session)
    if not pending:
        return ("I don't have a listing waiting on answers. Tell me the product details and I'll start one.")
    stage = pending.get("stage")
    if stage == "product_type":
        chosen = _match_option(answers, pending.get("options") or [])
        if not chosen:
            return product_type_question(ProductTypeChoice(
                product_type=(pending.get("options") or ["?"])[0], confidence=0.0, reason="",
                alternatives=(pending.get("options") or [])[1:]))
        return _draft(session, user=user, sku=pending["sku"], marketplace_code=pending["marketplace"],
                      details=pending["details"], product_type=chosen, asin=pending.get("asin"),
                      message_id=message_id)
    if stage == "duplicate":
        return ("Say \"create it anyway\" if it's genuinely a different product, or tell me which listing to "
                "edit instead.")
    return _draft(session, user=user, sku=pending["sku"], marketplace_code=pending["marketplace"],
                  details=pending["details"], product_type=pending["product_type"], asin=pending.get("asin"),
                  message_id=message_id, extra_answers=answers)


def _match_option(answer_text: str, options: list[str]) -> str | None:
    text = (answer_text or "").strip()
    if text.isdigit() and 1 <= int(text) <= len(options):
        return options[int(text) - 1]
    for option in options:
        if option.lower() in text.lower():
            return option
    return None


def _identifier_in(text: str) -> str | None:
    import re

    for match in re.findall(r"\b\d{8,14}\b", text or ""):
        if catalog.check_digit_ok(match):
            return match
    return None


def _brand_in(session: Session, text: str) -> Brand | None:
    for brand in session.execute(select(Brand)).scalars():
        if brand.name.lower() in (text or "").lower():
            return brand
    return None
