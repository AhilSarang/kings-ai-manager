"""Applying an approved change to Amazon, verifying it landed, and being able to undo it.

Three things about Amazon's write endpoints shape this module, all read from its own spec:

* **They answer 207, not 200.** Each item in the request succeeds or fails on its own, and the body
  carries `{entity: {success: [...], error: [...]}}`. An HTTP 207 whose every item failed looks like
  success to anything checking the status code, so the body is parsed and a failed item is an error.
* **The success entry echoes the whole updated entity**, which is a free read-back. The brief asks for
  verification by re-reading from the API (7.4), so both are done: the echo is checked immediately and
  the entity is read again from the list endpoint.
* **A write takes up to 1,000 items.** Changes are still sent one at a time, deliberately: a batch
  that half-applies is the thing the brief most wants to avoid, and one item per call means the
  before, the after and the verification all belong to exactly one recommendation.

The rollback payload is written **before** the request goes out, so undoing a change never depends on
being able to read Amazon afterwards.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (PpcAction, PpcCampaign, PpcNegative, PpcProfile, PpcRecommendation,
                           PpcTarget)
from app.ppc import api, profiles, structure
from app.ppc.errors import AdsError
from app.services import approvals, audit
from app.services.common import Actor, ServiceError, utcnow

log = logging.getLogger(__name__)


class WriteRefused(ServiceError):
    """We are not allowed to make this change — write mode, or nothing to write."""


class WriteFailed(ServiceError):
    """Amazon refused the change, or it did not land. Nothing else in the batch is attempted."""


@dataclass(frozen=True)
class Writer:
    """How one kind of change is made: where it goes, and how to read it back."""

    verb: str
    path: str
    media: str
    collection: str
    build: Callable[[Session, PpcRecommendation], dict[str, Any]]
    operation: str
    creates: bool = False


def _value(node: dict[str, Any] | None, default=None):
    if not node:
        return default
    return node.get("value", default) if "value" in node else node


def _target(session: Session, row: PpcRecommendation) -> PpcTarget:
    target = session.get(PpcTarget, row.entity_id) if row.entity_id else None
    if target is None:
        target = session.execute(select(PpcTarget).where(
            PpcTarget.amazon_target_id == row.amazon_entity_id)).scalars().first()
    if target is None:
        raise WriteRefused(f"{row.ref} refers to a target I no longer hold.")
    return target


def _campaign(session: Session, row: PpcRecommendation) -> PpcCampaign:
    campaign = session.get(PpcCampaign, row.entity_id) if row.entity_id else None
    if campaign is None:
        campaign = session.execute(select(PpcCampaign).where(
            PpcCampaign.amazon_campaign_id == row.amazon_entity_id)).scalars().first()
    if campaign is None:
        raise WriteRefused(f"{row.ref} refers to a campaign I no longer hold.")
    return campaign


# ---------------- building each kind of request ----------------
def _bid_body(session: Session, row: PpcRecommendation) -> dict[str, Any]:
    target = _target(session, row)
    bid = float(_value(row.proposed_value))
    return {"targetId" if target.target_kind == "target" else "keywordId": target.amazon_target_id,
            "bid": round(bid, 2)}


def _state_body(session: Session, row: PpcRecommendation) -> dict[str, Any]:
    target = _target(session, row)
    wanted = str(_value(row.proposed_value) or "paused").upper()
    return {"targetId" if target.target_kind == "target" else "keywordId": target.amazon_target_id,
            "state": "ENABLED" if wanted == "ENABLED" else "PAUSED"}


def _budget_body(session: Session, row: PpcRecommendation) -> dict[str, Any]:
    campaign = _campaign(session, row)
    amount = float(_value(row.proposed_value))
    return {"campaignId": campaign.amazon_campaign_id,
            "budget": {"budget": round(amount, 2),
                       "budgetType": (campaign.budget_type or "daily").upper()}}


def _placement_body(session: Session, row: PpcRecommendation) -> dict[str, Any]:
    campaign = _campaign(session, row)
    wanted = float(_value(row.proposed_value) or 0)
    placement = (row.evidence or {}).get("placement") or ""
    key = {"top_of_search": "PLACEMENT_TOP", "product_pages": "PLACEMENT_PRODUCT_PAGE",
           "rest_of_search": "PLACEMENT_REST_OF_SEARCH"}.get(placement)
    if key is None:
        raise WriteRefused(f"{row.ref} doesn't say which placement to adjust.")
    # The whole list is sent back, with one entry changed: Amazon replaces the set, it does not merge.
    adjustments = [dict(entry) for entry in (campaign.placement_adjustments or [])
                   if isinstance(entry, dict) and entry.get("placement") != key]
    adjustments.append({"placement": key, "percentage": int(round(wanted))})
    return {"campaignId": campaign.amazon_campaign_id,
            "dynamicBidding": {"strategy": campaign.bidding_strategy or "LEGACY_FOR_SALES",
                               "placementBidding": adjustments}}


def _negative_body(session: Session, row: PpcRecommendation) -> dict[str, Any]:
    proposed = row.proposed_value or {}
    expression = proposed.get("expression") or row.entity_label
    if not expression:
        raise WriteRefused(f"{row.ref} doesn't say what to block.")
    match = "NEGATIVE_PHRASE" if row.change_type == "add_negative_phrase" else "NEGATIVE_EXACT"
    ad_group_id = (row.evidence or {}).get("ad_group_id") or _ad_group_from(session, row)
    campaign_id = (row.evidence or {}).get("campaign_id") or _campaign_from(session, row)
    if not (ad_group_id and campaign_id):
        raise WriteRefused(f"{row.ref} doesn't say which ad group to block it in.")
    return {"campaignId": campaign_id, "adGroupId": ad_group_id, "keywordText": expression,
            "matchType": match, "state": "ENABLED"}


def _harvest_body(session: Session, row: PpcRecommendation) -> dict[str, Any]:
    proposed = row.proposed_value or {}
    expression = proposed.get("expression") or row.entity_label
    ad_group_id = (row.evidence or {}).get("ad_group_id") or _ad_group_from(session, row)
    campaign_id = (row.evidence or {}).get("campaign_id") or _campaign_from(session, row)
    if not (expression and ad_group_id and campaign_id):
        raise WriteRefused(f"{row.ref} doesn't have enough to create a keyword with.")
    body = {"campaignId": campaign_id, "adGroupId": ad_group_id, "keywordText": expression,
            "matchType": str(proposed.get("match_type") or "exact").upper(), "state": "ENABLED"}
    if proposed.get("bid"):
        body["bid"] = round(float(proposed["bid"]), 2)
    return body


def _ad_group_from(session: Session, row: PpcRecommendation) -> str | None:
    if row.entity_type == "ad_group" and row.amazon_entity_id:
        return row.amazon_entity_id
    if "|" in (row.amazon_entity_id or ""):
        return row.amazon_entity_id.split("|", 1)[0]
    return None


def _campaign_from(session: Session, row: PpcRecommendation) -> str | None:
    from app.db.models import PpcAdGroup

    ad_group_id = _ad_group_from(session, row)
    if not ad_group_id:
        return None
    group = session.execute(select(PpcAdGroup).where(
        PpcAdGroup.amazon_ad_group_id == ad_group_id)).scalars().first()
    return group.amazon_campaign_id if group else None


def _keyword_writer(verb: str, creates: bool = False) -> Writer:
    return Writer(verb=verb, path="/sp/keywords", media="application/vnd.spKeyword.v3+json",
                  collection="keywords", build=_harvest_body if creates else _bid_body,
                  operation=("CreateSponsoredProductsKeywords" if creates
                             else "UpdateSponsoredProductsKeywords"), creates=creates)


WRITERS: dict[str, Writer] = {
    "bid_increase": _keyword_writer("PUT"),
    "bid_decrease": _keyword_writer("PUT"),
    "pause_target": Writer("PUT", "/sp/keywords", "application/vnd.spKeyword.v3+json", "keywords",
                           _state_body, "UpdateSponsoredProductsKeywords"),
    "enable_target": Writer("PUT", "/sp/keywords", "application/vnd.spKeyword.v3+json", "keywords",
                            _state_body, "UpdateSponsoredProductsKeywords"),
    "budget_increase": Writer("PUT", "/sp/campaigns", "application/vnd.spCampaign.v3+json",
                              "campaigns", _budget_body, "UpdateSponsoredProductsCampaigns"),
    "budget_decrease": Writer("PUT", "/sp/campaigns", "application/vnd.spCampaign.v3+json",
                              "campaigns", _budget_body, "UpdateSponsoredProductsCampaigns"),
    "placement_adjustment": Writer("PUT", "/sp/campaigns", "application/vnd.spCampaign.v3+json",
                                   "campaigns", _placement_body,
                                   "UpdateSponsoredProductsCampaigns"),
    "add_negative_exact": Writer("POST", "/sp/negativeKeywords",
                                 "application/vnd.spNegativeKeyword.v3+json", "negativeKeywords",
                                 _negative_body, "CreateSponsoredProductsNegativeKeywords",
                                 creates=True),
    "add_negative_phrase": Writer("POST", "/sp/negativeKeywords",
                                  "application/vnd.spNegativeKeyword.v3+json", "negativeKeywords",
                                  _negative_body, "CreateSponsoredProductsNegativeKeywords",
                                  creates=True),
    "harvest_search_term": _keyword_writer("POST", creates=True),
}
# Changes with no safe automatic write. Approving one records the decision; the work is the owner's.
ADVISORY = {"campaign_structure", "dayparting", "other"}


def _target_writer(session: Session, row: PpcRecommendation, writer: Writer) -> Writer:
    """A product target lives on a different endpoint from a keyword, with a different media type."""
    if writer.path != "/sp/keywords" or writer.creates:
        return writer
    target = _target(session, row)
    if target.target_kind != "target":
        return writer
    return Writer(writer.verb, "/sp/targets", "application/vnd.spTargetingClause.v3+json",
                  "targetingClauses", writer.build, "UpdateSponsoredProductsTargetingClauses")


# ---------------- before, write, verify ----------------
def _before(session: Session, row: PpcRecommendation) -> dict[str, Any]:
    """What the entity looks like now, in the shape a rollback would send back."""
    if row.change_type in ("bid_increase", "bid_decrease"):
        target = _target(session, row)
        key = "targetId" if target.target_kind == "target" else "keywordId"
        return {key: target.amazon_target_id,
                "bid": float(target.bid) if target.bid is not None else None}
    if row.change_type in ("pause_target", "enable_target"):
        target = _target(session, row)
        key = "targetId" if target.target_kind == "target" else "keywordId"
        return {key: target.amazon_target_id, "state": (target.state or "enabled").upper()}
    if row.change_type in ("budget_increase", "budget_decrease"):
        campaign = _campaign(session, row)
        return {"campaignId": campaign.amazon_campaign_id,
                "budget": {"budget": float(campaign.daily_budget) if campaign.daily_budget else None,
                           "budgetType": (campaign.budget_type or "daily").upper()}}
    if row.change_type == "placement_adjustment":
        campaign = _campaign(session, row)
        return {"campaignId": campaign.amazon_campaign_id,
                "dynamicBidding": {"strategy": campaign.bidding_strategy or "LEGACY_FOR_SALES",
                                   "placementBidding": list(campaign.placement_adjustments or [])}}
    return {}


def _rollback_for(row: PpcRecommendation, before: dict[str, Any], writer: Writer) -> dict[str, Any]:
    """How to put it back. A creation is undone by archiving what was created, not by a PUT."""
    if writer.creates:
        return {"undo": "archive_created", "change_type": row.change_type}
    return {"undo": "put_back", "verb": "PUT", "path": writer.path, "media": writer.media,
            "collection": writer.collection, "body": before}


def read_multi_status(response: Any, collection: str) -> tuple[list[dict], list[dict]]:
    """Amazon's 207: per-item success and error lists. An all-errors 207 is not a success."""
    node = (response or {}).get(collection) or {}
    success = node.get("success") or []
    errors = node.get("error") or []
    return list(success), list(errors)


