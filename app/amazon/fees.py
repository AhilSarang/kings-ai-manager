"""Amazon's own fee estimates per ASIN — the referral and FBA cut of a sale.

This is what the PPC agent's break-even maths rests on, so the numbers come from Amazon rather than
from a percentage table someone typed in: referral rates vary by category and change, and FBA fees
depend on size and weight tiers that Amazon reprices.

Verified against Amazon's published model (selling-partner-api-models, productFeesV0) on 2026-09-25:

* `POST /products/fees/v0/feesEstimate` takes a **bare JSON array** at the top level, not an object.
* Each entry is `{IdType, IdValue, FeesEstimateRequest{MarketplaceId, IsAmazonFulfilled,
  PriceToEstimateFees{ListingPrice{CurrencyCode, Amount}}, Identifier}}`.
* Fees come back in `FeeDetailList` as `FeeType` plus `FinalFee` — `FinalFee`, not `FeeAmount`, is
  what the seller actually pays once promotions and tax are applied.
* The answer is matched back to the question by **`FeesEstimateIdentifier.SellerInputIdentifier`**,
  which is where Amazon echoes the `Identifier` we sent. There is no `FeesEstimateRequest` inside
  `FeesEstimateIdentifier`, so matching on one silently drops every estimate.
* The rate limit is **0.5 requests per second**, ten times tighter than the rest of SP-API, so these
  calls are spaced separately from everything else.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from app.amazon import api
from app.amazon import marketplaces as mkt
from app.amazon.client import _Throttle
from app.amazon.errors import AmazonError

log = logging.getLogger(__name__)

FEES_PATH = "/products/fees/v0/feesEstimate"
BATCH = 20                  # Amazon documents twenty items per request
FEES_PER_SECOND = 0.5       # the usage plan for this operation, from the model
FEE_THROTTLE = _Throttle(per_second=FEES_PER_SECOND)

# Amazon names the referral fee one way and the FBA fee several; the FBA aggregate carries its own
# breakdown, which is why the parts are listed too — an account can be shown either.
REFERRAL_TYPES = {"referralfee"}
# The aggregate Amazon normally sends, whose own IncludedFeeDetailList holds the parts.
FBA_TOTAL_TYPES = {"fbafees", "fulfillmentfees"}
# The parts, in case an account is shown them instead of the aggregate. Counted only when no
# aggregate is present, or the same money would be added twice.
FBA_PART_TYPES = {"fbaperunitfulfillmentfee", "fbapickandpack", "fbaweighthandling",
                  "fbaorderhandling", "fbadeliveryservicesfee"}
OTHER_TYPES = {"variableclosingfee", "peritemfee", "closingfee", "fixedclosingfee"}


@dataclass
class FeeEstimate:
    """What Amazon says it will take out of one sale at a given price."""

    asin: str
    sku: str | None
    price: float
    referral_fee: float | None = None
    fba_fee: float | None = None
    other_fees: float | None = None
    total: float | None = None
    currency: str = "USD"
    estimated_at: str | None = None
    error: str | None = None

    @property
    def usable(self) -> bool:
        """A usable estimate has a referral fee. Every Amazon sale has one, so its absence is a
        broken answer rather than a free sale, and a missing fee must never read as zero."""
        return self.error is None and self.referral_fee is not None

    @property
    def all_fees(self) -> float:
        """Everything Amazon takes, rounded like money rather than left as a float sum."""
        return round(sum(f for f in (self.referral_fee, self.fba_fee, self.other_fees) if f), 2)


def _money(node: Any) -> float | None:
    if not isinstance(node, dict):
        return None
    amount = node.get("Amount")
    try:
        return round(float(amount), 2) if amount is not None else None
    except (TypeError, ValueError):
        return None


def _request(item: dict[str, Any], marketplace_id: str) -> dict[str, Any]:
    """One entry of the batch. `Identifier` is how the answer is matched back to the question."""
    id_type = "SellerSKU" if item.get("sku") else "ASIN"
    id_value = item.get("sku") or item["asin"]
    return {
        "IdType": id_type, "IdValue": id_value,
        "FeesEstimateRequest": {
            "MarketplaceId": marketplace_id,
            "IsAmazonFulfilled": bool(item.get("fba", True)),
            "Identifier": f"{item['asin']}|{item.get('sku') or ''}",
            "PriceToEstimateFees": {
                "ListingPrice": {"CurrencyCode": item.get("currency") or "USD",
                                 "Amount": float(item["price"])}},
        },
    }


def _read(result: dict[str, Any], asked: dict[str, dict[str, Any]]) -> FeeEstimate | None:
    """Turn one result into an estimate, matched back to the item it was asked about.

    Matching order matters. `SellerInputIdentifier` is where Amazon echoes what we sent, so it is
    tried first; `IdValue` is whatever we asked by (a SKU when we asked by SKU), so it is matched
    against both halves of the key rather than as a prefix — a SKU is never the prefix of "ASIN|SKU".
    """
    identity = result.get("FeesEstimateIdentifier") or {}
    echoed = identity.get("SellerInputIdentifier")
    id_value = str(identity.get("IdValue") or "")
    item = asked.get(echoed) if echoed else None
    if item is None and id_value:
        item = next((v for v in asked.values()
                     if id_value in (v.get("sku") or "", v.get("asin") or "")), None)
    if item is None:
        log.warning("a fee estimate came back that we can't match to a request (%r / %r)",
                    echoed, id_value)
        return None

    estimate = FeeEstimate(asin=item["asin"], sku=item.get("sku"), price=float(item["price"]),
                           currency=item.get("currency") or "USD")
    if (result.get("Status") or "").strip().lower() != "success" or result.get("Error"):
        error = result.get("Error") or {}
        estimate.error = (error.get("Message") or result.get("Status") or "Amazon gave no estimate")[:300]
        return estimate

    fees = result.get("FeesEstimate") or {}
    estimate.estimated_at = fees.get("TimeOfFeesEstimation")
    estimate.total = _money(fees.get("TotalFeesEstimate"))
    details = []
    for detail in fees.get("FeeDetailList") or []:
        kind = str(detail.get("FeeType") or "").strip().lower()
        # FinalFee is what is actually charged: FeeAmount before promotions and tax is not.
        amount = _money(detail.get("FinalFee"))
        if amount is None:
            amount = _money(detail.get("FeeAmount"))
        if amount is not None:
            details.append((kind, amount))
    if not details:
        estimate.error = "Amazon returned an estimate with no fees in it"
        return estimate

    referral = sum(a for k, a in details if k in REFERRAL_TYPES)
    fba_total = sum(a for k, a in details if k in FBA_TOTAL_TYPES)
    fba_parts = sum(a for k, a in details if k in FBA_PART_TYPES)
    # When Amazon sends the aggregate, its parts are inside it; adding both would charge FBA twice.
    fba = fba_total if fba_total else fba_parts
    other = 0.0
    for kind, amount in details:
        if kind in REFERRAL_TYPES or kind in FBA_TOTAL_TYPES or kind in FBA_PART_TYPES:
            continue
        if kind not in OTHER_TYPES:
            log.info("unfamiliar Amazon fee type %r; counted against the margin as other",
                     kind)
        other += amount
    if not any(k in REFERRAL_TYPES for k, _a in details):
        # Every Amazon sale carries a referral fee. Without one, the estimate is wrong, not generous.
        estimate.error = "Amazon's estimate had no referral fee in it"
        return estimate
    estimate.referral_fee = round(referral, 2)
    estimate.fba_fee = round(fba, 2)
    estimate.other_fees = round(other, 2)
    total = _money(fees.get("TotalFeesEstimate"))
    if total is not None and abs(total - estimate.all_fees) > 0.02:
        # Not fatal — Amazon's total occasionally includes something we classified differently — but
        # it is the only cross-check available, so it is said out loud.
        log.warning("fee estimate for %s adds up to %.2f but Amazon's total is %.2f",
                    estimate.asin, estimate.all_fees, total)
    return estimate


def estimate(items: list[dict[str, Any]], marketplace_code: str = "US",
             client=None) -> list[FeeEstimate]:
    """Fee estimates for a list of `{asin, sku, price, currency, fba}` items.

    One request per twenty items, spaced for this operation's own rate limit. A batch Amazon refuses
    outright is reported per item rather than raising, so one bad ASIN doesn't cost the whole run.
    """
    client = client or api.client()
    marketplace_id = mkt.by_code(marketplace_code).marketplace_id
    priced = [i for i in items if i.get("price") is not None and i.get("asin")]
    out: list[FeeEstimate] = []
    for start in range(0, len(priced), BATCH):
        batch = priced[start:start + BATCH]
        asked = {f"{i['asin']}|{i.get('sku') or ''}": i for i in batch}
        FEE_THROTTLE.wait()
        try:
            response = client.post(FEES_PATH, json=[_request(i, marketplace_id) for i in batch],
                                   operation="getMyFeesEstimates")
        except AmazonError as exc:
            log.warning("a fee estimate batch was refused: %s", exc)
            out.extend(FeeEstimate(asin=i["asin"], sku=i.get("sku"), price=float(i["price"]),
                                   error=str(exc)[:300]) for i in batch)
            continue
        results = (response or {}).get("FeesEstimateResultList") or []
        seen: set[tuple[str, str | None]] = set()
        for result in results:
            got = _read(result, asked)
            if got is None:
                continue
            if (got.asin, got.sku) in seen:
                log.warning("Amazon sent two estimates for %s; keeping the first", got.asin)
                continue
            seen.add((got.asin, got.sku))
            out.append(got)
    log.info("fee estimates", extra={"asked": len(priced), "got": sum(1 for e in out if e.usable)})
    return out


def for_listing(asin: str, price: float, *, sku: str | None = None, marketplace_code: str = "US",
                fba: bool = True, currency: str = "USD", client=None) -> FeeEstimate | None:
    """One ASIN, for the times a person asks about a single product."""
    got = estimate([{"asin": asin, "sku": sku, "price": price, "currency": currency, "fba": fba}],
                   marketplace_code, client=client)
    return got[0] if got else None
