"""Listing changes: draft → validate → approve → submit → verify (brief Sections 8 and 10).

The LLM only ever proposes values. Everything here is plain code, and nothing reaches Amazon
without an approval whose payload matches what is about to be sent.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon import accounts, api
from app.amazon import listings as listings_svc
from app.amazon import marketplaces as mkt
from app.amazon import patches as patch_svc
from app.amazon import schemas
from app.amazon import validation as v
from app.amazon.errors import AmazonAuthError, AmazonError, AmazonRetryable
from app.db.models import AmazonChangeRequest, AmazonListing, AmazonListingVersion, Product
from app.jobs import queue
from app.services import approvals, audit, settings_store
from app.services.common import Actor, NotFound, ServiceError, utcnow
from app.services.formatting import truncate

log = logging.getLogger(__name__)
AGENT = Actor.agent("amazon")

CHECK_SCHEDULE_MINUTES = (5, 30, 120, 24 * 60)
OPEN_STATUSES = ("drafting", "needs_info", "validating", "ready_for_approval", "pending_approval", "approved",
                 "submitted", "accepted")


class ChangeError(ServiceError):
    pass


def normalize_ref(ref: str) -> str:
    ref = (ref or "").strip().upper().replace(" ", "")
    return ref if ref.startswith("LC-") else f"LC-{ref.lstrip('LC-')}"


def get_by_ref(session: Session, ref: str) -> AmazonChangeRequest:
    row = session.execute(select(AmazonChangeRequest)
                          .where(AmazonChangeRequest.ref == normalize_ref(ref))).scalar_one_or_none()
    if row is None:
        raise NotFound(f"I can't find change {normalize_ref(ref)}.")
    return row


def open_changes(session: Session, limit: int = 20) -> list[AmazonChangeRequest]:
    return list(session.execute(
        select(AmazonChangeRequest).where(AmazonChangeRequest.status.in_(OPEN_STATUSES))
        .order_by(AmazonChangeRequest.created_at.desc()).limit(limit)).scalars())


def current_attributes(session: Session, listing: AmazonListing) -> dict[str, Any]:
    version = session.get(AmazonListingVersion, listing.current_version_id) if listing.current_version_id else None
    return dict(version.attributes or {}) if version else {}


# ---------------- building ----------------
def build(session: Session, listing: AmazonListing, changes: dict[str, Any], *, operation: str,
          instructions: str = "", source_message_id=None, exemption_confirmed: bool = False,
          sync_first: bool = True) -> AmazonChangeRequest:
    """Draft a change, validate it locally and with Amazon, and leave it ready for approval."""
    if not changes:
        raise ChangeError("Tell me what to change and I'll draft it.")
    if sync_first:
        listing = listings_svc.sync_one(session, listing)  # never build a patch on a stale copy
    before = current_attributes(session, listing)
    patches = patch_svc.build_patches(changes, listing.marketplace_id, current=before)
    after = patch_svc.apply_patches(before, patches)

    request = AmazonChangeRequest(
        listing_id=listing.id, marketplace_id=listing.marketplace_id, operation=operation,
        owner_instructions=instructions, source_message_id=source_message_id,
        payload={"patches": patches, "product_type": listing.product_type, "sku": listing.sku},
        before_snapshot_version_id=listing.current_version_id,
        diff=patch_svc.diff(before, after), status="validating")
    session.add(request)
    session.flush()

    result = _validate(session, listing, after, patches)
    request.local_validation = result.as_dict()
    request.warnings = [f.message for f in result.warnings]
    request.requires_pin = result.requires_pin
    request.risk_level = _risk(operation, result)
    if not result.ok:
        request.status = "needs_info"
        session.flush()
        return request

    preview = _preview(session, listing, patches, request)
    if preview is not None and not preview.ok:
        request.status = "needs_info"
        request.warnings = request.warnings + [f.message for f in preview.errors]
        session.flush()
        return request
    if preview is not None:
        request.warnings = request.warnings + [f.message for f in preview.warnings]
    request.status = "ready_for_approval"
    session.flush()
    audit.record(session, AGENT, "amazon_change_drafted", target_type="amazon_change", target_id=request.ref,
                 details={"listing": listing.ref, "operation": operation,
                          "fields": patch_svc.touched_fields(patches)})
    return request


def _validate(session: Session, listing: AmazonListing, after: dict[str, Any],
              patches: list[dict[str, Any]]) -> v.Result:
    schema: dict[str, Any] = {}
    if listing.product_type:
        try:
            schema = schemas.get_schema(session, mkt.by_id(listing.marketplace_id).code, listing.product_type)
        except (AmazonError, Exception) as exc:  # a schema we can't fetch must not block a price change
            log.warning("could not load product type schema", extra={"error": str(exc)[:150]})
    new_price = patch_svc.price_of(after)
    product = session.get(Product, listing.product_id) if listing.product_id else None
    return v.validate(session, attributes=after, schema=schema, marketplace_id=listing.marketplace_id,
                      listing=listing, product=product, price=new_price,
                      currency=listing.currency or mkt.by_id(listing.marketplace_id).currency,
                      old_price=float(listing.price_cached) if listing.price_cached is not None else None,
                      check_identifier=False)


def _preview(session: Session, listing: AmazonListing, patches: list[dict[str, Any]],
             request: AmazonChangeRequest) -> v.Result | None:
    account = accounts.require(session)
    body = patch_svc.patch_body(listing.product_type or "PRODUCT", patches)
    try:
        result, raw = v.run_preview(api.client(), seller_id=account.seller_id, sku=listing.sku,
                                    marketplace_id=listing.marketplace_id, body=body)
    except AmazonAuthError:
        raise
    except (AmazonRetryable, AmazonError) as exc:
        log.warning("validation preview failed", extra={"error": str(exc)[:200]})
        request.amazon_preview_result = {"error": str(exc)[:300]}
        return None
    request.amazon_preview_result = {"status": raw.get("status"), "issues": raw.get("issues") or []}
    return result


def _risk(operation: str, result: v.Result) -> str:
    if result.requires_pin or operation in ("full_replace", "delete_listing", "create_new_asin",
                                            "create_variation_family", "remove_variation_child"):
        return "critical"
    return "high"


# ---------------- approval ----------------
def approval_summary(session: Session, request: AmazonChangeRequest) -> str:
    listing = request.listing
    mp = mkt.by_id(request.marketplace_id)
    brand = listing.brand.name if listing and listing.brand else "—"
    titles = {"patch_attributes": "Edit", "update_price": "Price change", "revert": "Revert",
              "create_offer_existing_asin": "New offer", "create_new_asin": "New listing",
              "full_replace": "Full replace", "delete_listing": "Delete listing"}
    lines = [f"🛒 LISTING CHANGE {request.ref} — {titles.get(request.operation, request.operation)} "
             f"({brand}, {mp.code})",
             f"SKU: {listing.sku}   ASIN: {listing.asin or '—'}",
             "Changes:"]
    for change in request.diff or []:
        lines.append(f"• {v.friendly(change['field'])}")
        lines.append(f"  Before: {truncate(change['before'], 300)}")
        lines.append(f"  After:  {truncate(change['after'], 300)}")
    if request.warnings:
        lines.append("Warnings:")
        lines += [f"• {w}" for w in request.warnings]
    preview = request.amazon_preview_result or {}
    if preview.get("error"):
        lines.append(f"Amazon preview: couldn't run ({preview['error'][:120]})")
    else:
        issues = preview.get("issues") or []
        errors = len([i for i in issues if (i.get("severity") or "").upper() == "ERROR"])
        lines.append(f"Amazon preview: {preview.get('status', 'checked')}, {errors} errors, "
                     f"{len(issues) - errors} warnings")
    if not accounts.writes_allowed(session):
        lines.append("Write mode is validation only: approving records your decision but sends nothing to Amazon.")
    return "\n".join(lines)


def request_approval(session: Session, request: AmazonChangeRequest, user) -> str:
    if request.status not in ("ready_for_approval", "needs_info"):
        raise ChangeError(f"{request.ref} is {request.status.replace('_', ' ')} and can't be approved now.")
    if request.status == "needs_info":
        problems = "\n".join(f"• {e}" for e in (request.local_validation or {}).get("errors", [])) or "—"
        raise ChangeError(f"{request.ref} can't go to Amazon yet:\n{problems}")
    listing = request.listing
    payload = {
        "change_ref": request.ref, "listing_ref": listing.ref, "sku": listing.sku,
        "marketplace_id": request.marketplace_id, "product_type": listing.product_type,
        "patches": request.payload.get("patches", []),
        "before_version_id": str(request.before_snapshot_version_id) if request.before_snapshot_version_id else None,
        "operation": request.operation,
    }
    if request.payload.get("attributes") is not None:  # a create sends every attribute, not a patch
        payload["attributes"] = request.payload["attributes"]
        payload["requirements"] = request.payload.get("requirements", "LISTING")
    if request.payload.get("batch"):  # a variation family: parent and children in one approval
        payload["batch"] = request.payload["batch"]
    if request.payload.get("delete"):
        payload["delete"] = True
        payload["confirm_text"] = request.payload["confirm_text"]  # the SKU the owner types back
    approval = approvals.request_approval(
        session, action_type=_action_type(request.operation), payload=payload,
        summary=approval_summary(session, request), risk_level=request.risk_level,
        requested_by_agent_key="amazon", notify=False)
    request.approval_id = approval.id
    request.status = "pending_approval"
    session.flush()
    return approvals.approval_message(approval)


def _action_type(operation: str) -> str:
    return {"patch_attributes": "amazon_patch_attributes", "update_price": "amazon_update_price",
            "create_variation_family": "amazon_create_variation_family",
            "add_variation_child": "amazon_add_variation_child",
            "remove_variation_child": "amazon_remove_variation_child",
            "revert": "amazon_revert", "create_offer_existing_asin": "amazon_create_offer_existing_asin",
            "create_new_asin": "amazon_create_new_asin", "full_replace": "amazon_full_replace",
            "delete_listing": "amazon_delete_listing"}.get(operation, "amazon_patch_attributes")


def cancel(session: Session, request: AmazonChangeRequest, user) -> str:
    if request.status in ("submitted", "accepted", "live_verified"):
        raise ChangeError(f"{request.ref} was already sent to Amazon and can't be cancelled here.")
    actor = Actor.user(user.id)
    if request.approval_id:
        from app.db.models import Approval

        approval = session.get(Approval, request.approval_id)
        if approval:
            approvals.cancel(session, approval, actor, "change cancelled")
    request.status = "cancelled"
    session.flush()
    audit.record(session, actor, "amazon_change_cancelled", target_type="amazon_change", target_id=request.ref)
    return f"🗑️ {request.ref} cancelled. Nothing was sent to Amazon."


# ---------------- execution ----------------
def _failed(session: Session, payload: dict, approval, error: str) -> None:
    request = session.execute(select(AmazonChangeRequest)
                              .where(AmazonChangeRequest.ref == payload["change_ref"])).scalar_one_or_none()
    if request is None or request.status in ("completed_with_issues", "live_verified"):
        return
    # A stale change is dead rather than broken: the owner has to ask again so it's rebuilt from the current listing.
    stale = "changed in Seller Central" in error
    request.status = "cancelled" if stale else "failed"
    request.final_issues = [error[:300]]


def _execute(session: Session, payload: dict, approval) -> dict:
    """Shared by every listing-write action: freshness check, submit, then schedule verification."""
    from app.jobs.handlers.amazon import amazon_paused

    request = session.execute(select(AmazonChangeRequest).where(
        AmazonChangeRequest.ref == payload["change_ref"]).with_for_update(of=AmazonChangeRequest)).scalar_one_or_none()
    if request is None or request.status != "pending_approval" or request.approval_id != approval.id:
        raise ChangeError("This change is no longer waiting for approval; nothing was sent.")
    if amazon_paused(session):
        raise ChangeError("Amazon is paused, so nothing was sent. Send AMAZON RESUME, then approve again.")
    account = accounts.require(session)
    listing = request.listing

    if payload.get("delete"):
        return _execute_delete(session, payload, request, listing, account)
    if payload.get("batch"):
        return _execute_batch(session, payload, request, account)
    if payload.get("attributes") is not None:
        return _execute_create(session, payload, request, listing, account)

    # Freshness (brief 8.3): re-read and refuse if somebody changed the same fields meanwhile.
    before = current_attributes(session, listing)
    listings_svc.sync_one(session, listing)
    now = current_attributes(session, listing)
    fields = patch_svc.touched_fields(payload["patches"])
    moved = patch_svc.unchanged_since(before, now, fields)
    if moved:
        raise ChangeError(f"{', '.join(v.friendly(f) for f in moved)} changed in Seller Central after you approved "
                          f"this, so nothing was sent. Ask me again and I'll rebuild {request.ref} from the "
                          "current listing.")

    if not accounts.writes_allowed(session):
        request.status = "completed_with_issues"
        request.final_issues = ["Write mode was validation only, so nothing was sent to Amazon."]
        return {"summary": f"{request.ref} approved, but write mode is validation only so nothing was sent. "
                           "Switch with AMAZON LIVE when you're ready.", "complete_task": False}

    path = f"/listings/2021-08-01/items/{account.seller_id}/{payload['sku']}"
    params = {"marketplaceIds": payload["marketplace_id"]}
    try:
        if payload.get("attributes") is not None:  # a create or a full replace sends every attribute
            body = patch_svc.put_body(payload.get("product_type") or "PRODUCT", payload["attributes"],
                                      requirements=payload.get("requirements", "LISTING"))
            response = api.client().put(path, params=params, json=body, operation="putListingsItem")
        else:
            body = patch_svc.patch_body(payload.get("product_type") or "PRODUCT", payload["patches"])
            response = api.client().patch(path, params=params, json=body, operation="patchListingsItem")
    except AmazonAuthError as exc:
        raise ChangeError(f"Amazon refused our credentials ({exc}); nothing was sent.") from exc
    except AmazonRetryable as exc:
        raise ChangeError(f"Amazon was unavailable ({exc}); nothing was sent. Approve again to retry.") from exc

    request.submission_id = response.get("submissionId")
    request.submitted_at = utcnow()
    request.status = "accepted" if (response.get("status") or "").upper() == "ACCEPTED" else "submitted"
    issues = response.get("issues") or []
    if issues:
        request.final_issues = [i.get("message") or i.get("code") for i in issues]
    _record_submitted_version(session, listing, payload, request)
    _schedule_checks(session, request)
    audit.record(session, AGENT, "amazon_change_submitted", target_type="amazon_change", target_id=request.ref,
                 details={"submission": request.submission_id, "sku": payload["sku"]})
    return {"summary": f"{request.ref} sent to Amazon for {payload['sku']} (submission "
                       f"{request.submission_id or 'accepted'}). I'll confirm when it's live.",
            "complete_task": False, "submission_id": request.submission_id}


def _execute_create(session: Session, payload: dict, request: AmazonChangeRequest, listing: AmazonListing,
                    account) -> dict:
    """A new listing or offer: nothing to compare against, so there's no freshness check."""
    if not accounts.writes_allowed(session):
        request.status = "completed_with_issues"
        request.final_issues = ["Write mode was validation only, so nothing was created on Amazon."]
        return {"summary": f"{request.ref} approved, but write mode is validation only so nothing was created. "
                           "Switch with AMAZON LIVE when you're ready.", "complete_task": False}
    body = patch_svc.put_body(payload.get("product_type") or "PRODUCT", payload["attributes"],
                              requirements=payload.get("requirements", "LISTING"))
    try:
        response = api.client().put(
            f"/listings/2021-08-01/items/{account.seller_id}/{payload['sku']}",
            params={"marketplaceIds": payload["marketplace_id"]}, json=body, operation="putListingsItem")
    except AmazonAuthError as exc:
        raise ChangeError(f"Amazon refused our credentials ({exc}); nothing was created.") from exc
    except AmazonRetryable as exc:
        raise ChangeError(f"Amazon was unavailable ({exc}); nothing was created. Approve again to retry.") from exc
    request.submission_id = response.get("submissionId")
    request.submitted_at = utcnow()
    request.status = "accepted" if (response.get("status") or "").upper() == "ACCEPTED" else "submitted"
    issues = response.get("issues") or []
    if issues:
        request.final_issues = [i.get("message") or i.get("code") for i in issues]
    version = AmazonListingVersion(listing_id=listing.id, version_number=1, source="submitted_change",
                                   attributes=payload["attributes"], retrieved_at=utcnow(),
                                   created_by="amazon_agent", change_request_id=request.id)
    session.add(version)
    session.flush()
    listing.current_version_id = version.id
    _schedule_checks(session, request)
    audit.record(session, AGENT, "amazon_listing_created", target_type="amazon_change", target_id=request.ref,
                 details={"sku": payload["sku"], "operation": request.operation})
    return {"summary": f"{request.ref} sent to Amazon: {payload['sku']} created. Amazon usually takes 15 minutes "
                       "to a few hours to publish it, and I'll confirm when it's live.",
            "complete_task": False, "submission_id": request.submission_id}