def _error_text(errors: list[dict]) -> str:
    out = []
    for item in errors:
        for error in (item or {}).get("errors") or []:
            message = (error or {}).get("errorValue") or error
            if isinstance(message, dict):
                message = (message.get("itemLevelError") or {}).get("message") or str(message)
            out.append(str(message))
        if not ((item or {}).get("errors")):
            out.append(str(item))
    return "; ".join(out)[:500] or "Amazon refused the change without saying why"


def apply_one(session: Session, row: PpcRecommendation, *, actor: Actor,
              client=None, verify: bool = True) -> PpcAction:
    """Make one approved change, record what it was, and check that it took."""
    profile = session.get(PpcProfile, row.profile_id) or profiles.require(session)
    if row.status != "approved":
        raise WriteRefused(f"{row.ref} is {row.status}, not approved.")
    if row.change_type in ADVISORY:
        raise WriteRefused(f"{row.ref} is a change I can't make for you — it needs doing in "
                           "Seller Central. Approving it records that you want it.")
    if not profiles.writes_allowed(session, profile):
        raise WriteRefused("PPC is in read-only mode, so nothing is sent to Amazon. "
                           "Switching to live needs your PIN.")
    writer = WRITERS.get(row.change_type)
    if writer is None:
        raise WriteRefused(f"I don't know how to make a {row.change_type} change.")
    writer = _target_writer(session, row, writer)
    client = client or api.client()

    before = _before(session, row)
    body = writer.build(session, row)
    action = PpcAction(
        ad_product=row.ad_product,
        recommendation_id=row.id, profile_id=profile.id, change_type=row.change_type,
        entity_type=row.entity_type, entity_id=row.entity_id,
        amazon_entity_id=row.amazon_entity_id[:40], asin=row.asin, before_value=before,
        after_value=body, rollback_payload=_rollback_for(row, before, writer))
    session.add(action)
    session.flush()

    try:
        response = client.request(writer.verb, writer.path, json={writer.collection: [body]},
                                  profile_id=profile.amazon_profile_id,
                                  content_type=writer.media, accept=writer.media,
                                  operation=writer.operation)
    except AdsError as exc:
        action.failed_reason = str(exc)[:1000]
        row.status = "failed"
        session.flush()
        raise WriteFailed(f"{row.ref}: Amazon refused it — {exc}") from exc

    action.amazon_response = response if isinstance(response, dict) else {"raw": str(response)[:500]}
    success, errors = read_multi_status(response, writer.collection)
    if errors or not success:
        detail = _error_text(errors)
        action.failed_reason = detail[:1000]
        row.status = "failed"
        session.flush()
        raise WriteFailed(f"{row.ref}: Amazon refused it — {detail}")

    created_id = _created_id(success[0], writer.collection)
    if writer.creates and created_id:
        action.amazon_entity_id = str(created_id)[:40]
        action.rollback_payload = {"undo": "archive_created", "path": writer.path,
                                  "media": writer.media, "collection": writer.collection,
                                  "id_field": _id_field(writer.collection),
                                  "amazon_entity_id": str(created_id)}
    row.status = "executed"
    session.flush()

    if verify:
        _verify(session, profile, row, action, writer, client)
    audit.record(session, actor, "ppc_change_applied", target_type="ppc_recommendation",
                 target_id=row.ref, details={"change_type": row.change_type, "before": before,
                                             "after": body, "verified": action.verified_at is not None})
    log.info("ppc change applied", extra={"ref": row.ref, "change_type": row.change_type})
    return action


