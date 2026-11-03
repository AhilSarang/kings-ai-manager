"""Watching the account: collectors, detection rules and alerts (brief Section 13).

Detection is plain code. The LLM is only ever used to phrase an explanation, never to decide whether
something is wrong and never to do arithmetic.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.amazon import inventory as inv
from app.amazon import listings as listings_svc
from app.amazon import marketplaces as mkt
from app.amazon import reports
from app.db.models import AmazonAlert, AmazonListing, AmazonSalesDaily, EmailThread
from app.services import settings_store
from app.services.common import Actor, utcnow
from app.services.formatting import truncate

log = logging.getLogger(__name__)
AGENT = Actor.agent("amazon")

BATCH_THRESHOLD = 3          # more than this in one sweep and the owner gets one message, not a stream
INSTANT_TYPES = {"listing_suppressed", "listing_issue", "stockout", "shipment_problem",
                 "receiving_discrepancy"}
STOCKOUT_SALES_WINDOW_DAYS = 14
URGENT_COVER_DAYS = 7


# ---------------- collectors ----------------
def collect_sales(session: Session, marketplace_code: str = "US", *, days: int = 3) -> int:
    """Sales & Traffic by ASIN. Re-runs the last few days, because Amazon revises them."""
    mp = mkt.by_code(marketplace_code)
    end = utcnow().date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    payload = reports.fetch_json(reports.SALES_AND_TRAFFIC, marketplace_code,
                                 options={"dateGranularity": "DAY", "asinGranularity": "CHILD"},
                                 start=f"{start}T00:00:00Z", end=f"{end}T23:59:59Z")
    rows = payload.get("salesAndTrafficByAsin") or []
    stored = 0
    for row in rows:
        asin = row.get("childAsin") or row.get("parentAsin")
        when = _date(row.get("date")) or end
        sales = row.get("salesByAsin") or {}
        traffic = row.get("trafficByAsin") or {}
        units = int(sales.get("unitsOrdered") or 0)
        amount = (sales.get("orderedProductSales") or {}).get("amount")
        # The report is per ASIN, so the natural key is (marketplace, asin, date) and the SKU is
        # only a label. Both lookups are ordered: an unordered `first()` could pick a different
        # listing on a re-run, miss the row it wrote last time, and insert a second row for the same
        # ASIN and day — which silently halves TACOS and doubles reported sales.
        listing = session.execute(select(AmazonListing).where(
            AmazonListing.asin == asin,
            AmazonListing.marketplace_id == mp.marketplace_id).order_by(
            AmazonListing.created_at)).scalars().first()
        existing = session.execute(select(AmazonSalesDaily).where(
            AmazonSalesDaily.marketplace_id == mp.marketplace_id, AmazonSalesDaily.asin == asin,
            AmazonSalesDaily.date == when).order_by(AmazonSalesDaily.sku)).scalars().first()
        record = existing or AmazonSalesDaily(marketplace_id=mp.marketplace_id, asin=asin,
                                              sku=listing.sku if listing else "", date=when)
        record.units = units
        record.ordered_product_sales = float(amount) if amount is not None else None
        record.currency = (sales.get("orderedProductSales") or {}).get("currencyCode")
        record.sessions = traffic.get("sessions")
        record.page_views = traffic.get("pageViews")
        record.unit_session_pct = traffic.get("unitSessionPercentage")
        record.buy_box_pct = traffic.get("buyBoxPercentage")
        if existing is None:
            session.add(record)
        stored += 1
    session.commit()
    log.info("amazon sales collected", extra={"rows": stored})
    return stored


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def collect_health(session: Session, marketplace_code: str = "US") -> dict[str, int]:
    """Suppressed listings and stranded inventory, straight from Amazon's own reports."""
    counts = {"suppressed": 0, "stranded": 0}
    for row in reports.fetch(reports.LISTINGS_DEFECTS, marketplace_code):
        sku = row.get("sku") or row.get("seller-sku")
        if not sku:
            continue
        reason = row.get("reason") or row.get("issue-description") or "Amazon suppressed this listing"
        raise_alert(session, type_="listing_suppressed", sku=sku, marketplace_code=marketplace_code,
                    details={"message": f"{sku} is suppressed: {reason}",
                             "status": row.get("status"), "reason": reason})
        counts["suppressed"] += 1
    for row in reports.fetch(reports.STRANDED, marketplace_code):
        sku = row.get("sku") or row.get("seller-sku")
        if not sku:
            continue
        raise_alert(session, type_="stranded", sku=sku, marketplace_code=marketplace_code,
                    details={"message": f"{sku} has stranded inventory: "
                                        f"{row.get('stranded-reason') or 'no active listing'}",
                             "units": row.get('quantity') or row.get('afn-fulfillable-quantity')})
        counts["stranded"] += 1
    session.commit()
    return counts


