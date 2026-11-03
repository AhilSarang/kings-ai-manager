"""The deterministic analysers (brief 7.1): plain code that proposes changes, with its reasons.

Nothing here calls an LLM. Every threshold check, every aggregation and every bit of arithmetic is
Python and SQL, and the LLM's job later is to write the sentence around numbers it did not compute.
That split is the house rule, and it is also what makes these testable: each analyser is a function
of one `Scene` and produces `Candidate` objects, so a fixture of metrics rows can prove the whole
decision without a network or a model.

Each analyser judges a target against **what its product can afford** — the target ACOS from M4 —
rather than against one number for the account. A 60% ACOS is a disaster on a product with a 30%
break-even and perfectly good on one with 75%.

Two things are applied to everything at the end, in `run_all`:

* **Context (7.5).** A proposal that would spend *more* on a product that is out of stock, low, not
  buyable, or losing the Buy Box is held rather than made, and says why. Cuts are never held.
* **Cooldown (7.5).** An entity changed in the last `ppc_cooldown_days_after_change` days is left
  alone, so a change gets long enough to be measured before the next one lands on top of it.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PpcAction, PpcCampaign, PpcNegative, PpcProfile, PpcTarget
from app.ppc import aggregates, candidates, context, economics
# Imported as a bare name: `structure` is also the name of one of the analysers below.
from app.ppc.structure import AD_PRODUCT
from app.ppc.aggregates import PlacementRollup, Rollup, TermRollup
from app.ppc.candidates import Candidate, Thresholds
from app.ppc.context import ProductContext

log = logging.getLogger(__name__)

ANALYSERS: dict[str, Callable[["Scene"], list[Candidate]]] = {}

# How far below target counts as "leaving money on the table" for an increase.
UNDER_TARGET_RATIO = 0.7
# Top-of-search impression share below this means there is room to buy more of it.
HEADROOM_SHARE = 60.0
# A placement has to differ from its campaign's average by this much before it is worth adjusting.
PLACEMENT_GAP = 0.25
# Amazon's own minimum daily campaign budget. The per-click bid floor is not a budget floor, and
# proposing 2 cents a day would simply be refused.
MIN_DAILY_BUDGET = 1.00
# Words too common to be a useful negative phrase.
STOPWORDS = {"the", "a", "an", "for", "with", "and", "of", "in", "to", "on", "best", "top", "new",
             "pack", "size", "free", "buy", "cheap", "online", "near", "me"}


def analyser(name: str):
    def register(fn):
        ANALYSERS[name] = fn
        fn.analyser_name = name
        return fn

    return register


# ---------------- everything an analyser needs, loaded once ----------------
@dataclass
class Scene:
    session: Session
    profile: PpcProfile
    today: date
    thresholds: Thresholds
    # Everything loaded into a Scene comes from one ad product's tables. Stated here so `run_all`
    # can stamp it onto every candidate rather than each analyser remembering to.
    ad_product: str = AD_PRODUCT
    targets: dict[str, PpcTarget] = field(default_factory=dict)
    campaigns: dict[str, PpcCampaign] = field(default_factory=dict)
    target_totals: dict[str, Rollup] = field(default_factory=dict)
    campaign_totals: dict[str, Rollup] = field(default_factory=dict)
    asin_totals: dict[str, Rollup] = field(default_factory=dict)
    terms: list[TermRollup] = field(default_factory=list)
    placements: dict[str, list[PlacementRollup]] = field(default_factory=dict)
    ad_group_asins: dict[str, list[str]] = field(default_factory=dict)
    contexts: dict[str, ProductContext] = field(default_factory=dict)
    targets_by_ad_group: dict[str, list[PpcTarget]] = field(default_factory=dict)
    ad_groups_by_campaign: dict[str, list[str]] = field(default_factory=dict)
    ad_group_campaign: dict[str, str] = field(default_factory=dict)
    ad_group_defaults: dict[str, float] = field(default_factory=dict)
    share_cache: dict[str, float | None] = field(default_factory=dict)
    campaign_days: dict[str, list] = field(default_factory=dict)
    acos_cache: dict[str, tuple[float | None, str]] = field(default_factory=dict)
    negatives: set[tuple[str, str]] = field(default_factory=set)   # (ad group or campaign, text)
    recent_changes: dict[tuple[str, str], date] = field(default_factory=dict)

    def strictest(self, ad_group_id: str | None) -> tuple[float | None, str, str | None]:
        """The target ACOS an ad group's products can afford, and which product set it.

        Where an ad group advertises several ASINs the strictest target wins: a bid that is fine for
        the roomiest product would quietly lose money on the tightest one. The ASIN is returned too,
        because the price has to come from the *same* product — pairing one ASIN's price with
        another's target produces a needed-CVR figure describing no real product at all.
        """
        asins = self.ad_group_asins.get(ad_group_id or "", [])
        found = []
        for asin in asins:
            # Read once per ASIN per run. `target_for` is a SELECT, and this is called once per
            # target in two analysers and once per harvestable search term in a third — thousands of
            # identical queries for a figure that cannot change mid-run.
            if asin not in self.acos_cache:
                self.acos_cache[asin] = economics.target_for(
                    self.session, asin, self.profile.marketplace_id)
            value, source = self.acos_cache[asin]
            if value is not None:
                found.append((value, source, asin))
        if not found:
            return None, ("no target ACOS for this ad group's products yet"
                          if asins else "this ad group advertises nothing we can price"), None
        value, source, asin = min(found)
        note = f"from {asin}" + (" (the strictest of several)" if len(found) > 1 else "")
        return value, f"{source}, {note}", asin

    def target_acos(self, ad_group_id: str | None) -> tuple[float | None, str]:
        value, source, _asin = self.strictest(ad_group_id)
        return value, source

    def price_for(self, ad_group_id: str | None) -> float | None:
        """The price of the product whose target ACOS is in force, not whichever has one first."""
        _value, _source, asin = self.strictest(ad_group_id)
        if asin:
            got = self.contexts.get(asin)
            if got is not None and got.price:
                return got.price
        for asin in self.ad_group_asins.get(ad_group_id or "", []):
            got = self.contexts.get(asin)
            if got is not None and got.price:
                return got.price
        return None

    def asins_for(self, ad_group_id: str | None) -> list[str]:
        return self.ad_group_asins.get(ad_group_id or "", [])

    def asins_for_campaign(self, campaign_id: str | None) -> list[str]:
        """Every ASIN a campaign advertises. A budget change is a spend change for all of them."""
        out: list[str] = []
        for ad_group_id in self.ad_groups_by_campaign.get(campaign_id or "", []):
            for asin in self.ad_group_asins.get(ad_group_id, []):
                if asin not in out:
                    out.append(asin)
        return out

    def changed_recently(self, entity_type: str, entity_id: str) -> date | None:
        return self.recent_changes.get((entity_type, entity_id))


def load(session: Session, profile: PpcProfile, *, today: date | None = None,
         days: int | None = None, ad_product: str = AD_PRODUCT) -> Scene:
    """Read everything the analysers need, in a fixed number of queries.

    Every read below is scoped to one ad product. Sponsored Products is the default because it is
    the only one built so far, but it is a parameter rather than an assumption: Display's sales
    include view-through attribution, so pooling it with Sponsored Products would produce an ACOS
    that describes neither.
    """
    from app.ppc import metrics

    today = today or metrics.today_for(profile)
    thresholds = Thresholds.load(session)
    days = days or thresholds.lookback_days
    scene = Scene(session=session, profile=profile, today=today, thresholds=thresholds,
                  ad_product=ad_product)

    # Paused targets too: without them `enable_target` — a change type the schema and the brief both
    # name — could never be produced, and a target paused in error would stay paused for ever.
    scene.targets = aggregates.targets(session, profile, ad_product=ad_product,
                                 states=("enabled", "paused"))
    scene.campaigns = {c.amazon_campaign_id: c for c in session.execute(select(PpcCampaign).where(
        PpcCampaign.profile_id == profile.id)).scalars()}
    scene.target_totals = aggregates.by_entity(session, profile, "target", ad_product=ad_product, days=days,
                                   today=today)
    scene.campaign_totals = aggregates.by_entity(session, profile, "campaign", ad_product=ad_product, days=days,
                                     today=today)
    # Every campaign's individual days in one query rather than one query per campaign: the budget
    # analyser needs the shape, not just the total, and it needs it for all of them.
    scene.campaign_days = aggregates.days_by_entity(session, profile, "campaign", ad_product=ad_product, days=days,
                                                    today=today)
    scene.asin_totals = aggregates.by_entity(session, profile, "asin", ad_product=ad_product, days=days,
                                 today=today)
    scene.terms = aggregates.search_terms(session, profile, ad_product=ad_product, days=days, today=today)
    scene.placements = aggregates.placements(session, profile, ad_product=ad_product, days=days, today=today)
    scene.ad_group_asins = aggregates.asin_for_ad_group(session, profile, ad_product=ad_product)
    # Every advertised ASIN, named explicitly: without the list, an ASIN that is advertised but has no
    # catalogue row silently got no context at all, so nothing checked its stock and nothing said so.
    advertised = sorted({asin for asins in scene.ad_group_asins.values() for asin in asins})
    scene.contexts = context.build(session, profile, today=today, asins=advertised or None)

    for target in scene.targets.values():
        scene.targets_by_ad_group.setdefault(target.amazon_ad_group_id, []).append(target)
    from app.db.models import PpcAdGroup

    for group in session.execute(select(PpcAdGroup).where(
            PpcAdGroup.profile_id == profile.id)).scalars():
        scene.ad_groups_by_campaign.setdefault(group.amazon_campaign_id, []).append(
            group.amazon_ad_group_id)
        scene.ad_group_campaign[group.amazon_ad_group_id] = group.amazon_campaign_id
        if group.default_bid is not None:
            scene.ad_group_defaults[group.amazon_ad_group_id] = float(group.default_bid)

    # Keyed by Amazon's ids and by scope. Keyed by our row uuids, as it was, nothing ever matched,
    # so every existing negative was proposed again every morning.
    groups_by_row = {g.id: g.amazon_ad_group_id for g in session.execute(select(PpcAdGroup).where(
        PpcAdGroup.profile_id == profile.id)).scalars()}
    campaigns_by_row = {c.id: c.amazon_campaign_id for c in scene.campaigns.values()
                        } | {c.id: c.amazon_campaign_id for c in session.execute(
                            select(PpcCampaign).where(PpcCampaign.profile_id == profile.id)).scalars()}
    for negative in session.execute(select(PpcNegative).where(
            PpcNegative.profile_id == profile.id, PpcNegative.state != "archived")).scalars():
        expression = (negative.expression or "").strip().lower()
        if not expression:
            continue
        if negative.ad_group_id is not None:
            scene.negatives.add((groups_by_row.get(negative.ad_group_id, ""), expression))
        if negative.campaign_id is not None:
            # A campaign negative covers every ad group in it, so it is recorded against each.
            amazon_campaign = campaigns_by_row.get(negative.campaign_id, "")
            scene.negatives.add((amazon_campaign, expression))
            for ad_group_id in scene.ad_groups_by_campaign.get(amazon_campaign, []):
                scene.negatives.add((ad_group_id, expression))
    scene.recent_changes = _recent_changes(session, profile, thresholds.cooldown_days, today)
    return scene


def _recent_changes(session: Session, profile: PpcProfile, cooldown_days: int,
                    today: date) -> dict[tuple[str, str], date]:
    """When each entity was last changed, so a cooldown can be honoured.

    Filtered by profile and by date in SQL: reading every action ever executed, for every
    marketplace, would both grow without limit and let Canada's changes freeze the US account.
    """
    since = today - timedelta(days=cooldown_days)
    out: dict[tuple[str, str], date] = {}
    from datetime import datetime, time, timezone

    floor = datetime.combine(since, time.min, tzinfo=timezone.utc)
    for action in session.execute(select(PpcAction).where(
            PpcAction.profile_id == profile.id,
            PpcAction.executed_at.is_not(None),
            PpcAction.executed_at >= floor)).scalars():
        when = action.executed_at.date() if action.executed_at else None
        if when is None or when < since:
            continue
        key = (action.entity_type, action.amazon_entity_id or "")
        if key[1] and (key not in out or out[key] < when):
            out[key] = when
    return out


# ---------------- 1. wasted spend ----------------
@analyser("wasted_spend")
def wasted_spend(scene: Scene) -> list[Candidate]:
    """Targets taking clicks and returning nothing.

    The fixed "30 clicks and no orders" rule is a reasonable default and a poor test on its own: on a
    £9 product needing a 12% conversion rate, 30 dry clicks is damning; on a £90 one needing 1%, it is
    barely a signal. So both numbers are computed — the threshold the owner set, and the number of clicks at
    which zero orders stops being bad luck for *this* product — and the stronger one decides whether
    the proposal is a pause or a bid cut.
    """
    out = []
    for target_id, totals in scene.target_totals.items():
        target = scene.targets.get(target_id)
        if target is None or target.state != "enabled" or totals.orders or not totals.clicks:
            continue
        if totals.clicks < scene.thresholds.min_clicks_for_pause:
            continue
        acos_target, source = scene.target_acos(target.amazon_ad_group_id)
        price = scene.price_for(target.amazon_ad_group_id)
        need = candidates.needed_cvr(totals.cpc or 0, price or 0, acos_target or 0)
        sure_at = candidates.clicks_to_be_sure(need) if need else None
        evidence = {**totals.as_dict(), "target_acos": acos_target, "target_acos_source": source,
                    "needed_cvr": need, "clicks_per_order_needed": candidates.clicks_for_one_order(need),
                    "clicks_before_certain": sure_at, "price": price}

        # Three ways to be sure enough to pause:
        #   the statistical test passed;
        #   no conversion rate could make this work at the current CPC (needed CVR at or above 100%);
        #   or there is no target ACOS to test against, in which case the owner's own click threshold —
        #   already passed to get here — is the only evidence available, and it is evidence.
        hopeless = need is not None and need >= 1.0
        untestable = acos_target is None or not price
        confident = (sure_at is not None and totals.clicks >= sure_at) or hopeless or untestable
        wasted = round(totals.spend, 2)
        monthly = wasted * (30.0 / max(totals.days, 1))
        if confident:
            out.append(Candidate(
                analyser="wasted_spend", change_type="pause_target", entity_type="target",
                entity_id=target_id, campaign_id=target.amazon_campaign_id,
                ad_group_id=target.amazon_ad_group_id, label=target.expression or target_id,
                current_value=target.state, proposed_value="paused",
                reason=_pause_reason(totals, wasted, need, sure_at, acos_target, hopeless,
                                     untestable),
                evidence=evidence, risk="low", confidence=0.85, spend_at_risk=0.0,
                monthly_effect=monthly,
                predicted_effect={"metric": "spend", "direction": "down",
                                  "monthly_saving": round(monthly, 2)}))
        else:
            current = _bid_of(scene, target)
            if not current:
                continue
            # A straight step down by the allowed maximum. Running the ACOS formula with invented
            # inputs (an ACOS of 1.0 against a target of 0.5) produced the same number while
            # claiming to have measured something — there are no sales here to measure.
            new_bid, notes = candidates.step_down(
                current, max_change_pct=scene.thresholds.max_bid_change_pct)
            out.append(Candidate(
                analyser="wasted_spend", change_type="bid_decrease", entity_type="target",
                entity_id=target_id, campaign_id=target.amazon_campaign_id,
                ad_group_id=target.amazon_ad_group_id, label=target.expression or target_id,
                current_value=current, proposed_value=new_bid,
                reason=(f"{totals.clicks} clicks and no orders in {totals.days} days, costing "
                        f"{wasted:.2f}. That is enough to cut the bid but not yet enough to call it "
                        f"dead — at this product's numbers that takes about {sure_at or '—'} clicks."),
                evidence=evidence, risk="low", confidence=0.6,
                monthly_effect=monthly * 0.25, notes=notes,
                predicted_effect={"metric": "spend", "direction": "down",
                                  "monthly_saving": round(monthly * 0.25, 2)}))
    return out


def _pause_reason(totals: Rollup, wasted: float, need: float | None, sure_at: int | None,
                  acos_target: float | None, hopeless: bool, untestable: bool) -> str:
    """Why this is a pause, in the terms that actually apply to it."""
    head = (f"{totals.clicks} clicks and no orders in {totals.days} days, costing {wasted:.2f}. ")
    if hopeless and need is not None:
        return head + (f"At {totals.cpc:.2f} a click, this product would have to convert at "
                       f"{need * 100:.0f}% to hit its {acos_target * 100:.0f}% target — there is no "
                       "conversion rate that makes this work at this cost per click.")
    if untestable:
        return head + ("I can't work out what this product can afford, so I'm going on the click "
                       "count alone — which is past the threshold you set.")
    return head + (f"At {totals.cpc:.2f} a click this product needs to convert at "
                   f"{need * 100:.1f}% to hit its {acos_target * 100:.0f}% target, so {sure_at} dry "
                   "clicks is past the point where this is bad luck.")


# ---------------- 2. over-target ACOS ----------------
@analyser("over_target_acos")
def over_target_acos(scene: Scene) -> list[Candidate]:
    """Targets that sell, but cost more per sale than the product can afford."""
    out = []
    for target_id, totals in scene.target_totals.items():
        target = scene.targets.get(target_id)
        if target is None or target.state != "enabled" or totals.acos is None:
            continue
        if totals.orders < scene.thresholds.min_orders_for_acos_change:
            continue
        if totals.clicks < scene.thresholds.min_clicks_for_bid_change:
            continue
        acos_target, source = scene.target_acos(target.amazon_ad_group_id)
        if acos_target is None or totals.acos <= acos_target:
            continue
        current = _bid_of(scene, target)
        if not current:
            continue
        new_bid, notes = candidates.bid_for_acos(
            current, totals.acos, acos_target,
            max_change_pct=scene.thresholds.max_bid_change_pct)
        if new_bid >= current:
            continue
        # Cutting the bid buys fewer clicks at a lower price; the saving is estimated from the
        # overspend against target rather than from the bid change, which is the honest direction.
        affordable = totals.sales * acos_target
        overspend = max(totals.spend - affordable, 0.0)
        monthly = overspend * (30.0 / max(totals.days, 1))
        out.append(Candidate(
            analyser="over_target_acos", change_type="bid_decrease", entity_type="target",
            entity_id=target_id, campaign_id=target.amazon_campaign_id,
            ad_group_id=target.amazon_ad_group_id, label=target.expression or target_id,
            current_value=current, proposed_value=new_bid,
            reason=(f"ACOS {totals.acos * 100:.0f}% against a {acos_target * 100:.0f}% target over "
                    f"{totals.days} days ({totals.orders} orders from {totals.clicks} clicks). "
                    f"Spending {totals.spend:.2f} where {affordable:.2f} was affordable."),
            evidence={**totals.as_dict(), "target_acos": acos_target, "target_acos_source": source,
                      "affordable_spend": round(affordable, 2), "overspend": round(overspend, 2),
                      "bid_inherited": target.bid is None},
            risk="low", confidence=0.75, monthly_effect=monthly, notes=notes,
            predicted_effect={"metric": "acos", "direction": "down", "toward": acos_target,
                              "monthly_saving": round(monthly, 2)}))
    return out


# ---------------- 3. under-target and under-exposed ----------------
@analyser("under_exposed")
def under_exposed(scene: Scene) -> list[Candidate]:
    """Targets doing better than they need to, with room to buy more of the same.

    Efficiency alone is not a reason to bid up — a target at half its target ACOS with 95% of
    top-of-search impressions has nothing left to buy. Headroom is the other half of the condition,
    which is why M2 added the impression-share column.
    """
    out = []
    for target_id, totals in scene.target_totals.items():
        target = scene.targets.get(target_id)
        if target is None or target.state != "enabled" or totals.acos is None:
            continue
        if totals.orders < scene.thresholds.min_orders_for_acos_change:
            continue
        if totals.clicks < scene.thresholds.min_clicks_for_bid_change:
            continue
        acos_target, source = scene.target_acos(target.amazon_ad_group_id)
        if acos_target is None or totals.acos > acos_target * UNDER_TARGET_RATIO:
            continue
        share = _top_of_search_share(scene, target.amazon_campaign_id)
        if share is not None and share >= HEADROOM_SHARE:
            continue
        current = _bid_of(scene, target)
        if not current:
            continue
        new_bid, notes = candidates.bid_for_acos(
            current, totals.acos, acos_target,
            max_change_pct=scene.thresholds.max_bid_change_pct)
        if new_bid <= current:
            continue
        # The brief pairs efficiency with headroom. Where Amazon has given no impression share, the
        # headroom half is unproven rather than false — so the proposal is still made, said plainly,
        # and carries less confidence than one with the figure behind it.
        headroom = (f"{share:.0f}% of top-of-search impressions" if share is not None
                    else "no impression-share figure from Amazon yet, so the headroom is unproven")
        extra_clicks = totals.clicks * (30.0 / max(totals.days, 1)) * 0.2
        upside = extra_clicks * (totals.rpc or 0) * (1 - acos_target)
        out.append(Candidate(
            analyser="under_exposed", change_type="bid_increase", entity_type="target",
            entity_id=target_id, campaign_id=target.amazon_campaign_id,
            ad_group_id=target.amazon_ad_group_id, label=target.expression or target_id,
            current_value=current, proposed_value=new_bid,
            asin=(scene.asins_for(target.amazon_ad_group_id) or [None])[0],
            reason=(f"ACOS {totals.acos * 100:.0f}% against a {acos_target * 100:.0f}% target over "
                    f"{totals.days} days, and it only has {headroom}. There is room to buy more of "
                    f"what is already working."),
            evidence={**totals.as_dict(), "target_acos": acos_target, "target_acos_source": source,
                      "top_of_search_share": share, "headroom_threshold": HEADROOM_SHARE,
                      "headroom_proven": share is not None},
            risk="medium", confidence=0.6 if share is not None else 0.45,
            spend_at_risk=round(extra_clicks * (totals.cpc or 0), 2),
            monthly_effect=round(upside, 2), notes=notes,
            predicted_effect={"metric": "sales", "direction": "up",
                              "monthly_gain": round(upside, 2)}))
    return out


def _bid_of(scene: Scene, target: PpcTarget) -> float | None:
    """The bid actually in force: the target's own, or the ad group default it inherits.

    Skipping targets with no bid of their own excluded every inheriting target from every bid
    analyser — which on a tidy account is most of them.
    """
    if target.bid is not None:
        return float(target.bid)
    group = scene.ad_group_defaults.get(target.amazon_ad_group_id)
    return float(group) if group is not None else None


def _top_of_search_share(scene: Scene, campaign_id: str | None) -> float | None:
    """Top-of-search impression share per campaign, weighted by impressions and cached.

    Weighted because a day with 20 impressions and a day with 20,000 are not equal evidence, and
    cached because this was one query per qualifying target — on a large account, thousands of them.
    """
    if not campaign_id:
        return None
    if campaign_id in scene.share_cache:
        return scene.share_cache[campaign_id]
    totals = scene.campaign_totals.get(campaign_id)
    if totals is None:
        scene.share_cache[campaign_id] = None
        return None
    from app.ppc import metrics

    rows = metrics.daily(scene.session, scene.profile, entity_type="campaign",
                         entity_ids=[campaign_id], start=totals.start, end=totals.end)
    weighted = sum(float(r.top_of_search_impression_share) * max(r.impressions, 1) for r in rows
                   if r.top_of_search_impression_share is not None)
    weight = sum(max(r.impressions, 1) for r in rows
                 if r.top_of_search_impression_share is not None)
    value = round(weighted / weight, 1) if weight else None
    scene.share_cache[campaign_id] = value
    return value


# ---------------- 4. search-term harvesting ----------------
@analyser("harvest")
def harvest(scene: Scene) -> list[Candidate]:
    """Search terms already selling, which deserve a target of their own.

    The starting bid is what the term itself says a click is worth: revenue per click times the target
    ACOS. That is the most a click can cost and still hit the target, and it beats copying the ad
    group default, which knows nothing about this term.
    """
    out = []
    for term in scene.terms:
        if term.harvest_status != "new" or term.orders < scene.thresholds.harvest_min_orders:
            continue
        acos_target, source = scene.target_acos(term.ad_group_id)
        if acos_target is None or term.acos is None or term.acos > acos_target:
            continue
        if _already_targeted(scene, term):
            continue
        destination, note = _harvest_destination(scene, term)
        if destination is None:
            continue
        bid = round(max((term.rpc or 0) * acos_target, candidates.MIN_BID), 2)
        monthly = term.sales * (30.0 / max(term.days, 1)) * 0.15
        out.append(Candidate(
            analyser="harvest", change_type="harvest_search_term", entity_type="search_term",
            entity_id=f"{term.ad_group_id}|{term.search_term}",
            campaign_id=scene.ad_group_campaign.get(destination, term.campaign_id),
            ad_group_id=destination, label=term.search_term,
            current_value=None, proposed_value={"match_type": "exact", "bid": bid,
                                                "expression": term.search_term,
                                                "ad_group_id": destination,
                                                "found_in_ad_group": term.ad_group_id},
            asin=(scene.asins_for(term.ad_group_id) or [None])[0],
            reason=(f"“{term.search_term}” brought {term.orders} orders from {term.clicks} "
                    f"clicks at {term.acos * 100:.0f}% ACOS over {term.days} days, inside the "
                    f"{acos_target * 100:.0f}% target. Worth its own exact-match target at "
                    f"{bid:.2f} — that is what its own revenue per click can afford."),
            evidence={**term.as_dict(), "target_acos": acos_target, "target_acos_source": source,
                      "revenue_per_click": term.rpc, "starting_bid": bid,
                      "destination_ad_group": destination, "destination_note": note,
                      "ad_group_id": destination, "campaign_id":
                      scene.ad_group_campaign.get(destination, term.campaign_id)},
            risk="low", confidence=0.7, spend_at_risk=round(bid * 30, 2),
            monthly_effect=round(monthly, 2), notes=[note] if note else [],
            predicted_effect={"metric": "sales", "direction": "up",
                              "monthly_gain": round(monthly, 2)}))
    return out


def _harvest_destination(scene: Scene, term: TermRollup) -> tuple[str | None, str | None]:
    """Where a harvested keyword can actually live.

    An automatic campaign cannot accept keywords — which is the whole point of harvesting out of one —
    so proposing an exact keyword in the ad group the term was found in would be refused by Amazon.
    The destination is a manual ad group advertising the same products; without one there is nothing
    to propose yet, and saying "add a manual campaign first" is a structure finding, not a harvest.
    """
    campaign_id = scene.ad_group_campaign.get(term.ad_group_id, term.campaign_id or "")
    campaign = scene.campaigns.get(campaign_id)
    if campaign is not None and (campaign.targeting_type or "").lower() != "auto":
        return term.ad_group_id, None

    wanted = set(scene.asins_for(term.ad_group_id))
    for ad_group_id, asins in scene.ad_group_asins.items():
        other = scene.campaigns.get(scene.ad_group_campaign.get(ad_group_id, ""))
        if other is None or other.state != "enabled":
            continue
        if (other.targeting_type or "").lower() == "auto":
            continue
        if wanted and not wanted.intersection(asins):
            continue
        return ad_group_id, (f"found in the automatic campaign, so it goes into "
                             f"{other.name} instead — automatic campaigns can't hold keywords")
    log.info("nothing to harvest %r into: no manual ad group advertises these products",
             term.search_term)
    return None, None


def _already_targeted(scene: Scene, term: TermRollup) -> bool:
    """Don't propose a keyword the ad group already has, in any match type."""
    text = term.search_term.strip().lower()
    for target in scene.targets_by_ad_group.get(term.ad_group_id, []):
        if (target.expression or "").strip().lower() == text:
            return True
    return False