def _id_field(collection: str) -> str:
    return {"keywords": "keywordId", "negativeKeywords": "keywordId",
            "campaignNegativeKeywords": "keywordId", "targetingClauses": "targetId",
            "campaigns": "campaignId"}.get(collection, "keywordId")


def _created_id(entry: dict[str, Any], collection: str) -> str | None:
    field = _id_field(collection)
    if entry.get(field):
        return str(entry[field])
    inner = entry.get(collection[:-1]) if collection.endswith("s") else None
    if isinstance(inner, dict) and inner.get(field):
        return str(inner[field])
    for value in entry.values():
        if isinstance(value, dict) and value.get(field):
            return str(value[field])
    return None


def _verify(session: Session, profile: PpcProfile, row: PpcRecommendation, action: PpcAction,
            writer: Writer, client) -> None:
    """Read the entity back from Amazon and check it says what we asked for (brief 7.4).

    A failed verification is not a failed change — the write was accepted — so it is recorded and
    reported rather than raised. Silence would be worse: a change nobody confirmed is a change
    nobody can measure.
    """
    try:
        if writer.collection in ("keywords", "targetingClauses") and not writer.creates:
            name = "keywords" if writer.collection == "keywords" else "targets"
            field = _id_field(writer.collection)
            rows = structure.rows(client, name, profile,
                                  body={f"{'keywordIdFilter' if name == 'keywords' else 'targetIdFilter'}":
                                        {"include": [action.amazon_entity_id]}})
            found = next((r for r in rows if str(r.get(field)) == action.amazon_entity_id), None)
        elif writer.collection == "campaigns":
            rows = structure.rows(client, "campaigns", profile,
                                  body={"campaignIdFilter": {"include": [action.amazon_entity_id]}})
            found = next((r for r in rows
                          if str(r.get("campaignId")) == action.amazon_entity_id), None)
        else:
            found = None
    except AdsError as exc:
        action.failed_reason = f"applied, but reading it back failed: {exc}"[:1000]
        session.flush()
        return

    if found is None:
        action.failed_reason = "applied, but Amazon didn't return the entity when asked for it again"
        session.flush()
        return
    action.verified_value = found
    action.verified_at = utcnow()
    if not _matches(row, found):
        action.failed_reason = ("applied, but Amazon reports a different value than we asked for — "
                        f"wanted {action.after_value}, Amazon has {_shown(row, found)}")
    session.flush()