# ---------------- alerts ----------------
def open_alert(session: Session, type_: str, sku: str | None) -> AmazonAlert | None:
    return session.execute(select(AmazonAlert).where(
        AmazonAlert.type == type_, AmazonAlert.sku == sku,
        AmazonAlert.status == "open")).scalars().first()


def raise_alert(session: Session, *, type_: str, sku: str | None, marketplace_code: str = "US",
                details: dict[str, Any], severity: str | None = None,
                asin: str | None = None, notify: bool = True) -> AmazonAlert | None:
    """One open alert per SKU and type: nothing is repeated until it's resolved."""
    if open_alert(session, type_, sku) is not None:
        return None
    instant = type_ in INSTANT_TYPES
    alert = AmazonAlert(type=type_, severity=severity or ("high" if instant else "medium"),
                        marketplace_id=mkt.by_code(marketplace_code).marketplace_id, sku=sku, asin=asin,
                        details=details, status="open", alerted_at=utcnow() if instant and notify else None)
    session.add(alert)
    session.flush()
    if instant and notify:
        _notify(session, alert)
    return alert


def notify_new(session: Session, alerts: list[AmazonAlert]) -> None:
    """One message each, or a single combined one when a sweep turns up a pile of them."""
    from app.services.notify import notify_owner

    instant = [a for a in alerts if a is not None and a.type in INSTANT_TYPES and a.alerted_at is None]
    if not instant:
        return
    if len(instant) <= BATCH_THRESHOLD:
        for alert in instant:
            alert.alerted_at = utcnow()
            _notify(session, alert)
        session.flush()
        return
    by_type: dict[str, list[AmazonAlert]] = {}
    for alert in instant:
        by_type.setdefault(alert.type, []).append(alert)
        alert.alerted_at = utcnow()
    lines = [f"🛒⚠️ {len(instant)} Amazon problems need you:"]
    for type_, rows in by_type.items():
        skus = sorted({r.sku for r in rows if r.sku})
        more = f" and {len(skus) - 5} more" if len(skus) > 5 else ""
        lines.append(f"• {TYPE_WORDS.get(type_, type_)} ({len(rows)}): {', '.join(skus[:5])}{more}")
    lines.append("Reply ISSUES for the full list, or a SKU for one of them.")
    session.flush()
    notify_owner(session, "\n".join(lines), urgent=True, key=f"amazon_alerts:{utcnow():%Y%m%d%H%M}")


def resolve_alerts(session: Session, type_: str, sku: str) -> None:
    for alert in session.execute(select(AmazonAlert).where(
            AmazonAlert.type == type_, AmazonAlert.sku == sku, AmazonAlert.status == "open")).scalars():
        alert.status = "resolved"
    session.flush()


def _notify(session: Session, alert: AmazonAlert) -> None:
    from app.services.notify import notify_owner

    notify_owner(session, alert_message(session, alert), urgent=True, key=f"amazon_alert:{alert.ref}")


TYPE_WORDS = {
    "listing_suppressed": "Listing suppressed", "listing_issue": "Listing problem",
    "buy_box_lost": "Buy Box lost", "low_stock": "Low stock", "stockout": "Out of stock",
    "stranded": "Stranded inventory", "sales_drop": "Sales drop", "price_anomaly": "Price changed",
    "submission_rejected": "Amazon rejected a change", "shipment_problem": "Shipment problem",
    "receiving_discrepancy": "Shipment received short",
}


def alert_message(session: Session, alert: AmazonAlert) -> str:
    listing = session.execute(select(AmazonListing).where(AmazonListing.sku == alert.sku)).scalars().first() \
        if alert.sku else None
    lines = [f"🛒 AMAZON ALERT — {TYPE_WORDS.get(alert.type, alert.type)} ({alert.ref})"]
    if listing:
        lines.append(f"{listing.ref} {listing.sku} — {truncate(listing.title_cached or '', 60)}")
    elif alert.sku:
        lines.append(alert.sku)
    lines.append(alert.details.get("message", ""))
    if alert.details.get("suggestion"):
        lines.append(f"Suggested: {alert.details['suggestion']}")
    if listing:
        lines.append(f'Reply "{listing.ref}" for the listing, or tell me the fix you want.')
    return "\n".join(line for line in lines if line)


