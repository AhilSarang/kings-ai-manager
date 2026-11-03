"""Amazon SP-API error types, mapped to plain English for the owner."""

from __future__ import annotations

from typing import Any

from app.services.common import ServiceError


class AmazonError(ServiceError):
    """A call failed and retrying it unchanged won't help."""


class AmazonAuthError(AmazonError):
    """Amazon refused the call. `missing_role` means this one operation isn't permitted — the
    credentials themselves are fine, so nothing else should stop working because of it."""

    def __init__(self, message: str, *, missing_role: bool = False):
        super().__init__(message)
        self.missing_role = missing_role


    """Credentials or roles are wrong: the owner has to re-authorise or add a role."""


class AmazonRetryable(AmazonError):
    """Temporary: rate limit, 5xx or a network problem."""


class AmazonOperationFailed(AmazonError):
    """An async Fulfillment Inbound operation finished as FAILED, or never finished."""

    def __init__(self, message: str, *, status: dict[str, Any] | None = None, timed_out: bool = False):
        super().__init__(message)
        self.status = status or {}
        self.timed_out = timed_out


def problem_text(problems: Any) -> str:
    """Amazon returns errors/operationProblems as a list of dicts; turn them into one readable line."""
    if not problems:
        return ""
    if isinstance(problems, dict):
        problems = [problems]
    parts = []
    for p in problems:
        if not isinstance(p, dict):
            parts.append(str(p))
            continue
        message = p.get("message") or p.get("details") or p.get("code") or ""
        code = p.get("code") or p.get("severity") or ""
        detail = p.get("details") if p.get("details") and p.get("details") != message else ""
        bit = " ".join(x for x in (message, detail) if x).strip()
        parts.append(f"{bit} [{code}]" if code and code not in bit else bit)
    # Amazon mixes warnings in with the refusal; the error is what the owner needs to read first.
    parts.sort(key=lambda text: 0 if text.upper().startswith("ERROR") else 1)
    return "; ".join(x for x in parts if x)[:500]


# What Amazon's inbound errors mean in practice (brief Section 16). Amazon's own words are always
# kept; this only adds the sentence that tells the owner what to do about it from WhatsApp.
INBOUND_GUIDANCE: list[tuple[str, str]] = [
    ("FBA_INB_0132", "That's your FBA storage limit for sortable stock, not a problem with the boxes. Check "
                     "the Capacity Monitor in Seller Central (Inventory → Inventory Planning): if there's room "
                     "left, tell me a smaller number of units and I'll rebuild it; if the limit is used up, "
                     "nothing can be sent in until stock sells through or Amazon raises it."),
    ("exceeds the allowed volume", "That's your FBA storage limit, not a problem with the boxes. Check the "
                                   "Capacity Monitor in Seller Central; if there's room left, tell me a smaller "
                                   "number of units and I'll rebuild it."),
    ("exceeds the allowed quantity", "Amazon caps how much of this product it will take right now. Tell me a "
                                     "smaller number of units and I'll rebuild the shipment."),
    ("expiration date required", "Amazon needs an expiry date for this product before it will take the "
                                 "stock. Send it as \"expiry 2028-03-01\" (a lot code too if you have one) "
                                 "and I'll rebuild the shipment."),
    ("FBA_INB_0180", "Amazon needs an expiry date for this product. Send \"expiry 2028-03-01\" and I'll "
                     "rebuild the shipment."),
    ("not eligible", "Amazon won't accept this SKU into FBA at the moment, usually a listing problem or a "
                     "category restriction. Reply ISSUES and I'll show what it's flagging."),
    ("box", "Check the box sizes and weights — Amazon won't take a box over 25 inches on any side or over "
            "50 lb. Send me the corrected numbers and I'll rebuild it."),
    ("prep", "Amazon wants different prep for this SKU. Tell me what it's asking for and I'll set it."),
]


def inbound_guidance(message: str) -> str:
    """One plain-English line about what to do, or nothing when we don't recognise the error."""
    text = (message or "").lower()
    for marker, advice in INBOUND_GUIDANCE:
        if marker.lower() in text:
            return advice
    return ""