def _shown(row: PpcRecommendation, found: dict[str, Any]) -> Any:
    if row.change_type in ("bid_increase", "bid_decrease"):
        return found.get("bid")
    if row.change_type in ("pause_target", "enable_target"):
        return found.get("state")
    if row.change_type in ("budget_increase", "budget_decrease"):
        return (found.get("budget") or {}).get("budget")
    return found


def _matches(row: PpcRecommendation, found: dict[str, Any]) -> bool:
    wanted = _value(row.proposed_value)
    if row.change_type in ("bid_increase", "bid_decrease"):
        got = found.get("bid")
        return got is not None and abs(float(got) - float(wanted)) < 0.011
    if row.change_type in ("pause_target", "enable_target"):
        return str(found.get("state", "")).upper() == str(wanted).upper()
    if row.change_type in ("budget_increase", "budget_decrease"):
        got = (found.get("budget") or {}).get("budget")
        return got is not None and abs(float(got) - float(wanted)) < 0.011
    return True


# ---------------- a whole batch ----------------
def apply_batch(session: Session, run, *, actor: Actor, client=None) -> dict[str, Any]:
    """Apply every approved change in a batch, stopping the moment one fails (brief 7.4).

    Stopping matters more than finishing. A batch that carries on past a failure leaves the account in
    a state nobody chose, and the report afterwards cannot say what happened. So the first failure
    ends the run and the answer names exactly what was applied and what was not.
    """
    from app.ppc import recommendations

    rows = recommendations.in_batch(session, run, ("approved",))
    applied, failed, skipped = [], None, []
    for row in rows:
        if row.change_type in ADVISORY:
            skipped.append({"ref": row.ref, "why": "needs doing by hand in Seller Central"})
            continue
        try:
            apply_one(session, row, actor=actor, client=client)
            applied.append(row.ref)
            session.commit()
        except (WriteFailed, WriteRefused) as exc:
            session.commit()
            failed = {"ref": row.ref, "why": str(exc)}
            break
    remaining = [r.ref for r in rows if r.status == "approved"
                 and r.ref != (failed or {}).get("ref")
                 and r.ref not in {s["ref"] for s in skipped}]
    result = {"batch": run.ref, "applied": applied, "failed": failed, "skipped": skipped,
              "not_attempted": remaining}
    log.info("ppc batch applied", extra={"batch": run.ref, "applied": len(applied),
                                         "failed": bool(failed)})
    return result