# ---------------- detection ----------------
def detect(session: Session, marketplace_code: str = "US") -> dict[str, int]:
    """Every rule from brief 13.2. Plain arithmetic, no LLM."""
    found = {"listing_issue": 0, "not_buyable": 0, "low_stock": 0, "stockout": 0, "sales_drop": 0,
             "price_anomaly": 0}
    cover_days = float(settings_store.get(session, "amazon_low_stock_days_cover", 21) or 21)
    drop_pct = float(settings_store.get(session, "amazon_sales_drop_alert_pct", 40) or 40)

    new_alerts: list[AmazonAlert] = []

    def raise_(**kwargs) -> AmazonAlert | None:
        alert = raise_alert(session, notify=False, **kwargs)   # one message at the end, not one per SKU
        if alert is not None:
            new_alerts.append(alert)
        return alert

    for listing in session.execute(select(AmazonListing)).scalars():
        errors = [i for i in (listing.issues_cached or []) if (i.get("severity") or "").upper() == "ERROR"]
        snap = inv.for_sku(session, listing.sku)
        status = (listing.status_cached or "").upper()
        if errors:
            if raise_(type_="listing_issue", sku=listing.sku, asin=listing.asin,
                      marketplace_code=mkt.by_id(listing.marketplace_id).code,
                      details={"message": f"{listing.sku}: {errors[0].get('message')}",
                               "suggestion": "Tell me the fix and I'll draft the change."}):
                found["listing_issue"] += 1
        else:
            resolve_alerts(session, "listing_issue", listing.sku)

        # Not buyable *because there is nothing to sell* is a stock story, not a suppression. Amazon
        # keeps such a listing DISCOVERABLE; a real suppression has errors, lost discoverability, or
        # stock sitting there that nobody can buy.
        blocked = bool(errors) or "DISCOVERABLE" not in status or bool(snap and snap.fulfillable_qty > 0)
        if not listing.buyable and status and "DELETED" not in status and blocked:
            if raise_(type_="listing_suppressed", sku=listing.sku, asin=listing.asin,
                      marketplace_code=mkt.by_id(listing.marketplace_id).code,
                      details={"message": f"{listing.sku} is no longer buyable "
                                          f"({listing.status_cached})."}):
                found["not_buyable"] += 1
        elif listing.buyable:
            resolve_alerts(session, "listing_suppressed", listing.sku)

        if _sales_dropped(session, listing.sku, drop_pct):
            if raise_(type_="sales_drop", sku=listing.sku, asin=listing.asin,
                      details={"message": f"{listing.sku} sold {_units_since(session, listing.sku, 7)} "
                                          f"units in the last 7 days, down more than {drop_pct:g}% on the "
                                          "7 days before."}):
                found["sales_drop"] += 1

        if snap is None:
            continue  # stock rules need a snapshot; the rules above don't
        cover = inv.days_of_cover(session, listing.sku)
        recent_units = _units_since(session, listing.sku, STOCKOUT_SALES_WINDOW_DAYS)
        if snap.fulfillable_qty == 0 and recent_units > 0:
            if raise_(type_="stockout", sku=listing.sku, asin=listing.asin,
                      details={"message": f"{listing.sku} is out of stock at Amazon and sold "
                                          f"{recent_units} units in the last {STOCKOUT_SALES_WINDOW_DAYS} days."
                                          + (f" {snap.inbound_qty} units are inbound."
                                             if snap.inbound_qty else " Nothing is inbound."),
                               "suggestion": "Send more stock, or I can draft an FBA shipment."}):
                found["stockout"] += 1
        elif cover is not None and cover < cover_days:
            severity = "high" if cover < URGENT_COVER_DAYS else "medium"
            alert = raise_(type_="low_stock", sku=listing.sku, asin=listing.asin, severity=severity,
                           details={"message": f"{listing.sku} has about {cover:g} days of cover "
                                               f"({snap.fulfillable_qty} units"
                                               + (f", {snap.inbound_qty} inbound" if snap.inbound_qty
                                                  else ", nothing inbound") + ").",
                                    "cover_days": cover})
            if alert is not None:
                found["low_stock"] += 1
                if cover < URGENT_COVER_DAYS:  # under a week is worth interrupting for
                    alert.alerted_at = utcnow()
                    _notify(session, alert)
                    new_alerts.remove(alert)
        else:
            resolve_alerts(session, "low_stock", listing.sku)
            resolve_alerts(session, "stockout", listing.sku)

    notify_new(session, new_alerts)
    session.flush()
    return found


def _units_since(session: Session, sku: str, days: int, *, offset: int = 0) -> int:
    end = utcnow().date() - timedelta(days=offset)
    start = end - timedelta(days=days)
    return int(session.execute(select(func.coalesce(func.sum(AmazonSalesDaily.units), 0)).where(
        AmazonSalesDaily.sku == sku, AmazonSalesDaily.date > start, AmazonSalesDaily.date <= end)).scalar_one())