# ---------------- 5. negatives ----------------
@analyser("negatives")
def negatives(scene: Scene) -> list[Candidate]:
    """Search terms spending without selling, and the patterns behind them."""
    out: list[Candidate] = []
    dry = [t for t in scene.terms
           if not t.orders and t.spend >= scene.thresholds.wasted_spend_floor
           and t.harvest_status in ("new", "watching")]
    for term in dry:
        if _already_negative(scene, term):
            continue
        monthly = term.spend * (30.0 / max(term.days, 1))
        out.append(Candidate(
            analyser="negatives", change_type="add_negative_exact", entity_type="search_term",
            entity_id=f"{term.ad_group_id}|{term.search_term}", campaign_id=term.campaign_id,
            ad_group_id=term.ad_group_id, label=term.search_term,
            current_value=None, proposed_value={"match_type": "negative_exact",
                                                "expression": term.search_term},
            reason=(f"“{term.search_term}” has cost {term.spend:.2f} over {term.days} days "
                    f"across {term.clicks} clicks and sold nothing."),
            evidence={**term.as_dict(), "floor": scene.thresholds.wasted_spend_floor},
            risk="low", confidence=0.8, monthly_effect=round(monthly, 2),
            predicted_effect={"metric": "spend", "direction": "down",
                              "monthly_saving": round(monthly, 2)}))

    patterns = _negative_patterns(scene, dry)
    # A phrase candidate claims the whole spend of every term it covers, and each of those terms also
    # has its own exact candidate claiming the same money. Left alone, three dead searches worth $24
    # ranked as $96 of savings and crowded out real findings. The phrase keeps the money; the exact
    # candidates it covers keep their place in the digest but stop claiming it twice.
    covered = {term for candidate in patterns for term in candidate.evidence.get("covers") or []}
    for candidate in out:
        if candidate.label in covered:
            candidate.monthly_effect = 0.0
            candidate.notes.append("counted under the pattern it belongs to, so it isn't added twice")
    return out + patterns