def _execute_batch(session: Session, payload: dict, request: AmazonChangeRequest, account) -> dict:
    """A variation family: parent first, then children, with a result per SKU."""
    if not accounts.writes_allowed(session):
        request.status = "completed_with_issues"
        request.final_issues = ["Write mode was validation only, so nothing was created on Amazon."]
        return {"summary": f"{request.ref} approved, but write mode is validation only so nothing was created.",
                "complete_task": False}
    client = api.client()
    done, failed = [], []
    for item in payload["batch"]:
        body = patch_svc.put_body(payload.get("product_type") or "PRODUCT", item["attributes"])
        try:
            client.put(f"/listings/2021-08-01/items/{account.seller_id}/{item['sku']}",
                       params={"marketplaceIds": payload["marketplace_id"]}, json=body,
                       operation="putListingsItem")
            done.append(item["sku"])
        except (AmazonAuthError, AmazonRetryable, AmazonError) as exc:
            failed.append(f"{item['sku']}: {exc}")
            if isinstance(exc, AmazonAuthError):
                break  # credentials won't fix themselves; stop rather than hammer Amazon
    request.submitted_at = utcnow()
    request.final_issues = failed
    request.status = "accepted" if not failed else "completed_with_issues"
    _schedule_checks(session, request)
    audit.record(session, AGENT, "amazon_variation_family_submitted", target_type="amazon_change",
                 target_id=request.ref, details={"created": done, "failed": failed})
    lines = [f"{request.ref}: {len(done)} of {len(payload['batch'])} SKUs sent to Amazon."]
    if done:
        lines.append("Sent: " + ", ".join(done))
    if failed:
        lines.append("Failed: " + "; ".join(failed))
        lines.append("The ones that failed were not created. Tell me to try those again once they're fixed.")
    return {"summary": "\n".join(lines), "complete_task": False}