def _sales_dropped(session: Session, sku: str, drop_pct: float, *, baseline_minimum: int = 10) -> bool:
    previous = _units_since(session, sku, 7, offset=7)
    if previous < baseline_minimum:  # too small to read anything into
        return False
    recent = _units_since(session, sku, 7)
    return (previous - recent) / previous * 100 >= drop_pct


def price_changed_outside(session: Session, listing: AmazonListing, old_price: float | None,
                          new_price: float | None) -> None:
    """Called by the sync when a price moved without one of our change requests."""
    if old_price is None or new_price is None or abs(float(old_price) - float(new_price)) < 0.01:
        return
    raise_alert(session, type_="price_anomaly", sku=listing.sku, asin=listing.asin,
                marketplace_code=mkt.by_id(listing.marketplace_id).code,
                details={"message": f"{listing.sku} changed price from {float(old_price):,.2f} to "
                                    f"{float(new_price):,.2f} without going through me."})


# ---------------- views ----------------
def issues_command(session: Session) -> str:
    rows = list(session.execute(select(AmazonListing).order_by(AmazonListing.sku)).scalars())
    with_errors = [(r, [i for i in (r.issues_cached or []) if (i.get("severity") or "").upper() == "ERROR"])
                   for r in rows]
    with_errors = [(r, e) for r, e in with_errors if e]
    warnings = [(r, [i for i in (r.issues_cached or []) if (i.get("severity") or "").upper() == "WARNING"])
                for r in rows]
    warnings = [(r, w) for r, w in warnings if w and not any(r is x for x, _ in with_errors)]
    if not with_errors and not warnings:
        return "✅ No listing errors or warnings from Amazon."
    lines = ["🛒 *Listing issues*"]
    for listing, issues in with_errors:
        lines.append(f"❗ {listing.ref} {listing.sku}")
        lines += [f"   {truncate(i.get('message') or i.get('code') or '', 140)}" for i in issues[:3]]
    for listing, issues in warnings:
        lines.append(f"⚠️ {listing.ref} {listing.sku}")
        lines += [f"   {truncate(i.get('message') or i.get('code') or '', 140)}" for i in issues[:2]]
    lines.append("\nTell me the fix you want and I'll draft it.")
    return "\n".join(lines)


def sales_command(session: Session) -> str:
    yesterday = utcnow().date() - timedelta(days=1)
    day_rows = list(session.execute(select(AmazonSalesDaily).where(AmazonSalesDaily.date == yesterday)
                                    .order_by(AmazonSalesDaily.units.desc())).scalars())
    if not day_rows:
        return "No sales data yet. The daily sales report runs each morning."
    units = sum(r.units for r in day_rows)
    revenue = sum(float(r.ordered_product_sales or 0) for r in day_rows)
    currency = next((r.currency for r in day_rows if r.currency), "USD")
    week_units = int(session.execute(select(func.coalesce(func.sum(AmazonSalesDaily.units), 0)).where(
        AmazonSalesDaily.date > yesterday - timedelta(days=7))).scalar_one())
    week_revenue = float(session.execute(
        select(func.coalesce(func.sum(AmazonSalesDaily.ordered_product_sales), 0)).where(
            AmazonSalesDaily.date > yesterday - timedelta(days=7))).scalar_one())
    lines = [f"📈 *Amazon sales*",
             f"Yesterday ({yesterday:%a %b %d}): {units} units, {currency} {revenue:,.2f}",
             f"Last 7 days: {week_units} units, {currency} {week_revenue:,.2f}", "", "*Yesterday's best*"]
    for row in day_rows[:5]:
        listing = session.execute(select(AmazonListing).where(AmazonListing.asin == row.asin)).scalars().first()
        name = listing.sku if listing else row.asin
        lines.append(f"• {name}: {row.units} units"
                     + (f", {currency} {float(row.ordered_product_sales):,.2f}"
                        if row.ordered_product_sales else ""))
    return "\n".join(lines)


