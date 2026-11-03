"""A proposed change, with the numbers that produced it — before anyone has approved anything.

An analyser produces `Candidate` objects; M6 turns them into `ppc_recommendations` rows, ranks them,
caps them, and asks the owner. Keeping them as plain objects until then means the analysers can be tested
without a database and re-run without leaving half a digest behind.

The rule the brief keeps returning to (7.2) is that a recommendation must carry its own evidence: the
window, the clicks, the orders, the spend, the sales, the CVR, the ACOS, the target it was judged
against, and the arithmetic that turned those into a proposal. If a number cannot be shown, the
recommendation should not be made — so `evidence` is not optional and `reason` is written from it
rather than around it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.db.models import PPC_CHANGE_TYPES, PPC_RISK_LEVELS
from app.services import settings_store

log = logging.getLogger(__name__)

# Amazon's own bid floor for Sponsored Products in the US, and a sane ceiling we never exceed
# without one of the owner's rules saying so.
MIN_BID = 0.02
MAX_BID = 100.00


@dataclass
class Candidate:
    """One proposed change. Everything needed to explain it, and nothing the LLM has to work out."""

    analyser: str
    change_type: str
    entity_type: str
    entity_id: str
    reason: str
    # Which ad product this is about. Left empty here and stamped once in `analysers.run_all` from
    # the scene, so a new analyser cannot forget it and no default can quietly file a Sponsored
    # Display finding as Sponsored Products. `recommendations.store_run` refuses an empty one.
    ad_product: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    current_value: Any = None
    proposed_value: Any = None
    asin: str | None = None
    campaign_id: str | None = None
    ad_group_id: str | None = None
    label: str | None = None                 # what to call the entity in a message
    risk: str = "low"
    confidence: float = 0.6
    spend_at_risk: float = 0.0
    monthly_effect: float = 0.0              # estimated $/month, signed: positive is better
    predicted_effect: dict[str, Any] = field(default_factory=dict)
    holds: list[str] = field(default_factory=list)   # why this is being shown but not proposed
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.change_type not in PPC_CHANGE_TYPES:
            raise ValueError(f"{self.change_type!r} is not a change type the schema allows")
        if self.risk not in PPC_RISK_LEVELS:
            raise ValueError(f"{self.risk!r} is not a risk level the schema allows")

    @property
    def held(self) -> bool:
        """True when the numbers say do it but the context says not yet (brief 7.5)."""
        return bool(self.holds)

    @property
    def why_not(self) -> str | None:
        """The hold, in one sentence, for the digest. A held finding is still worth reporting."""
        return "; ".join(self.holds) if self.holds else None

    @property
    def increases_spend(self) -> bool:
        return self.change_type in ("bid_increase", "budget_increase", "enable_target",
                                    "harvest_search_term")

    def as_dict(self) -> dict[str, Any]:
        return {"analyser": self.analyser, "change_type": self.change_type,
                "entity_type": self.entity_type, "entity_id": self.entity_id,
                "label": self.label, "asin": self.asin, "current_value": self.current_value,
                "proposed_value": self.proposed_value, "reason": self.reason,
                "evidence": self.evidence, "risk": self.risk, "confidence": self.confidence,
                "spend_at_risk": round(self.spend_at_risk, 2),
                "monthly_effect": round(self.monthly_effect, 2),
                "predicted_effect": self.predicted_effect, "holds": list(self.holds),
                "notes": list(self.notes)}


# ---------------- thresholds, all of them settings ----------------
def setting(session, key: str, default: float) -> float:
    try:
        return float(settings_store.get(session, key, default))
    except (TypeError, ValueError):
        log.warning("setting %s isn't a number; using %s", key, default)
        return float(default)


@dataclass
class Thresholds:
    """The owner's dials, read once per run so every analyser judges by the same numbers."""

    min_clicks_for_bid_change: int = 15
    min_clicks_for_pause: int = 30
    harvest_min_orders: int = 2
    max_bid_change_pct: float = 25.0
    cooldown_days: int = 14
    daily_cap: int = 25
    wasted_spend_floor: float = 5.0        # spend below this is not worth a negative keyword
    min_orders_for_acos_change: int = 2
    placement_min_clicks: int = 50
    lookback_days: int = 30

    @classmethod
    def load(cls, session) -> Thresholds:
        return cls(
            min_clicks_for_bid_change=int(setting(session, "ppc_min_clicks_for_bid_change", 15)),
            min_clicks_for_pause=int(setting(session, "ppc_min_clicks_for_pause", 30)),
            harvest_min_orders=int(setting(session, "ppc_harvest_min_orders", 2)),
            max_bid_change_pct=setting(session, "ppc_max_bid_change_pct", 25),
            cooldown_days=int(setting(session, "ppc_cooldown_days_after_change", 14)),
            daily_cap=int(setting(session, "ppc_daily_recommendation_cap", 25)),
            wasted_spend_floor=setting(session, "ppc_wasted_spend_floor_usd", 5),
            min_orders_for_acos_change=int(setting(session, "ppc_min_orders_for_acos_change", 2)),
            placement_min_clicks=int(setting(session, "ppc_placement_min_clicks", 50)),
            lookback_days=int(setting(session, "ppc_lookback_days", 30)))