def _negative_patterns(scene: Scene, dry: list[TermRollup]) -> list[Candidate]:
    """A word appearing across several dry terms is worth blocking as a phrase, not one by one.

    Three separate negatives for "wholesale mint pouches", "wholesale pouches bulk" and "pouches
    wholesale uk" treat a pattern as three accidents. One negative phrase on "wholesale" is the
    actual finding, and it says which terms it came from.
    """
    out = []
    claimed: set[str] = set()
    by_group: dict[str, list[TermRollup]] = defaultdict(list)
    for term in dry:
        by_group[term.ad_group_id].append(term)
    for ad_group_id, terms in by_group.items():
        if len(terms) < 3:
            continue
        counts: Counter[str] = Counter()
        for term in terms:
            for word in set(re.findall(r"[a-z0-9]+", term.search_term.lower())):
                if word not in STOPWORDS and len(word) > 2:
                    counts[word] += 1
        own_words = _own_target_words(scene, ad_group_id)
        for word, seen in counts.most_common(3):
            if seen < 3:
                continue
            if word in own_words:
                # This would switch off the ad group's own keyword. The pattern may still be real,
                # but a negative phrase is the wrong instrument for it.
                log.info("not proposing %r as a negative phrase: it is this ad group's own target",
                         word)
                continue
            matching = [t for t in terms if word in t.search_term.lower()]
            spend = sum(t.spend for t in matching)
            if spend < scene.thresholds.wasted_spend_floor * 2:
                continue
            claimed.update(t.search_term for t in matching)
            if (ad_group_id, word) in scene.negatives:
                continue
            monthly = spend * (30.0 / max(terms[0].days, 1))
            out.append(Candidate(
                analyser="negatives", change_type="add_negative_phrase", entity_type="ad_group",
                entity_id=ad_group_id, campaign_id=matching[0].campaign_id,
                ad_group_id=ad_group_id, label=word,
                current_value=None,
                proposed_value={"match_type": "negative_phrase", "expression": word},
                reason=(f"{seen} different searches containing “{word}” have cost "
                        f"{spend:.2f} between them and sold nothing: "
                        f"{', '.join(t.search_term for t in matching[:4])}. One negative phrase "
                        f"blocks the pattern rather than chasing each search."),
                evidence={"pattern": word, "terms": [t.as_dict() for t in matching[:10]],
                          "terms_matched": seen, "spend": round(spend, 2),
                          "covers": [t.search_term for t in matching]},
                risk="medium", confidence=0.65, monthly_effect=round(monthly, 2),
                predicted_effect={"metric": "spend", "direction": "down",
                                  "monthly_saving": round(monthly, 2)}))
    return out


