"""What an ASIN can afford to spend on advertising, and what it should aim for.

The chain is short and every link comes from a real source (brief 6):

    contribution margin = price − unit cost − referral fee − FBA fee − other costs
    break-even ACOS     = contribution margin ÷ price
    target ACOS         = break-even ACOS × the safety margin, unless the owner set one

Break-even ACOS is the point where advertising a sale earns nothing. Spending *at* it is not a
mistake — it buys rank and reviews — but the agent needs to know where it is before it can decide
that on purpose. Hence the safety margin: 70% of break-even by default, so ordinary bidding leaves
some profit rather than exactly none.

Two rules the brief is firm about and this module enforces:

* **A missing unit cost is never guessed.** No cost means no target, the ASIN is marked unverified,
  and the owner is asked once. Everything that touches it says so.
* **Rows are never updated in place.** A price, cost or fee change closes the current row and opens a
  new one, so a recommendation made six weeks ago can still be explained with the numbers it used.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (AmazonListing, PpcProductEconomics, PpcProfile, PpcRule, Product)
from app.services import settings_store
from app.services.common import utcnow

log = logging.getLogger(__name__)

# A difference smaller than this isn't worth a new row: Amazon's fee estimates wobble by a cent.
MATERIAL = 0.01


@dataclass
class Economics:
    """One ASIN's money, as far as we know it. `missing` says what stopped us."""

    asin: str
    sku: str | None = None
    price: float | None = None
    unit_cost: float | None = None
    referral_fee: float | None = None
    fba_fee: float | None = None
    other_costs: float = 0.0
    contribution_margin: float | None = None
    break_even_acos: float | None = None
    target_acos: float | None = None
    target_source: str = "derived"
    currency: str = "USD"
    missing: list[str] | None = None
    note: str | None = None

    @property
    def verified(self) -> bool:
        return not self.missing and self.target_acos is not None