def daily_sections(session: Session) -> list[str]:
    """Blocks for the morning summary (brief Section 14)."""
    sections: list[str] = []
    pending = list(session.execute(select(AmazonAlert).where(AmazonAlert.status == "open")
                                   .order_by(AmazonAlert.created_at.desc())).scalars())
    by_type: dict[str, list[AmazonAlert]] = {}
    for alert in pending:
        by_type.setdefault(alert.type, []).append(alert)
    for type_, alerts in by_type.items():
        if type_ in ("listing_suppressed", "listing_issue"):
            sections.append("🛒 *Amazon listing problems*\n"
                            + "\n".join(f"• {a.details.get('message', a.sku)}" for a in alerts[:6]))
        elif type_ in ("low_stock", "stockout"):
            sections.append("📦 *Stock to watch*\n"
                            + "\n".join(f"• {a.details.get('message', a.sku)}" for a in alerts[:6]))
        elif type_ == "stranded":
            sections.append("🚧 *Stranded inventory*\n"
                            + "\n".join(f"• {a.details.get('message', a.sku)}" for a in alerts[:6]))
        elif type_ == "sales_drop":
            sections.append("📉 *Sales drops*\n"
                            + "\n".join(f"• {a.details.get('message', a.sku)}" for a in alerts[:6]))
        elif type_ == "price_anomaly":
            sections.append("💲 *Prices changed outside the system*\n"
                            + "\n".join(f"• {a.details.get('message', a.sku)}" for a in alerts[:6]))
    shipments = _shipment_section(session)
    if shipments:
        sections.append(shipments)
    changes = _changes_section(session)
    if changes:
        sections.append(changes)
    return sections


def _shipment_section(session: Session) -> str:
    from app.db.models import FbaInboundRequest

    rows = list(session.execute(select(FbaInboundRequest).where(FbaInboundRequest.status.in_(
        ("options_ready", "pending_approval", "confirmed", "in_transit", "receiving",
         "closed_with_discrepancy")))).scalars())
    if not rows:
        return ""
    words = {"options_ready": "waiting for you to pick a warehouse option",
             "pending_approval": "waiting for your approval", "confirmed": "confirmed, not yet shipped",
             "in_transit": "in transit", "receiving": "being received",
             "closed_with_discrepancy": "closed short — worth a case"}
    return "📦 *FBA shipments*\n" + "\n".join(f"• {r.ref}: {words.get(r.status, r.status)}" for r in rows[:8])


def _changes_section(session: Session) -> str:
    from app.db.models import AmazonChangeRequest

    rows = list(session.execute(select(AmazonChangeRequest).where(AmazonChangeRequest.status.in_(
        ("ready_for_approval", "pending_approval", "submitted", "accepted",
         "completed_with_issues")))).scalars())
    if not rows:
        return ""
    words = {"ready_for_approval": "ready to approve", "pending_approval": "waiting for your approval",
             "submitted": "sent to Amazon", "accepted": "accepted, waiting to go live",
             "completed_with_issues": "done, with issues"}
    return "🛠️ *Listing changes*\n" + "\n".join(
        f"• {r.ref} {(r.listing.sku if r.listing else '')}: {words.get(r.status, r.status)}" for r in rows[:8])


# ---------------- linking Phase 2 emails ----------------
def link_amazon_emails(session: Session) -> int:
    """Genuine Amazon emails that name a SKU, ASIN or FBA shipment id get tied to what they're about."""
    from app.db.models import EmailMessage, FbaShipment

    linked = 0
    threads = list(session.execute(
        select(EmailThread).where(EmailThread.category == "amazon",
                                  EmailThread.last_message_at > utcnow() - timedelta(days=14))).scalars())
    if not threads:
        return 0
    listings = {row.sku.lower(): row for row in session.execute(select(AmazonListing)).scalars()}
    asins = {row.asin.lower(): row for row in session.execute(select(AmazonListing)) .scalars() if row.asin}
    shipments = {(s.shipment_confirmation_id or "").lower(): s
                 for s in session.execute(select(FbaShipment)).scalars() if s.shipment_confirmation_id}
    for thread in threads:
        text = " ".join(filter(None, [thread.subject or "", thread.summary or ""])).lower()
        bodies = session.execute(select(EmailMessage.body_text).where(
            EmailMessage.thread_id == thread.id).limit(3)).scalars().all()
        text += " " + " ".join((b or "")[:2000].lower() for b in bodies)
        target = None
        for shipment_id, shipment in shipments.items():
            if shipment_id and shipment_id in text:
                target = ("shipment", shipment)
                break
        if target is None:
            for sku, listing in listings.items():
                if sku and sku in text:
                    target = ("listing", listing)
                    break
        if target is None:
            for asin, listing in asins.items():
                if asin in text:
                    target = ("listing", listing)
                    break
        if target is None:
            continue
        kind, row = target
        alert = session.execute(select(AmazonAlert).where(
            AmazonAlert.sku == (row.sku if kind == "listing" else None),
            AmazonAlert.status == "open")).scalars().first()
        if alert is not None and alert.linked_email_thread_id is None:
            alert.linked_email_thread_id = thread.id
            linked += 1
    session.flush()
    return linked