def _own_target_words(scene: Scene, ad_group_id: str) -> set[str]:
    """Every word in this ad group's live targets. A negative phrase matching one kills it."""
    words: set[str] = set()
    for target in scene.targets_by_ad_group.get(ad_group_id, []):
        if target.state != "enabled":
            continue
        for word in re.findall(r"[a-z0-9]+", (target.expression or "").lower()):
            words.add(word)
    return words


def _already_negative(scene: Scene, term: TermRollup) -> bool:
    """Whether this term is already blocked *in this ad group*.

    Scope matters: ignoring it meant one campaign's negative silenced the same search everywhere,
    so a term wasting money in a second campaign was never proposed.
    """
    text = term.search_term.strip().lower()
    campaign = scene.ad_group_campaign.get(term.ad_group_id, term.campaign_id or "")
    return ((term.ad_group_id, text) in scene.negatives
            or (campaign, text) in scene.negatives)


# ---------------- 6. budget ----------------
@analyser("budget")
def budget(scene: Scene) -> list[Candidate]:
    """Campaigns held back by their budget while paying for themselves, and the reverse."""
    out = []
    for campaign_id, totals in scene.campaign_totals.items():
        campaign = scene.campaigns.get(campaign_id)
        if campaign is None or campaign.state != "enabled" or not campaign.daily_budget:
            continue
        cap = float(campaign.daily_budget)
        rows = scene.campaign_days.get(campaign_id) or []
        if not rows:
            continue
        capped_days = [r for r in rows if float(r.spend) >= cap * 0.95]
        acos_target, source = _campaign_target_acos(scene, campaign_id)
        evidence = {**totals.as_dict(), "daily_budget": cap,
                    "days_at_budget": len(capped_days), "days_measured": len(rows),
                    "target_acos": acos_target, "target_acos_source": source}

        if not totals.sales and totals.spend >= scene.thresholds.wasted_spend_floor \
                and totals.clicks >= scene.thresholds.min_clicks_for_pause:
            # Spending with nothing to show for it. There is no ACOS to compare, which is exactly
            # why the earlier version said nothing about the worst campaigns in the account.
            spent_per_day = totals.spend / max(len(rows), 1)
            proposed = round(max(spent_per_day * 0.5, MIN_DAILY_BUDGET), 2)
            if proposed < cap:
                monthly = totals.spend * (30.0 / max(totals.days, 1))
                out.append(Candidate(
                    analyser="budget", change_type="budget_decrease", entity_type="campaign",
                    entity_id=campaign_id, campaign_id=campaign_id, label=campaign.name,
                    current_value=cap, proposed_value=proposed,
                    reason=(f"{campaign.name} has spent {totals.spend:.2f} over {totals.days} days "
                            f"across {totals.clicks} clicks and sold nothing at all. Halving the "
                            f"budget limits it while you decide whether to keep it."),
                    evidence=evidence, risk="low", confidence=0.7,
                    monthly_effect=round(monthly * 0.5, 2),
                    predicted_effect={"metric": "spend", "direction": "down",
                                      "monthly_saving": round(monthly * 0.5, 2)}))
            continue
        if len(capped_days) >= max(3, len(rows) // 3) and acos_target and totals.acos \
                and totals.acos <= acos_target:
            proposed = round(cap * 1.25, 2)
            missed = (totals.sales / max(len(rows), 1)) * 0.25 * 30
            out.append(Candidate(
                analyser="budget", change_type="budget_increase", entity_type="campaign",
                entity_id=campaign_id, campaign_id=campaign_id, label=campaign.name,
                current_value=cap, proposed_value=proposed,
                reason=(f"{campaign.name} hit its {cap:.2f} budget on {len(capped_days)} of "
                        f"{len(rows)} days at {totals.acos * 100:.0f}% ACOS, inside its "
                        f"{acos_target * 100:.0f}% target — it is being stopped while it is working."),
                evidence=evidence, risk="medium", confidence=0.65,
                spend_at_risk=round((proposed - cap) * 30, 2), monthly_effect=round(missed, 2),
                predicted_effect={"metric": "sales", "direction": "up",
                                  "monthly_gain": round(missed, 2)}))
        elif not capped_days and acos_target and totals.acos and totals.acos > acos_target * 1.5 \
                and totals.clicks >= scene.thresholds.min_clicks_for_bid_change:
            # The *busiest* day, not the mean: a budget at 1.1x the average truncates every day
            # above it, which is half of them, while the message claims nothing changes today.
            busiest = max((float(r.spend) for r in rows), default=0.0)
            proposed = round(max(busiest * 1.1, MIN_DAILY_BUDGET), 2)
            if proposed >= cap:
                continue
            out.append(Candidate(
                analyser="budget", change_type="budget_decrease", entity_type="campaign",
                entity_id=campaign_id, campaign_id=campaign_id, label=campaign.name,
                current_value=cap, proposed_value=proposed,
                reason=(f"{campaign.name} never reaches its {cap:.2f} budget and runs at "
                        f"{totals.acos * 100:.0f}% ACOS against a {acos_target * 100:.0f}% target. "
                        f"Bringing the budget down to {proposed:.2f} caps the damage — that is "
                        f"above its busiest day ({busiest:.2f}), so nothing it does today changes."),
                evidence=evidence, risk="low", confidence=0.6,
                monthly_effect=0.0,
                predicted_effect={"metric": "spend", "direction": "capped"}))
    return out


def _campaign_target_acos(scene: Scene, campaign_id: str) -> tuple[float | None, str]:
    """A campaign's target is the strictest of *its own* ad groups'.

    The first version walked every ad group in the account and used a loose test to decide which
    belonged to the campaign, so a campaign with no priceable products could borrow the target ACOS
    of an unrelated one — and then have its budget cut for missing a number that was never its own.
    """
    found = []
    for ad_group_id in scene.ad_groups_by_campaign.get(campaign_id, []):
        value, source = scene.target_acos(ad_group_id)
        if value is not None:
            found.append((value, source))
    if not found:
        return None, "no target ACOS for this campaign's products yet"
    return min(found)


# ---------------- 7. placement ----------------
@analyser("placement")
def placement(scene: Scene) -> list[Candidate]:
    """Where in the results a campaign converts, which is what the placement multipliers buy."""
    out = []
    for campaign_id, rows in scene.placements.items():
        campaign = scene.campaigns.get(campaign_id)
        if campaign is None or campaign.state != "enabled":
            continue
        measurable = [r for r in rows if r.clicks >= scene.thresholds.placement_min_clicks
                      and r.acos is not None]
        if len(measurable) < 2:
            continue
        for row in measurable:
            # Compared against the campaign *without* this placement. Measuring it against an
            # average that includes itself booked one gap twice — a two-placement campaign produced
            # the same finding from both ends of a single comparison.
            others = [r for r in rows if r.placement != row.placement]
            rest_spend = sum(r.spend for r in others)
            rest_sales = sum(r.sales for r in others)
            if not rest_sales:
                continue
            overall_acos = rest_spend / rest_sales
            gap = (row.acos - overall_acos) / overall_acos if overall_acos else 0
            if abs(gap) < PLACEMENT_GAP:
                continue
            direction = "down" if gap > 0 else "up"
            current = _current_adjustment(campaign, row.placement)
            proposed = _adjusted(current, gap)
            if proposed == current:
                continue
            out.append(Candidate(
                analyser="placement", change_type="placement_adjustment", entity_type="campaign",
                entity_id=campaign_id, campaign_id=campaign_id,
                label=f"{campaign.name} — {row.placement.replace('_', ' ')}",
                current_value=current, proposed_value=proposed,
                reason=(f"{row.placement.replace('_', ' ')} runs at {row.acos * 100:.0f}% ACOS "
                        f"against {overall_acos * 100:.0f}% everywhere else in the campaign over "
                        f"{row.days} days "
                        f"({row.clicks} clicks). Adjusting the multiplier {direction} moves spend "
                        f"toward where it converts."),
                evidence={**row.as_dict(), "other_placements_acos": round(overall_acos, 4),
                          "gap_vs_rest": round(gap, 4),
                          "placements": [r.as_dict() for r in rows]},
                risk="medium", confidence=0.6,
                monthly_effect=round(abs(row.spend - row.sales * overall_acos)
                                     * (30.0 / max(row.days, 1)), 2),
                predicted_effect={"metric": "acos", "direction": "down"}))
    return out


def _current_adjustment(campaign: PpcCampaign, placement: str) -> float:
    wanted = {"top_of_search": "PLACEMENT_TOP", "product_pages": "PLACEMENT_PRODUCT_PAGE",
              "rest_of_search": "PLACEMENT_REST_OF_SEARCH"}.get(placement)
    for entry in campaign.placement_adjustments or []:
        if isinstance(entry, dict) and entry.get("placement") == wanted:
            try:
                return float(entry.get("percentage") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _adjusted(current: float, gap: float) -> float:
    """Move the multiplier a quarter of the way toward closing the gap, within Amazon's 0–900."""
    step = max(min(-gap * 100 * 0.25, 25.0), -25.0)
    return round(max(min(current + step, 900.0), 0.0))


# ---------------- 8. structure ----------------
@analyser("structure")
def structure(scene: Scene) -> list[Candidate]:
    """Problems with the shape of the account rather than with any one number.

    These are proposals for the owner to think about, not bid tweaks: they change how the account is laid
    out, so they carry a higher risk and a plainer explanation.
    """
    out: list[Candidate] = []
    out.extend(_duplicate_targets(scene))
    out.extend(_asins_without_exact_coverage(scene))
    out.extend(_auto_campaigns_without_harvesting(scene))
    out.extend(_missing_discovery_negatives(scene))
    return out


def _missing_discovery_negatives(scene: Scene) -> list[Candidate]:
    """A term harvested into exact match should be blocked in the campaign that found it.

    Otherwise the discovery campaign keeps buying the same search that now has its own target, and
    the two bid against each other — the account pays twice for one shopper. Section 7.1 asks for
    this by name, and it is the one structure problem that costs money every day it is left.
    """
    out = []
    exact_by_asin: dict[str, set[str]] = defaultdict(set)
    for ad_group_id, targets in scene.targets_by_ad_group.items():
        for target in targets:
            if target.match_type == "exact" and target.state == "enabled" and target.expression:
                for asin in scene.asins_for(ad_group_id):
                    exact_by_asin[asin].add(target.expression.strip().lower())

    for term in scene.terms:
        campaign_id = scene.ad_group_campaign.get(term.ad_group_id, term.campaign_id or "")
        campaign = scene.campaigns.get(campaign_id)
        if campaign is None or campaign.state != "enabled":
            continue
        is_discovery = ((campaign.targeting_type or "").lower() == "auto"
                        or any(t.match_type in ("broad", "phrase")
                               for t in scene.targets_by_ad_group.get(term.ad_group_id, [])))
        if not is_discovery:
            continue
        text = term.search_term.strip().lower()
        covered = any(text in exact_by_asin.get(asin, set())
                      for asin in scene.asins_for(term.ad_group_id))
        if not covered or _already_negative(scene, term):
            continue
        monthly = term.spend * (30.0 / max(term.days, 1))
        out.append(Candidate(
            analyser="structure", change_type="add_negative_exact", entity_type="search_term",
            entity_id=f"{term.ad_group_id}|{term.search_term}", campaign_id=campaign_id,
            ad_group_id=term.ad_group_id, label=term.search_term,
            current_value=None,
            proposed_value={"match_type": "negative_exact", "expression": term.search_term},
            reason=(f"\u201c{term.search_term}\u201d already has its own exact-match target, but "
                    f"{campaign.name} is still buying it — {term.spend:.2f} over {term.days} days. "
                    f"Blocking it here stops the two bidding against each other."),
            evidence={**term.as_dict(), "campaign": campaign.name,
                      "ad_group_id": term.ad_group_id, "campaign_id": campaign_id,
                      "reason_code": "already_covered_by_exact"},
            risk="low", confidence=0.75, monthly_effect=round(monthly, 2),
            predicted_effect={"metric": "spend", "direction": "down",
                              "monthly_saving": round(monthly, 2)}))
    return out


def _duplicate_targets(scene: Scene) -> list[Candidate]:
    """The same keyword live in two places, bidding against itself."""
    by_text: dict[str, list[PpcTarget]] = defaultdict(list)
    for target in scene.targets.values():
        if target.target_kind != "keyword":
            continue
        text = (target.expression or "").strip().lower()
        if text:
            by_text[text].append(target)
    out = []
    for text, group in by_text.items():
        if len(group) < 2:
            continue
        campaigns = {t.amazon_campaign_id for t in group}
        if len(campaigns) < 2:
            continue
        rollups = [(t, scene.target_totals.get(t.amazon_target_id)) for t in group]
        measured = [(t, r) for t, r in rollups if r is not None and r.clicks]
        if len(measured) < 2:
            continue
        best = min(measured, key=lambda pair: pair[1].acos if pair[1].acos is not None else 9.9)
        worst = max(measured, key=lambda pair: pair[1].acos if pair[1].acos is not None else 9.9)
        if best[0].amazon_target_id == worst[0].amazon_target_id:
            continue
        out.append(Candidate(
            analyser="structure", change_type="campaign_structure", entity_type="target",
            entity_id=worst[0].amazon_target_id, campaign_id=worst[0].amazon_campaign_id,
            ad_group_id=worst[0].amazon_ad_group_id, label=text,
            current_value=worst[0].state, proposed_value="paused",
            reason=(f"“{text}” is live in {len(campaigns)} campaigns and they are bidding "
                    f"against each other. The better copy runs at "
                    f"{(best[1].acos or 0) * 100:.0f}% ACOS, this one at "
                    f"{(worst[1].acos or 0) * 100:.0f}%."),
            evidence={"duplicates": [{"target_id": t.amazon_target_id,
                                      "campaign_id": t.amazon_campaign_id, **r.as_dict()}
                                     for t, r in measured]},
            risk="medium", confidence=0.55,
            monthly_effect=round(worst[1].spend * (30.0 / max(worst[1].days, 1)) * 0.3, 2),
            predicted_effect={"metric": "spend", "direction": "down"}))
    return out


def _asins_without_exact_coverage(scene: Scene) -> list[Candidate]:
    """A product selling through broad or auto targeting, with no exact-match target of its own."""
    out = []
    for ad_group_id, asins in scene.ad_group_asins.items():
        targets = scene.targets_by_ad_group.get(ad_group_id, [])
        if not targets:
            continue
        has_exact = any(t.match_type == "exact" and t.state == "enabled" for t in targets)
        if has_exact:
            continue
        selling = [t for t in targets
                   if (scene.target_totals.get(t.amazon_target_id) or Rollup(
                       entity_type="target", entity_id="", days=0, start=scene.today,
                       end=scene.today)).orders]
        if not selling:
            continue
        campaign_id = targets[0].amazon_campaign_id
        total_orders = sum((scene.target_totals.get(t.amazon_target_id) or Rollup(
            entity_type="target", entity_id="", days=0, start=scene.today,
            end=scene.today)).orders for t in selling)
        out.append(Candidate(
            analyser="structure", change_type="campaign_structure", entity_type="ad_group",
            entity_id=ad_group_id, campaign_id=campaign_id, ad_group_id=ad_group_id,
            label=", ".join(asins[:2]),
            current_value="no exact-match targets", proposed_value="add exact-match coverage",
            reason=(f"This ad group has sold {total_orders} orders through broad or automatic "
                    f"targeting with no exact-match target of its own, so the searches that are "
                    f"working cost more than they need to."),
            evidence={"asins": asins, "orders": total_orders,
                      "match_types": sorted({t.match_type for t in targets})},
            risk="medium", confidence=0.5, monthly_effect=0.0,
            predicted_effect={"metric": "acos", "direction": "down"}))
    return out


def _auto_campaigns_without_harvesting(scene: Scene) -> list[Candidate]:
    """An auto campaign is a discovery tool. One that is never harvested is just a bill."""
    out = []
    for campaign_id, campaign in scene.campaigns.items():
        if campaign.state != "enabled" or (campaign.targeting_type or "") != "auto":
            continue
        totals = scene.campaign_totals.get(campaign_id)
        if totals is None or not totals.orders:
            continue
        terms = [t for t in scene.terms if t.campaign_id == campaign_id]
        harvested = [t for t in terms if t.harvest_status == "harvested"]
        winners = [t for t in terms if t.orders >= scene.thresholds.harvest_min_orders]
        if harvested or not winners:
            continue
        out.append(Candidate(
            analyser="structure", change_type="campaign_structure", entity_type="campaign",
            entity_id=campaign_id, campaign_id=campaign_id, label=campaign.name,
            current_value=f"{len(winners)} proven searches, none harvested",
            proposed_value="harvest the winners into exact match",
            reason=(f"{campaign.name} is an automatic campaign that has found {len(winners)} searches "
                    f"with orders, and none has been moved into its own target. Discovery is doing "
                    f"its job; nothing is being kept."),
            evidence={"winners": [t.as_dict() for t in winners[:10]], **totals.as_dict()},
            risk="low", confidence=0.6, monthly_effect=0.0,
            predicted_effect={"metric": "acos", "direction": "down"}))
    return out


# ---------------- running them all, then the filters ----------------
@analyser("dayparting")
def _dayparting(scene: Scene) -> list[Candidate]:
    """The shape of the day, when there is hourly data to see it in.

    Registered like any other analyser so its findings travel through the playbook, the context
    filters and the digest with everything else. Without Marketing Stream it costs one indexed query
    that returns nothing, which is the right price for a feature that is switched off.
    """
    from app.ppc import dayparting

    return dayparting.candidates(scene.session, scene.profile, today=scene.today)


def run_all(session: Session, profile: PpcProfile, *, today: date | None = None,
            only: list[str] | None = None, scene: Scene | None = None,
            ad_product: str = AD_PRODUCT) -> dict[str, Any]:
    """Every analyser, then the context and cooldown filters. Returns candidates and why any are held."""
    scene = scene or load(session, profile, today=today, ad_product=ad_product)
    produced: list[Candidate] = []
    per_analyser: dict[str, int] = {}
    for name, fn in ANALYSERS.items():
        if only and name not in only:
            continue
        try:
            found = fn(scene)
        except Exception:
            # One analyser failing must not cost the others their findings.
            log.exception("the %s analyser failed", name)
            per_analyser[name] = -1
            continue
        per_analyser[name] = len(found)
        for candidate in found:
            # One place, so a new analyser cannot forget and a wrong value cannot be defaulted in.
            candidate.ad_product = candidate.ad_product or scene.ad_product
        produced.extend(found)

    # Step 4 of the daily cycle: the learned rules add, modify or suppress what the analysers
    # produced, before the filters run. Code applies them, and each candidate records which fired.
    from app.ppc import playbook

    produced, applied = playbook.apply_rules(session, produced)

    kept, held, skipped = [], [], []
    for candidate in produced:
        _apply_context(scene, candidate)
        cooled = _cooldown_block(scene, candidate)
        if cooled:
            candidate.holds.append(cooled)
            skipped.append(candidate)
        elif candidate.held:
            held.append(candidate)
        else:
            kept.append(candidate)

    kept.sort(key=lambda c: -abs(c.monthly_effect))
    # A messy account can produce thousands of findings — mostly negatives and harvests, one per
    # search term. The digest only ever shows a capped number, so carrying the rest through costs
    # work nobody reads. What was dropped is counted and reported: a silent truncation would read as
    # "that is everything".
    ceiling = max(scene.thresholds.daily_cap * 4, 100)
    dropped = max(len(kept) - ceiling, 0)
    if dropped:
        log.info("ppc analysis produced more than it can show", extra={"kept": ceiling,
                                                                       "dropped": dropped})
        kept = kept[:ceiling]
    return {"profile": profile.amazon_profile_id, "ad_product": scene.ad_product,
            "window_days": scene.thresholds.lookback_days,
            "per_analyser": per_analyser, "candidates": kept, "held": held, "in_cooldown": skipped,
            "dropped_beyond_cap": dropped, "playbook": applied.as_dict(),
            "counts": {"proposed": len(kept), "held": len(held), "cooldown": len(skipped),
                       "dropped": dropped,
                       "suppressed_by_playbook": len(applied.suppressed)}}


def _spends_more(candidate: Candidate) -> bool:
    """Whether this change buys more traffic.

    `increases_spend` covers the obvious ones. A placement adjustment is the awkward case: the same
    change type can raise or lower the multiplier, so it is only a spend increase when the number
    goes up — and when it does, it is every bit as much a spend increase as a bid rise.
    """
    if candidate.increases_spend:
        return True
    if candidate.change_type == "placement_adjustment":
        try:
            return float(candidate.proposed_value) > float(candidate.current_value or 0)
        except (TypeError, ValueError):
            return True        # can't tell: treat it as spending more and let the filter look
    return False


def _affected_asins(scene: Scene, candidate: Candidate) -> list[str]:
    """Every product a change touches — not just the first one we happened to name.

    A candidate's `asin` is a label for the message. The filter needs the whole set: an ad group
    advertising four ASINs where one is out of stock should not get a bid rise, and a campaign-level
    budget change reaches every ad group under it.
    """
    out: list[str] = []
    for asin in (scene.asins_for(candidate.ad_group_id)
                 + scene.asins_for_campaign(candidate.campaign_id)
                 + ([candidate.asin] if candidate.asin else [])):
        if asin and asin not in out:
            out.append(asin)
    return out


def _apply_context(scene: Scene, candidate: Candidate) -> None:
    """Brief 7.5: never spend more on a product that cannot sell. Cuts are always allowed."""
    if not _spends_more(candidate):
        return
    asins = _affected_asins(scene, candidate)
    if not asins:
        candidate.notes.append("I couldn't work out which product this affects, so I couldn't "
                               "check its stock")
    for asin in [a for a in asins if a]:
        got = scene.contexts.get(asin.upper())
        if got is None:
            continue
        if not got.known:
            candidate.notes.append(f"{asin} isn't in the catalogue I hold, so I can't check its stock")
            continue
        if not got.may_increase:
            candidate.holds.append(f"{asin}: {got.why_not}")
        candidate.evidence.setdefault("context", {})[asin] = got.as_dict()


def _cooldown_block(scene: Scene, candidate: Candidate) -> str | None:
    when = scene.changed_recently(candidate.entity_type, candidate.entity_id)
    if when is None:
        return None
    days = (scene.today - when).days
    return (f"changed {days} days ago; the {scene.thresholds.cooldown_days}-day cooldown has "
            f"{scene.thresholds.cooldown_days - days} days left")