def extra_costs(session: Session, asin: str) -> float:
    """Anything else the owner says this product costs him — freight in, inserts, returns provision.

    Kept as a setting rather than a column because it is a handful of ASINs, set by hand, and the
    brief calls it "any other cost the owner sets" rather than anything Amazon reports.
    """
    stored = settings_store.get(session, "ppc_other_costs", None) or {}
    if not isinstance(stored, dict):
        return 0.0
    try:
        return round(float(stored.get(asin.strip().upper(), 0) or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def set_extra_cost(session: Session, asin: str, amount: float, actor) -> None:
    stored = settings_store.get(session, "ppc_other_costs", None) or {}
    stored = dict(stored) if isinstance(stored, dict) else {}
    stored[asin.strip().upper()] = round(float(amount), 2)
    settings_store.set_value(session, "ppc_other_costs", stored, actor)


def safety_margin(session: Session) -> float:
    """The fraction of break-even the agent aims at. 70 means "target 70% of break-even"."""
    raw = settings_store.get(session, "ppc_target_acos_safety_margin_pct", 70)
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        pct = 70.0
    return max(min(pct, 100.0), 1.0) / 100.0


def compute(session: Session, *, asin: str, price: float | None, unit_cost: float | None,
            referral_fee: float | None, fba_fee: float | None, other_costs: float | None = 0.0,
            sku: str | None = None, currency: str = "USD",
            brand_id=None) -> Economics:
    """The arithmetic, with every missing input named rather than assumed to be zero."""
    result = Economics(asin=asin, sku=sku, price=price, unit_cost=unit_cost,
                       referral_fee=referral_fee, fba_fee=fba_fee,
                       other_costs=float(other_costs or 0.0), currency=currency)
    missing = []
    if price is None or float(price) <= 0:
        missing.append("price")
    if unit_cost is None:
        missing.append("unit cost")
    if referral_fee is None:
        missing.append("Amazon's fee estimate")
    if missing:
        result.missing = missing
        return result

    price = float(price)
    margin = (price - float(unit_cost) - float(referral_fee) - float(fba_fee or 0.0)
              - float(other_costs or 0.0))
    result.contribution_margin = round(margin, 2)
    # break_even_acos is Numeric(6,4): a mistyped cost ("420" instead of "4.20") gives a ratio of
    # -20 and a big enough mistake overflows the column, which would abort the whole refresh rather
    # than flag one product. Clamped, because past -1 the number means the same thing anyway.
    result.break_even_acos = round(max(min(margin / price, 99.0), -99.0), 4)
    if margin <= 0:
        # Selling at a loss before a penny of advertising. Say so; don't produce a negative target.
        result.missing = None
        result.target_acos = None
        result.note = ("this product loses money on every sale before advertising, so there is no "
                       "ACOS that pays for itself")
        return result

    override = rule_target(session, asin=asin, brand_id=brand_id)
    if override is not None:
        result.target_acos, result.target_source = round(float(override), 4), "owner"
    else:
        result.target_acos = round(result.break_even_acos * safety_margin(session), 4)
        result.target_source = "derived"
    return result


def rule_target(session: Session, *, asin: str, brand_id=None) -> float | None:
    """The owner's own target for this ASIN or its brand. His number always wins (brief 6.4)."""
    rules = session.execute(select(PpcRule).where(
        PpcRule.active.is_(True), PpcRule.target_acos.is_not(None))).scalars().all()
    for scope, ref in (("asin", asin), ("brand", str(brand_id) if brand_id else None),
                       ("account", None)):
        for rule in rules:
            if rule.scope != scope:
                continue
            if scope == "account" or (rule.scope_ref or "").strip().lower() == str(ref or "").lower():
                return float(rule.target_acos)
    return None


# ---------------- storing, with history ----------------
def current(session: Session, asin: str, marketplace_id: str) -> PpcProductEconomics | None:
    return session.execute(select(PpcProductEconomics).where(
        PpcProductEconomics.asin == asin, PpcProductEconomics.marketplace_id == marketplace_id,
        PpcProductEconomics.valid_to.is_(None))).scalars().first()


def at(session: Session, asin: str, marketplace_id: str, when) -> PpcProductEconomics | None:
    """What we believed about this ASIN at a point in time, for reproducing an old analysis."""
    rows = session.execute(select(PpcProductEconomics).where(
        PpcProductEconomics.asin == asin, PpcProductEconomics.marketplace_id == marketplace_id,
        PpcProductEconomics.valid_from <= when).order_by(
        PpcProductEconomics.valid_from.desc())).scalars()
    for row in rows:
        if row.valid_to is None or row.valid_to > when:
            return row
    return None


def _same(row: PpcProductEconomics, got: Economics) -> bool:
    def close(a, b) -> bool:
        if a is None or b is None:
            return a is None and b is None
        return abs(float(a) - float(b)) < MATERIAL

    return (close(row.sale_price, got.price) and close(row.unit_cost, got.unit_cost)
            and close(row.referral_fee, got.referral_fee) and close(row.fba_fee, got.fba_fee)
            and close(row.other_costs, got.other_costs)
            and close(row.target_acos, got.target_acos) and row.target_source == got.target_source)


def store(session: Session, profile: PpcProfile, got: Economics, *, listing_id=None,
          product_id=None) -> PpcProductEconomics:
    """Keep one open row per ASIN. Anything that moved closes the old row and opens a new one."""
    row = current(session, got.asin, profile.marketplace_id)
    if row is not None and _same(row, got):
        return row
    now = utcnow()
    if row is not None:
        row.valid_to = now
    fresh = PpcProductEconomics(
        profile_id=profile.id, marketplace_id=profile.marketplace_id, asin=got.asin, sku=got.sku,
        listing_id=listing_id, product_id=product_id, sale_price=got.price, unit_cost=got.unit_cost,
        referral_fee=got.referral_fee, fba_fee=got.fba_fee, other_costs=got.other_costs,
        contribution_margin=got.contribution_margin, break_even_acos=got.break_even_acos,
        target_acos=got.target_acos, target_source=got.target_source,
        cost_verified=got.verified, currency=got.currency, valid_from=now,
        source_note=got.note or (f"waiting on: {', '.join(got.missing)}" if got.missing else None),
        cost_asked_at=row.cost_asked_at if row is not None else None)
    session.add(fresh)
    session.flush()
    return fresh


# ---------------- putting it together for the whole catalogue ----------------
def _listing_price(listing: AmazonListing) -> float | None:
    """Phase 3 keeps Amazon's current price on the listing as `price_cached`."""
    return float(listing.price_cached) if listing.price_cached is not None else None


def inputs_for(session: Session, profile: PpcProfile,
               listings: list[AmazonListing] | None = None) -> list[dict[str, Any]]:
    """Everything we know before asking Amazon for fees: price, cost, brand, and the links."""
    if listings is None:
        listings = list(session.execute(select(AmazonListing).where(
            AmazonListing.marketplace_id == profile.marketplace_id)).scalars())
    out = []
    for listing in listings:
        if not listing.asin:
            continue
        product = session.get(Product, listing.product_id) if listing.product_id else None
        out.append({"asin": listing.asin, "sku": listing.sku, "price": _listing_price(listing),
                    "currency": listing.currency or profile.currency,
                    "fba": (listing.fulfillment or "FBA").upper() == "FBA",
                    "listing": listing, "product": product,
                    "unit_cost": float(product.unit_cost) if product and product.unit_cost is not None
                    else None,
                    "brand_id": listing.brand_id})
    return out


def one_per_asin(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per ASIN, because the economics table holds one open row per ASIN.

    Two SKUs on the same ASIN — a relisted product, a second condition — made every refresh close
    and reopen the row twice, so the history filled with rows that recorded nothing but the refresh
    itself. The SKU kept is the one that can actually be priced: a price, then a cost, then FBA.
    """
    best: dict[str, dict[str, Any]] = {}
    for item in items:
        key = item["asin"].upper()
        rival = best.get(key)
        if rival is None or _better(item, rival):
            best[key] = item
    return list(best.values())


def _better(item: dict[str, Any], rival: dict[str, Any]) -> bool:
    def score(i: dict[str, Any]) -> tuple:
        return (i["price"] is not None, i["unit_cost"] is not None, bool(i["fba"]),
                str(i.get("sku") or ""))

    return score(item) > score(rival)


def refresh(session: Session, profile: PpcProfile, *, fee_client=None,
            listings: list[AmazonListing] | None = None,
            asins: list[str] | None = None) -> dict[str, Any]:
    """Recompute every ASIN's economics: price and cost from us, fees from Amazon.

    Only ASINs whose price we hold are sent for a fee estimate — Amazon's estimate is per price, so
    asking without one would be meaningless.
    """
    from app.amazon import fees as fees_api

    items = one_per_asin(inputs_for(session, profile, listings))
    if asins:
        wanted = {a.strip().upper() for a in asins}
        items = [i for i in items if i["asin"].upper() in wanted]
    priced = [i for i in items if i["price"] is not None]
    estimates = {}
    if priced:
        # The profile knows its own marketplace; a reverse lookup that quietly falls back to "US"
        # would price Canadian products with American fees.
        for got in fees_api.estimate([{k: i[k] for k in ("asin", "sku", "price", "currency", "fba")}
                                      for i in priced], profile.country_code, client=fee_client):
            estimates[(got.asin, got.sku)] = got

    stats = {"asins": 0, "priced": len(priced), "with_fees": 0, "missing_cost": [], "loss_making": []}
    for item in items:
        got_fees = estimates.get((item["asin"], item["sku"])) or estimates.get((item["asin"], None))
        usable = bool(got_fees and got_fees.usable)
        result = compute(
            session, asin=item["asin"], sku=item["sku"], price=item["price"],
            unit_cost=item["unit_cost"], currency=item["currency"], brand_id=item["brand_id"],
            referral_fee=got_fees.referral_fee if usable else None,
            fba_fee=got_fees.fba_fee if usable else None,
            # Amazon's other fees (variable closing and anything new) plus whatever the owner adds.
            other_costs=(got_fees.other_fees or 0.0 if usable else 0.0)
            + extra_costs(session, item["asin"]))
        store(session, profile, result, listing_id=item["listing"].id,
              product_id=item["product"].id if item["product"] else None)
        stats["asins"] += 1
        stats["with_fees"] += 1 if got_fees and got_fees.usable else 0
        if result.missing and "unit cost" in result.missing:
            stats["missing_cost"].append(item["asin"])
        if result.note:
            stats["loss_making"].append(item["asin"])
    session.flush()
    log.info("ppc economics refreshed", extra={k: (len(v) if isinstance(v, list) else v)
                                               for k, v in stats.items()})
    return stats


def unverified(session: Session, profile: PpcProfile) -> list[PpcProductEconomics]:
    """ASINs the agent cannot price properly. Every recommendation touching one must say so."""
    return list(session.execute(select(PpcProductEconomics).where(
        PpcProductEconomics.marketplace_id == profile.marketplace_id,
        PpcProductEconomics.valid_to.is_(None),
        PpcProductEconomics.cost_verified.is_(False)).order_by(PpcProductEconomics.asin)).scalars())


def missing_cost(session: Session, profile: PpcProfile) -> list[PpcProductEconomics]:
    """The ones a number from the owner would fix — as opposed to the ones that lose money anyway."""
    return [row for row in unverified(session, profile) if row.unit_cost is None]


def loss_making(session: Session, profile: PpcProfile) -> list[PpcProductEconomics]:
    """Products that lose money before a penny of advertising. A pricing problem, not a bidding one."""
    return [row for row in unverified(session, profile)
            if row.contribution_margin is not None and float(row.contribution_margin) <= 0]


def target_for(session: Session, asin: str, marketplace_id: str) -> tuple[float | None, str]:
    """The target ACOS to judge this ASIN by, and what it rests on. Never invented."""
    row = current(session, asin, marketplace_id)
    if row is None:
        return None, "no economics stored for this ASIN yet"
    if row.target_acos is None:
        return None, (row.source_note or "target ACOS unverified")
    return float(row.target_acos), row.target_source


# ---------------- the owner filling in what only he knows ----------------
def set_unit_cost(session: Session, asin: str, amount: float, actor) -> str:
    """`COST B0… 4.20` — the one number the agent can never work out for itself.

    It goes on the product record, not on the economics row: the cost is a fact about the product,
    and the economics row is a dated conclusion drawn from it. Then the ASIN is recomputed at once,
    so the answer names the target ACOS it just unlocked rather than promising one later.
    """
    listing = session.execute(select(AmazonListing).where(
        AmazonListing.asin == asin.strip().upper()).order_by(AmazonListing.created_at)).scalars().first()
    if listing is None:
        return f"I can't find a listing for {asin}. Send LISTINGS to see what I have."

    product = session.get(Product, listing.product_id) if listing.product_id else None
    was = float(product.unit_cost) if product is not None and product.unit_cost is not None else None
    if product is None:
        # Every listing should have a product behind it; make one rather than lose the number.
        product = Product(internal_name=listing.title_cached or listing.sku or asin,
                          brand_id=listing.brand_id, unit_cost=amount)
        session.add(product)
        session.flush()
        listing.product_id = product.id
    product.unit_cost = round(float(amount), 2)
    session.flush()
    # A unit cost rewrites every target ACOS derived from it, so who set it and when is auditable.
    from app.services import audit

    audit.record(session, actor, "ppc_unit_cost_set", target_type="product",
                 target_id=str(product.id),
                 details={"asin": asin.strip().upper(), "old": was, "new": product.unit_cost})

    # The cost is the same wherever it sells, so every marketplace's economics are redone, not just
    # the one whose listing happened to be found first.
    from app.ppc import profiles as profiles_svc

    results = []
    for profile in profiles_svc.active(session):
        rows = list(session.execute(select(AmazonListing).where(
            AmazonListing.asin == asin.strip().upper(),
            AmazonListing.marketplace_id == profile.marketplace_id)).scalars())
        for row in rows:
            if row.product_id is None:
                row.product_id = product.id
        # One row per ASIN per marketplace, as the table holds: recomputing each SKU in turn closed
        # and reopened the row once per SKU and answered with whichever one came last.
        chosen = one_per_asin([{"asin": r.asin, "sku": r.sku, "price": _listing_price(r),
                                "unit_cost": amount, "fba": (r.fulfillment or "FBA").upper() == "FBA",
                                "listing": r} for r in rows])
        for item in chosen:
            results.append((profile, recompute_asin(session, profile, item["listing"])))
    if not results:
        return (f"Noted: {asin} costs {amount:.2f} a unit. I'll use it as soon as Amazon Ads is "
                "connected.")
    profile, result = results[0]
    changed = f" (was {was:.2f})" if was is not None and abs(was - amount) >= MATERIAL else ""
    where = f" in {profile.country_code}" if len(profiles_svc.active(session)) > 1 else ""
    if result.target_acos is not None:
        return (f"✅ {asin} costs {amount:.2f} a unit{changed}. Margin "
                f"{result.contribution_margin:.2f}{where}, break-even ACOS "
                f"{result.break_even_acos * 100:.1f}%, so I'll aim at "
                f"{result.target_acos * 100:.1f}%.")
    if result.note:
        return f"✅ Noted: {asin} costs {amount:.2f} a unit{changed}. But {result.note}."
    return (f"✅ Noted: {asin} costs {amount:.2f} a unit{changed}. I still need "
            f"{' and '.join(result.missing or [])} before I can set a target.")


def _profile_for(session: Session, marketplace_id: str) -> PpcProfile | None:
    return session.execute(select(PpcProfile).where(
        PpcProfile.marketplace_id == marketplace_id,
        PpcProfile.status == "active")).scalars().first()


def recompute_asin(session: Session, profile: PpcProfile, listing: AmazonListing,
                   fee_client=None) -> Economics:
    """Redo one ASIN, reusing Amazon's last fee estimate rather than asking again for one price."""
    stats_row = current(session, listing.asin, profile.marketplace_id)
    product = session.get(Product, listing.product_id) if listing.product_id else None
    price = _listing_price(listing)
    referral = float(stats_row.referral_fee) if stats_row and stats_row.referral_fee is not None else None
    fba = float(stats_row.fba_fee) if stats_row and stats_row.fba_fee is not None else None
    # The stored other_costs is Amazon's other fees plus whatever the owner added, so the owner's part is
    # subtracted out before it is added back below — otherwise re-running a COST command quietly
    # drops Amazon's closing fees from the margin, or counts the owner's twice.
    mine = extra_costs(session, listing.asin)
    other = max(float(stats_row.other_costs or 0.0) - mine, 0.0) if stats_row is not None else 0.0
    # The stored estimate was made at a particular price; if the price has moved, it is stale.
    if stats_row is not None and price is not None and stats_row.sale_price is not None:
        if abs(float(stats_row.sale_price) - price) >= MATERIAL:
            referral = fba = None
    if referral is None and price is not None:
        other = 0.0
        from app.amazon import fees as fees_api

        # The profile's own marketplace: asking Amazon for US fees on a Canadian listing would
        # produce a plausible, wrong margin.
        got = fees_api.for_listing(listing.asin, price, sku=listing.sku, client=fee_client,
                                   marketplace_code=profile.country_code,
                                   currency=listing.currency or profile.currency,
                                   fba=(listing.fulfillment or "FBA").upper() == "FBA")
        if got is not None and got.usable:
            referral, fba, other = got.referral_fee, got.fba_fee, (got.other_fees or 0.0)
    result = compute(session, asin=listing.asin, sku=listing.sku, price=price,
                     unit_cost=float(product.unit_cost) if product and product.unit_cost is not None
                     else None, referral_fee=referral, fba_fee=fba,
                     other_costs=other + mine,
                     currency=listing.currency or profile.currency, brand_id=listing.brand_id)
    store(session, profile, result, listing_id=listing.id,
          product_id=product.id if product else None)
    return result


def ask_for_missing_costs(session: Session, profile: PpcProfile) -> int:
    """Ask the owner once per ASIN for a cost we don't have. Once, not every morning (brief 6.6)."""
    from app.services.notify import notify_owner

    waiting = [row for row in unverified(session, profile)
               if row.cost_asked_at is None and row.unit_cost is None]
    if not waiting:
        return 0
    names = ", ".join(row.asin for row in waiting[:8])
    more = f" and {len(waiting) - 8} more" if len(waiting) > 8 else ""
    # The key names the products, not the profile: with a fixed key, a product added next month was
    # marked as asked about while the message was silently suppressed as a duplicate, so the owner would
    # never hear about it and its target would stay unverified for ever.
    digest = hashlib.sha256(",".join(sorted(r.asin for r in waiting)).encode()).hexdigest()[:12]
    notify_owner(session,
                 f"📊 I can't work out what these products can afford to spend on ads because I "
                 f"don't have your unit cost: {names}{more}. Send `COST <ASIN> <amount>` for each — "
                 f"until then I'll advertise them but every recommendation will say the target is "
                 f"unverified.", key=f"ppc_missing_costs:{profile.amazon_profile_id}:{digest}")
    asked = utcnow()
    for row in waiting:
        row.cost_asked_at = asked
    session.flush()
    return len(waiting)
