"""Amazon jobs: listing sync, inventory snapshots, schema refresh."""

from __future__ import annotations

import logging

from app.amazon import accounts, inventory, listings, schemas
from app.amazon.accounts import AmazonNotConnected
from app.amazon.errors import AmazonAuthError, AmazonError, AmazonRetryable
from app.jobs.errors import PermanentError, RetryableError
from app.jobs.handlers import handler
from app.services import settings_store, users
from app.services.common import ServiceError

log = logging.getLogger(__name__)


def amazon_paused(session) -> bool:
    return bool(settings_store.is_paused(session) or settings_store.get(session, "amazon_paused", False))


def _auth_failed(session, exc: AmazonAuthError) -> None:
    """One notice, then stop trying until the owner re-authorises."""
    from app.services.notify import notify_owner

    if getattr(exc, "missing_role", False):
        # One operation is off-limits. The credentials are fine, so nothing else should stop.
        session.rollback()
        notify_owner(session, f"🛒⚠️ Amazon won't let this app do one thing: {exc} Everything else keeps "
                              "working.", key=f"amazon_missing_role:{str(exc)[:40]}")
        session.commit()
        return
    session.rollback()
    account = accounts.get_primary(session)
    if account is not None:
        accounts.set_status(session, account, "auth_error", str(exc)[:300])
    notify_owner(session, f"🛒⚠️ Amazon access stopped working ({exc}). Listing sync and alerts are paused. "
                          "Re-authorise the app in Seller Central, then reply AMAZON RESUME.",
                 key="amazon_auth_error")
    session.commit()


@handler("amazon_sync_listings")
def amazon_sync_listings(session, payload, job) -> None:
    if amazon_paused(session):
        return
    try:
        stats = listings.sync_all(session)
    except AmazonAuthError as exc:
        _auth_failed(session, exc)
        return
    except AmazonNotConnected:
        return
    except AmazonRetryable as exc:
        raise RetryableError(str(exc)) from exc
    except AmazonError as exc:
        raise PermanentError(str(exc)) from exc
    log.info("amazon listings sync", extra=stats)


@handler("amazon_inventory_snapshot")
def amazon_inventory_snapshot(session, payload, job) -> None:
    if amazon_paused(session):
        return
    try:
        inventory.snapshot(session)
    except AmazonAuthError as exc:
        _auth_failed(session, exc)
    except AmazonNotConnected:
        return
    except AmazonRetryable as exc:
        raise RetryableError(str(exc)) from exc
    except AmazonError as exc:
        raise PermanentError(str(exc)) from exc


@handler("amazon_refresh_schemas")
def amazon_refresh_schemas(session, payload, job) -> None:
    if amazon_paused(session):
        return
    try:
        log.info("amazon schema refresh", extra={"count": schemas.refresh_all(session)})
    except AmazonAuthError as exc:
        _auth_failed(session, exc)
    except AmazonNotConnected:
        return


@handler("amazon_check_submission")
def amazon_check_submission(session, payload, job) -> None:
    """Did Amazon actually apply our change? Runs at 5 min, 30 min, 2 h and 24 h."""
    from app.amazon import changes

    if amazon_paused(session):
        return
    try:
        request = changes.get_by_ref(session, payload["change_ref"])
    except Exception:
        return
    try:
        result = changes.check_submission(session, request, int(payload.get("after_minutes") or 0))
    except AmazonAuthError as exc:
        _auth_failed(session, exc)
        return
    except AmazonRetryable as exc:
        raise RetryableError(str(exc)) from exc
    log.info("amazon submission check", extra={"change": request.ref, "result": result})


@handler("fba_build_plan")
def fba_build_plan(session, payload, job) -> None:
    """Create the draft plan in Amazon and send the owner the warehouse options."""
    from app.amazon import fba_plan
    from app.amazon.errors import AmazonOperationFailed
    from app.services.notify import notify_owner

    if amazon_paused(session):
        return
    request = fba_plan.get_by_ref(session, payload["request_ref"])
    if request.status not in ("prechecks_passed", "building_plan"):
        return
    try:
        fba_plan.build_plan(session, request)
    except AmazonAuthError as exc:
        _auth_failed(session, exc)
        return
    except AmazonRetryable as exc:
        raise RetryableError(str(exc)) from exc
    except (AmazonOperationFailed, AmazonError) as exc:
        from app.amazon.errors import inbound_guidance

        request.status = "failed"
        request.error = str(exc)[:500]
        advice = inbound_guidance(str(exc))
        notify_owner(session, f"📦⚠️ {request.ref}: Amazon wouldn't build this shipment.\n{exc}"
                     + (f"\n\n{advice}" if advice else ""), urgent=True)
        return
    notify_owner(session, fba_plan.options_message(session, request), urgent=True)


@handler("fba_options_reminder")
def fba_options_reminder(session, payload, job) -> None:
    from app.amazon import fba_plan
    from app.services.notify import notify_owner

    request = fba_plan.get_by_ref(session, payload["request_ref"])
    if request.status != "options_ready":
        return
    notify_owner(session, f"⏰ {request.ref}: the warehouse options expire in about 30 minutes. Reply "
                          f"\"SHIP {request.ref} OPTION <n>\" to keep them, or they'll be regenerated later.",
                 urgent=True, key=f"fba_expiry:{request.ref}")