# ---------------- the bid arithmetic, in one place ----------------
def bid_for_acos(current_bid: float, actual_acos: float, target_acos: float, *,
                 max_change_pct: float, min_bid: float | None = None,
                 max_bid: float | None = None) -> tuple[float, list[str]]:
    """The brief's formula: new bid = current × (target ACOS ÷ actual ACOS), then capped.

    Capping matters more than the formula. A target at four times its ACOS target would otherwise be
    cut by 75% in one move, which changes so much at once that the result teaches you nothing —
    ranking, impressions and conversion rate all move together and none of them can be attributed.
    Several 25% steps get to the same place and stay measurable.
    """
    notes: list[str] = []
    if actual_acos <= 0 or current_bid <= 0:
        return current_bid, ["no bid change: there is nothing to scale from"]
    wanted = current_bid * (target_acos / actual_acos)
    limit = max_change_pct / 100.0
    capped = max(min(wanted, current_bid * (1 + limit)), current_bid * (1 - limit))
    if abs(capped - wanted) > 0.005:
        notes.append(f"the arithmetic wants {wanted:.2f}; capped to {max_change_pct:g}% a step")
    floor = MIN_BID if min_bid is None else max(min_bid, MIN_BID)
    ceiling = MAX_BID if max_bid is None else min(max_bid, MAX_BID)
    bounded = max(min(round(capped, 2), ceiling), floor)
    if bounded != round(capped, 2):
        notes.append(f"held at {bounded:.2f} by the bid limits in force")
    return bounded, notes


def step_down(current_bid: float, *, max_change_pct: float,
              min_bid: float | None = None) -> tuple[float, list[str]]:
    """One allowed step down, for the case where there is nothing to scale against.

    A target with no orders has no ACOS, so there is no ratio to apply: the honest move is the
    largest cut the rules permit, said plainly, rather than feeding invented numbers into the ACOS
    formula to arrive at the same place.
    """
    floor = MIN_BID if min_bid is None else max(min_bid, MIN_BID)
    wanted = round(current_bid * (1 - max_change_pct / 100.0), 2)
    bounded = max(wanted, floor)
    notes = [f"cut by the most a single step allows ({max_change_pct:g}%)"]
    if bounded != wanted:
        notes.append(f"held at the {floor:.2f} bid floor")
    return bounded, notes


def clicks_for_one_order(needed_cvr: float) -> float | None:
    """Expected clicks per order at the conversion rate this product needs (brief 7.1)."""
    return round(1 / needed_cvr, 1) if needed_cvr and needed_cvr > 0 else None


def needed_cvr(cpc: float, price: float, target_acos: float) -> float | None:
    """The conversion rate at which these clicks would hit the target ACOS.

    ACOS = spend/sales = (clicks × CPC) / (orders × price), and orders = clicks × CVR, so
    ACOS = CPC / (CVR × price) and the CVR that reaches the target is CPC / (price × target).
    """
    if not (cpc and price and target_acos):
        return None
    value = cpc / (price * target_acos)
    return round(min(value, 1.0), 4)


def clicks_to_be_sure(needed: float, confidence: float = 0.95) -> int | None:
    """How many click with no orders it takes before "this doesn't convert" is more than a hunch.

    If the true conversion rate were the rate this product needs, the chance of seeing no orders at
    all in n clicks is (1 − needed)^n. Solving for the point where that drops below 5% gives the
    number of clicks at which zero orders stops being bad luck. It is the honest version of a
    fixed "30 clicks" threshold, which is far too strict for a $9 product and far too loose for a $90
    one — and it is why the wasted-spend analyser reports both.
    """
    import math

    if not needed or needed <= 0 or needed >= 1:
        return None
    return int(math.ceil(math.log(1 - confidence) / math.log(1 - needed)))