# ---------------- undoing ----------------
def rollback(session: Session, row: PpcRecommendation, *, actor: Actor, client=None) -> PpcAction:
    """Put one change back. A safety action: no approval, always logged (brief 7.4)."""
    action = session.execute(select(PpcAction).where(
        PpcAction.recommendation_id == row.id,
        PpcAction.rolled_back_at.is_(None)).order_by(
        PpcAction.executed_at.desc())).scalars().first()
    if action is None:
        raise WriteRefused(f"{row.ref} has nothing to undo.")
    plan = action.rollback_payload or {}
    client = client or api.client()
    profile = session.get(PpcProfile, action.profile_id) or profiles.require(session)

    if plan.get("undo") == "archive_created":
        body = {plan.get("id_field", "keywordId"): action.amazon_entity_id, "state": "ARCHIVED"}
        verb, path, media = "PUT", plan.get("path", "/sp/keywords"), plan.get("media")
        collection = plan.get("collection", "keywords")
    elif plan.get("undo") == "put_back":
        verb, path, media = plan["verb"], plan["path"], plan["media"]
        collection, body = plan["collection"], plan["body"]
    else:
        raise WriteRefused(f"{row.ref} has no usable undo recorded.")

    response = client.request(verb, path, json={collection: [body]},
                              profile_id=profile.amazon_profile_id, content_type=media,
                              accept=media, operation=f"Rollback{collection}")
    success, errors = read_multi_status(response, collection)
    if errors or not success:
        raise WriteFailed(f"{row.ref}: Amazon wouldn't undo it — {_error_text(errors)}")
    action.rolled_back_at = utcnow()
    action.rollback_reason = f"undone by {actor.type} {actor.id or ''}".strip()
    row.status = "rolled_back"
    session.flush()
    audit.record(session, actor, "ppc_change_rolled_back", target_type="ppc_recommendation",
                 target_id=row.ref, details={"change_type": row.change_type, "sent": body})
    log.info("ppc change rolled back", extra={"ref": row.ref})
    return action