def _execute_delete(session: Session, payload: dict, request: AmazonChangeRequest, listing, account) -> dict:
    if not accounts.writes_allowed(session):
        request.status = "completed_with_issues"
        request.final_issues = ["Write mode was validation only, so nothing was deleted."]
        return {"summary": f"{request.ref} approved, but write mode is validation only so nothing was deleted.",
                "complete_task": False}
    try:
        api.client().delete(f"/listings/2021-08-01/items/{account.seller_id}/{payload['sku']}",
                            params={"marketplaceIds": payload["marketplace_id"]},
                            operation="deleteListingsItem")
    except AmazonAuthError as exc:
        raise ChangeError(f"Amazon refused our credentials ({exc}); nothing was deleted.") from exc
    except AmazonRetryable as exc:
        raise ChangeError(f"Amazon was unavailable ({exc}); nothing was deleted.") from exc
    request.submitted_at = utcnow()
    request.status = "accepted"
    listing.status_cached = "deleted"
    listing.buyable = False
    audit.record(session, AGENT, "amazon_listing_deleted", target_type="amazon_change", target_id=request.ref,
                 details={"sku": payload["sku"], "kept_version": str(listing.current_version_id)})
    return {"summary": f"{payload['sku']} deleted from Amazon. I've kept the last version, so say "
                       f"\"recreate {listing.ref}\" if you ever want it back. Any FBA stock for it is still "
                       "at Amazon and now counts as stranded.",
            "complete_task": False}


