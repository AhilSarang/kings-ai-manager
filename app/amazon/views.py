"""WhatsApp views for Amazon: plain code, no LLM, so the numbers are always the stored ones."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon import inventory as inv
from app.amazon import listings as listings_svc
from app.amazon import marketplaces as mkt
from app.db.models import AmazonListing, AmazonListingVersion
from app.services import settings_store
from app.services.formatting import fmt_dt, truncate

SEVERITY_MARK = {"ERROR": "❗", "WARNING": "⚠️"}


def _money(listing: AmazonListing) -> str:
    if listing.price_cached is None:
        return "no price"
    return f"{listing.currency or ''} {float(listing.price_cached):,.2f}".strip()


def _errors(listing: AmazonListing) -> list[dict]:
    return [i for i in (listing.issues_cached or []) if (i.get("severity") or "").upper() == "ERROR"]


def _line(session: Session, listing: AmazonListing) -> str:
    stock = inv.for_sku(session, listing.sku)
    bits = [f"{listing.ref} {truncate(listing.title_cached or listing.sku, 48)}"]
    bits.append(f"  {listing.sku}" + (f" · {listing.asin}" if listing.asin else ""))
    state = "✅ buyable" if listing.buyable else "⛔ not buyable"
    if _errors(listing):
        state = f"❗ {len(_errors(listing))} error(s)"
    stock_text = f"{stock.fulfillable_qty} in stock" if stock else "stock unknown"
    if stock and stock.inbound_qty:
        stock_text += f" (+{stock.inbound_qty} inbound)"
    bits.append(f"  {_money(listing)} · {state} · {stock_text}")
    return "\n".join(bits)


def listings_command(session: Session, limit: int = 12) -> str:
    rows = list(session.execute(select(AmazonListing).order_by(AmazonListing.sku)).scalars())
    if not rows:
        return ("No Amazon listings stored yet. They arrive with the next sync "
                "(hourly), or reply SYNC AMAZON to fetch them now.")
    with_errors = [r for r in rows if _errors(r)]
    not_buyable = [r for r in rows if not r.buyable and r not in with_errors]
    healthy = [r for r in rows if r not in with_errors and r not in not_buyable]
    out = [f"🛒 *Amazon listings* ({len(rows)})",
           f"{len(healthy)} fine · {len(with_errors)} with errors · {len(not_buyable)} not buyable"]
    for title, group in (("*Needs attention*", with_errors + not_buyable), ("*Everything else*", healthy)):
        if group:
            out.append("")
            out.append(title)
            out += [_line(session, r) for r in group[:limit]]
            if len(group) > limit:
                out.append(f"…and {len(group) - limit} more")
    out.append('\nReply with a ref (L-4), "SKU <sku>", "ASIN <asin>" or STOCK.')
    return "\n".join(out)


def listing_detail(session: Session, listing: AmazonListing) -> str:
    stock = inv.for_sku(session, listing.sku)
    cover = inv.days_of_cover(session, listing.sku)
    mp = mkt.by_id(listing.marketplace_id)
    lines = [f"🛒 *{listing.ref}* {truncate(listing.title_cached or '(no title)', 90)}",
             f"SKU: {listing.sku}" + (f" · ASIN: {listing.asin}" if listing.asin else ""),
             f"{mp.code} · {_money(listing)} · {listing.fulfillment} · "
             f"{'buyable' if listing.buyable else 'not buyable'}"]
    if listing.product_type:
        lines.append(f"Product type: {listing.product_type}"
                     + (f" · brand: {listing.brand.name}" if listing.brand else ""))
    if stock:
        lines.append(f"Stock: {stock.fulfillable_qty} sellable · {stock.inbound_qty} inbound · "
                     f"{stock.reserved_qty} reserved · {stock.unfulfillable_qty} unsellable")
    if cover is not None:
        lines.append(f"Cover: about {cover} days at the last 30 days' rate")
    units7 = _units(session, listing, 7)
    if units7 is not None:
        lines.append(f"Sold in the last 7 days: {units7}")
    for issue in (listing.issues_cached or [])[:6]:
        mark = SEVERITY_MARK.get((issue.get("severity") or "").upper(), "•")
        lines.append(f"{mark} {truncate(issue.get('message') or issue.get('code') or '', 160)}")
    if listing.last_synced_at:
        lines.append(f"Last checked: {fmt_dt(listing.last_synced_at)}")
    lines.append(f'\nReply "VERSIONS {listing.ref}" for history, or tell me the change you want.')
    return "\n".join(lines)


def _units(session: Session, listing: AmazonListing, days: int) -> int | None:
    from datetime import timedelta

    from sqlalchemy import func

    from app.db.models import AmazonSalesDaily
    from app.services.common import utcnow

    since = (utcnow() - timedelta(days=days)).date()
    total = session.execute(select(func.sum(AmazonSalesDaily.units)).where(
        AmazonSalesDaily.sku == listing.sku, AmazonSalesDaily.date >= since)).scalar()
    return int(total) if total is not None else None


def stock_command(session: Session) -> str:
    rows = inv.latest(session)
    if not rows:
        return "No FBA inventory stored yet. The next inventory check is within 2 hours."
    threshold = int(settings_store.get(session, "amazon_low_stock_days_cover", 21) or 21)
    out_of_stock, low, fine = [], [], 0
    for snap in rows:
        cover = inv.days_of_cover(session, snap.sku)
        if snap.fulfillable_qty == 0:
            out_of_stock.append((snap, cover))
        elif cover is not None and cover < threshold:
            low.append((snap, cover))
        else:
            fine += 1
    lines = ["📦 *FBA stock*"]
    if out_of_stock:
        lines.append("\n*Out of stock*")
        lines += [f"• {s.sku} — 0 sellable" + (f", {s.inbound_qty} inbound" if s.inbound_qty else ", nothing inbound")
                  for s, _ in out_of_stock]
    if low:
        lines.append(f"\n*Low (under {threshold} days)*")
        lines += [f"• {s.sku} — {s.fulfillable_qty} left, about {c} days"
                  + (f", {s.inbound_qty} inbound" if s.inbound_qty else "") for s, c in sorted(low, key=lambda x: x[1])]
    lines.append(f"\n{fine} other SKU(s) have enough stock.")
    return "\n".join(lines)


def versions_command(session: Session, listing: AmazonListing) -> str:
    rows = listings_svc.versions(session, listing)
    if not rows:
        return f"{listing.ref} has no stored versions yet."
    source = {"amazon_sync": "read from Amazon", "submitted_change": "our change", "revert": "revert"}
    lines = [f"🕘 *Versions of {listing.ref}* ({truncate(listing.title_cached or listing.sku, 50)})"]
    for v in rows:
        current = " ← current" if v.id == listing.current_version_id else ""
        lines.append(f"V{v.version_number} · {fmt_dt(v.retrieved_at or v.created_at)} · "
                     f"{source.get(v.source, v.source)}{current}")
    lines.append(f'\nReply "REVERT {listing.ref} V<n>" to go back to one (approval required).')
    return "\n".join(lines)


def version_summary(version: AmazonListingVersion) -> dict:
    """Attributes flattened to simple values, for diffs and approval messages."""
    flat = {}
    for key, value in (version.attributes or {}).items():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "value" in value[0]:
            flat[key] = value[0]["value"]
        else:
            flat[key] = value
    return flat


# ---------------- change requests ----------------
CHANGE_STATUS_WORDS = {
    "drafting": "drafting", "needs_info": "needs your input", "validating": "checking",
    "ready_for_approval": "ready to approve", "pending_approval": "waiting for your approval",
    "approved": "approved", "submitted": "sent to Amazon", "accepted": "accepted by Amazon",
    "live_verified": "live and verified", "completed_with_issues": "done, with issues",
    "rejected_by_amazon": "rejected by Amazon", "cancelled": "cancelled", "failed": "failed",
}


def changes_command(session: Session) -> str:
    from app.amazon import changes as changes_svc

    rows = changes_svc.open_changes(session)
    if not rows:
        return "No open listing changes. Tell me what to change and I'll draft it."
    lines = ["🛠️ *Open listing changes*"]
    for r in rows:
        listing = r.listing
        fields = ", ".join(d["field"].replace("_", " ") for d in (r.diff or [])[:3]) or r.operation
        lines.append(f"• {r.ref} — {listing.ref if listing else '?'} {truncate(listing.sku if listing else '', 20)} — "
                     f"{truncate(fields, 50)} ({CHANGE_STATUS_WORDS.get(r.status, r.status)})")
    lines.append('\nReply with a ref (LC-3) for details, or "CANCEL LC-3".')
    return "\n".join(lines)


def change_detail(session: Session, request, *, full: bool = False) -> str:
    from app.amazon import changes as changes_svc

    listing = request.listing
    lines = [f"🛠️ *{request.ref}* — {request.operation.replace('_', ' ')} on "
             f"{listing.ref if listing else '?'} ({listing.sku if listing else '?'})",
             f"Status: {CHANGE_STATUS_WORDS.get(request.status, request.status)}"]
    if request.owner_instructions:
        lines.append(f"You asked: {truncate(request.owner_instructions, 200)}")
    for change in (request.diff or []):
        limit = 1000 if full else 160
        lines.append(f"• {change['field'].replace('_', ' ')}")
        lines.append(f"  Before: {truncate(change['before'], limit)}")
        lines.append(f"  After:  {truncate(change['after'], limit)}")
    for problem in (request.local_validation or {}).get("errors", []):
        lines.append(f"⛔ {problem}")
    for warning in (request.warnings or []):
        lines.append(f"⚠️ {warning}")
    for issue in (request.final_issues or []):
        lines.append(f"❗ {issue}")
    if request.submission_id:
        lines.append(f"Amazon submission: {request.submission_id}")
    if request.status == "ready_for_approval":
        lines.append("\nReply \"SUBMIT " + request.ref + "\" and I'll ask for your approval code.")
    elif request.status == "pending_approval":
        lines.append("\nWaiting for your APPROVE reply.")
    return "\n".join(lines)