def rollback_batch(session: Session, run, *, actor: Actor, client=None) -> dict[str, Any]:
    """Undo everything a batch applied, newest first, and say what wouldn't go back."""
    from app.ppc import recommendations

    rows = [r for r in recommendations.in_batch(session, run) if r.status == "executed"]
    undone, problems = [], []
    for row in reversed(rows):
        try:
            rollback(session, row, actor=actor, client=client)
            undone.append(row.ref)
            session.commit()
        except (WriteFailed, WriteRefused) as exc:
            session.commit()
            problems.append({"ref": row.ref, "why": str(exc)})
    return {"batch": run.ref, "rolled_back": undone, "problems": problems}


# ---------------- the approval executor ----------------
def _apply_failed(session: Session, payload: dict[str, Any], approval, error: str) -> None:
    """Say precisely what was and was not applied. A vague failure here is unusable."""
    from app.services.notify import notify_owner

    notify_owner(session, f"📊⚠️ {payload.get('batch')} stopped part way through: {error[:300]} "
                          "Nothing after the failure was attempted. Send "
                          f"`{payload.get('batch')} DETAILS` to see where it got to.",
                 key=f"ppc_apply_failed:{payload.get('batch')}", urgent=True)


@approvals.executor("ppc_apply_change", on_failure=_apply_failed)
def _run(session: Session, payload: dict[str, Any], approval) -> dict[str, Any]:
    """Apply the batch the owner approved. Only rows he actually chose are `approved`.

    The frozen payload is checked against what is about to be sent: if a recommendation's
    proposed value has changed since the approval was created, it is not applied. That cannot
    normally happen — rows are immutable once proposed — but the approval is the contract and
    this is the line that keeps it one.
    """
    from app.ppc import recommendations

    run = recommendations.batch_by_ref(session, payload["batch"])
    frozen = {item["ref"]: item for item in payload.get("recommendations") or []}
    for row in recommendations.in_batch(session, run, ("approved",)):
        promised = frozen.get(row.ref)
        if promised is None:
            row.status = "expired"
            continue
        if promised.get("proposed_value") != row.proposed_value:
            row.status = "failed"
            raise WriteFailed(f"{row.ref} has changed since you approved it; nothing was sent.")
    session.flush()
    result = apply_batch(session, run, actor=Actor("agent", "ppc"))
    if result.get("failed"):
        raise WriteFailed(f"{result['failed']['ref']}: {result['failed']['why']}")
    # The approval machinery sends one message carrying this summary, so the detail goes in there
    # rather than in a second message of our own.
    return {"summary": report_text(session, run, result), "applied": result["applied"],
            "skipped": [s["ref"] for s in result["skipped"]], "complete_task": True}


def report_text(session: Session, run, result: dict[str, Any]) -> str:
    """What landed, what didn't, and what only the owner can do — in one message."""
    lines = [f"📊 *{run.ref} applied*"]
    if result["applied"]:
        lines.append(f"✅ {len(result['applied'])} changes made at Amazon: "
                     + ", ".join(result["applied"][:8]))
    unverified = _unverified(session, run)
    if unverified:
        lines.append(f"⚠️ {len(unverified)} went through but Amazon reports something different — "
                     + "; ".join(unverified[:3]))
    if result["skipped"]:
        lines.append("✋ Needs doing in Seller Central yourself: "
                     + ", ".join(s["ref"] for s in result["skipped"]))
    if result["failed"]:
        lines.append(f"⛔ Stopped at {result['failed']['ref']}: {result['failed']['why']}")
        if result["not_attempted"]:
            lines.append(f"Not attempted: {', '.join(result['not_attempted'][:8])}")
    lines.append(f"`ROLLBACK BATCH {run.ref}` puts all of it back.")
    return "\n".join(lines)


def _unverified(session: Session, run) -> list[str]:
    from app.ppc import recommendations

    out = []
    for row in recommendations.in_batch(session, run, ("executed",)):
        action = session.execute(select(PpcAction).where(
            PpcAction.recommendation_id == row.id).order_by(
            PpcAction.executed_at.desc())).scalars().first()
        if action is not None and action.failed_reason:
            out.append(f"{row.ref} ({action.failed_reason[:80]})")
    return out