def _record_submitted_version(session: Session, listing: AmazonListing, payload: dict,
                              request: AmazonChangeRequest) -> None:
    before = current_attributes(session, listing)
    after = patch_svc.apply_patches(before, payload["patches"])
    last = session.execute(select(AmazonListingVersion.version_number)
                           .where(AmazonListingVersion.listing_id == listing.id)
                           .order_by(AmazonListingVersion.version_number.desc()).limit(1)).scalar() or 0
    version = AmazonListingVersion(listing_id=listing.id, version_number=last + 1, source="submitted_change",
                                   attributes=after, retrieved_at=utcnow(), created_by="amazon_agent",
                                   change_request_id=request.id)
    session.add(version)
    session.flush()


def _schedule_checks(session: Session, request: AmazonChangeRequest) -> None:
    for minutes in CHECK_SCHEDULE_MINUTES:
        queue.enqueue(session, "amazon_check_submission", {"change_ref": request.ref, "after_minutes": minutes},
                      run_after=utcnow() + timedelta(minutes=minutes), priority=40,
                      idempotency_key=f"amazon_check:{request.ref}:{minutes}")


def check_submission(session: Session, request: AmazonChangeRequest, after_minutes: int) -> str:
    """Did Amazon actually apply it? Runs at 5 minutes, 30 minutes, 2 hours and 24 hours."""
    if request.status not in ("submitted", "accepted", "completed_with_issues"):
        return "not waiting"
    listing = request.listing
    listings_svc.sync_one(session, listing)
    errors = [i for i in (listing.issues_cached or []) if (i.get("severity") or "").upper() == "ERROR"]
    now = current_attributes(session, listing)
    wanted = patch_svc.apply_patches(current_attributes(session, listing), request.payload.get("patches", []))
    outstanding = patch_svc.unchanged_since(wanted, now, patch_svc.touched_fields(request.payload.get("patches", [])))
    if errors:
        request.status = "completed_with_issues"
        request.final_issues = [i.get("message") or i.get("code") for i in errors]
        _notify(session, f"⚠️ {request.ref} ({listing.ref} {listing.sku}) is live but Amazon flagged: "
                         f"{'; '.join(request.final_issues[:3])}")
        return "issues"
    if not outstanding:
        request.status = "live_verified"
        _notify(session, f"✅ {request.ref} is live on Amazon: {listing.ref} {listing.sku}"
                         + (f" ({listing.asin})" if listing.asin else ""))
        return "live"
    if after_minutes >= CHECK_SCHEDULE_MINUTES[-1]:
        request.status = "completed_with_issues"
        request.final_issues = [f"Amazon hasn't applied: {', '.join(v.friendly(f) for f in outstanding)}"]
        _notify(session, f"⚠️ 24 hours after {request.ref}, Amazon still hasn't applied "
                         f"{', '.join(v.friendly(f) for f in outstanding)} on {listing.sku}. This usually means "
                         "another contributor's data is ranked higher. Reply "
                         f"\"{request.ref} DETAILS\" and I'll draft a case for Seller Support.")
        return "not applied"
    return "waiting"