@handler("fba_abandon_check")
def fba_abandon_check(session, payload, job) -> None:
    """Draft plans nobody answered are cancelled, so they don't linger in Amazon."""
    from app.amazon import fba_plan
    from app.services.notify import notify_owner

    request = fba_plan.get_by_ref(session, payload["request_ref"])
    if request.status not in ("options_ready", "prechecks_passed", "building_plan", "needs_info"):
        return
    message = fba_plan.cancel_plan(session, request, reason="no reply for 72 hours")
    notify_owner(session, f"📦 {message} Tell me again when you're ready and I'll rebuild it.")


@handler("fba_transport_options")
def fba_transport_options(session, payload, job) -> None:
    """Placement is confirmed, so Amazon can now quote carriers."""
    from app.amazon import fba_confirm, fba_plan
    from app.services.notify import notify_owner

    request = fba_plan.get_by_ref(session, payload["request_ref"])
    try:
        message = fba_confirm.fetch_transport_options(session, request)
    except AmazonAuthError as exc:
        _auth_failed(session, exc)
        return
    except AmazonRetryable as exc:
        raise RetryableError(str(exc)) from exc
    except AmazonError as exc:
        what = ("register a carrier" if request.selected_transport_choice == "own_carrier"
                else "price the carriers")
        notify_owner(session, f"🚚⚠️ {request.ref}: Amazon couldn't {what} ({exc}). The warehouses are "
                              f"confirmed; reply SHIP {request.ref} CARRIER to try again.", urgent=True)
        return
    if request.selected_transport_choice == "own_carrier":
        # Amazon isn't quoting anything — it just needs to know who's bringing the boxes. If the owner's
        # usual carrier is on the list, ask for the one approval instead of showing him a menu.
        try:
            notify_owner(session, fba_confirm.choose_carrier(session, request, user=users.get_owner(session)),
                         urgent=True)
            return
        except ServiceError:
            pass                    # his usual carrier isn't offered; fall through to the list
    notify_owner(session, message, urgent=True)


@handler("fba_fetch_labels")
def fba_fetch_labels(session, payload, job) -> None:
    from app.amazon import fba_confirm, fba_plan
    from app.services.notify import notify_owner
    from app.services import users
    from app.services.outbound import queue_document

    request = fba_plan.get_by_ref(session, payload["request_ref"])
    try:
        labels = fba_confirm.fetch_labels(session, request)
    except AmazonAuthError as exc:
        if getattr(exc, "missing_role", False):
            # The shipment is real and confirmed either way; only the label download is blocked.
            ids = ", ".join(s.shipment_confirmation_id or s.amazon_shipment_id
                            for s in fba_confirm.shipments_of(session, request))
            notify_owner(session, f"🏷️ {request.ref} is confirmed, but Amazon won't let this app download "
                                  f"the labels ({exc.args[0].split(' — ')[0]}).\nPrint them in Seller Central: "
                                  f"Inventory → Shipments → {ids} → Print box labels.\nEverything else about "
                                  "the shipment works normally.", urgent=True)
            return
        _auth_failed(session, exc)
        return
    except AmazonRetryable as exc:
        raise RetryableError(str(exc)) from exc
    if not labels:
        notify_owner(session, f"🏷️ {request.ref}: Amazon hasn't produced labels yet. Reply LABELS "
                              f"{request.ref} in a few minutes and I'll fetch them again.", urgent=True)
        return
    notify_owner(session, fba_confirm.labels_message(session, request, labels), urgent=True)
    owner = users.get_owner(session)
    for label in labels:
        if owner:
            queue_document(session, owner, label["file"], caption=f"{request.ref} {label['kind']} labels")


@handler("fba_sync_status")
def fba_sync_status(session, payload, job) -> None:
    from app.amazon import fba_confirm, fba_plan
    from app.db.models import FbaInboundRequest
    from sqlalchemy import select as _select

    if amazon_paused(session):
        return
    open_statuses = ("confirmed", "in_transit", "receiving")
    requests = list(session.execute(_select(FbaInboundRequest)
                                    .where(FbaInboundRequest.status.in_(open_statuses))).scalars())
    for request in requests:
        try:
            changed = fba_confirm.sync_status(session, request)
        except AmazonAuthError as exc:
            _auth_failed(session, exc)
            return
        except (AmazonRetryable, AmazonError) as exc:
            log.warning("shipment sync failed", extra={"request": request.ref, "error": str(exc)[:150]})
            continue
        if changed:
            log.info("fba shipment status", extra={"request": request.ref, "changed": changed})
        session.commit()


@handler("amazon_collect_sales")
def amazon_collect_sales(session, payload, job) -> None:
    from app.amazon import monitor

    if amazon_paused(session):
        return
    try:
        monitor.collect_sales(session)
    except AmazonAuthError as exc:
        _auth_failed(session, exc)
        return
    except AmazonRetryable as exc:
        raise RetryableError(str(exc)) from exc
    except AmazonError as exc:
        log.warning("sales report failed", extra={"error": str(exc)[:150]})


@handler("amazon_collect_health")
def amazon_collect_health(session, payload, job) -> None:
    """Suppressed listings and stranded inventory, then every detection rule."""
    from app.amazon import monitor

    if amazon_paused(session):
        return
    try:
        monitor.collect_health(session)
    except AmazonAuthError as exc:
        _auth_failed(session, exc)
        return
    except AmazonRetryable as exc:
        raise RetryableError(str(exc)) from exc
    except AmazonError as exc:
        log.warning("health reports failed", extra={"error": str(exc)[:150]})
    log.info("amazon detection", extra=monitor.detect(session))
    monitor.link_amazon_emails(session)


@handler("amazon_detect")
def amazon_detect(session, payload, job) -> None:
    from app.amazon import monitor

    if amazon_paused(session):
        return
    log.info("amazon detection", extra=monitor.detect(session))