def _notify(session: Session, text: str) -> None:
    from app.services.notify import notify_owner

    notify_owner(session, text)


# ---------------- revert ----------------
def build_revert(session: Session, listing: AmazonListing, version_number: int, user) -> AmazonChangeRequest:
    target = listings_svc.version_by_number(session, listing, version_number)
    listing = listings_svc.sync_one(session, listing)
    before = current_attributes(session, listing)
    changes = {}
    for key, value in (target.attributes or {}).items():
        if before.get(key) != value:
            changes[key] = value
    for key in before:
        if key not in (target.attributes or {}):
            changes[key] = None
    if not changes:
        raise ChangeError(f"{listing.ref} already matches V{version_number}; there's nothing to revert.")
    request = build(session, listing, changes, operation="revert",
                    instructions=f"Revert to V{version_number}", sync_first=False)
    request.payload = {**request.payload, "revert_to_version": version_number}
    session.flush()
    return request


def price_change(session: Session, listing: AmazonListing, new_price: float, *, instructions: str = "",
                 source_message_id=None) -> AmazonChangeRequest:
    currency = listing.currency or mkt.by_id(listing.marketplace_id).currency
    changes = {patch_svc.PRICE_ATTRIBUTE: patch_svc.price_attribute(new_price, currency, listing.marketplace_id)}
    return build(session, listing, changes, operation="update_price", instructions=instructions,
                 source_message_id=source_message_id)


def settings_thresholds(session: Session) -> tuple[float, float]:
    return (float(settings_store.get(session, "amazon_price_change_warn_pct", 15) or 15),
            float(settings_store.get(session, "amazon_price_change_pin_pct", 40) or 40))


# ---------------- executors (one per write action, all sharing _execute) ----------------
for _action in ("amazon_patch_attributes", "amazon_update_price", "amazon_revert",
                "amazon_create_offer_existing_asin", "amazon_full_replace", "amazon_create_new_asin",
                "amazon_create_variation_family", "amazon_add_variation_child",
                "amazon_remove_variation_child", "amazon_delete_listing"):
    approvals.executor(_action, on_failure=_failed)(_execute)
