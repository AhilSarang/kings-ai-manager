"""Intraday budget pace, from the one Ads endpoint that answers in the present tense.

Everything else in Phase 4 reads yesterday. `POST /sp/campaigns/budget/usage` reads *now*: for up to
a hundred campaigns at a time it returns how much of today's budget is already spent. That single
fact is what makes the difference between "your best campaign went dark at 11am and you found out
tomorrow" and finding out at 11am.

Verified against the Sponsored Products OpenAPI spec (`spCampaignsBudgetUsage`), because the details
are not guessable:

* the media type is `application/vnd.spcampaignbudgetusage.v1+json` — lower case, and v1, where
  every other Sponsored Products v3 endpoint is camelCase and v3;
* the request takes 1 to 100 ids, so an empty list is a 422 and 101 ids is a 422;
* the answer is a 207 whose `success` and `error` live at the *top level*, not nested under a
  collection name the way the campaign-management 207s are — `apply.read_multi_status` is the wrong
  reader for it;
* `budgetUsagePercent` is a snapshot taken at `usageUpdatedTimestamp`, not at the moment you asked.

That last point is the one that matters for honesty. Amazon evaluates budget usage periodically, so a
reading can be an hour old. A stale reading is reported as stale rather than treated as current: "87%
of budget used, as of an hour ago" is useful, and "87% used" presented as live is a small lie that
will eventually make a 2pm decision out of 11am data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import PpcCampaign, PpcHourlyMetric, PpcProfile
from app.ppc import api, metrics
from app.ppc.errors import AdsAuthError, AdsError

log = logging.getLogger(__name__)

# Sponsored Products. Sponsored Display has its own `/sd/campaigns/budget/usage` with the same
# shape, so this becomes a table when M12 wires SD up; stated as a constant now so the rows this
# module writes are labelled honestly rather than defaulting.
AD_PRODUCT = "sp"

PATH = "/sp/campaigns/budget/usage"
MEDIA = "application/vnd.spcampaignbudgetusage.v1+json"
OPERATION = "spCampaignsBudgetUsage"

# The spec's own bounds. Sending 101 ids is a 422, and so is sending none.
MAX_IDS = 100

# A reading older than this is reported as stale rather than used for a decision.
STALE_AFTER = timedelta(minutes=90)


@dataclass
class Reading:
    """One campaign's budget, as Amazon last evaluated it."""

    amazon_campaign_id: str
    label: str
    budget: float | None
    used_pct: float | None
    as_of: datetime | None
    currency: str = "USD"

    def age(self, at: datetime) -> timedelta | None:
        """How old this reading was at a given moment. None when Amazon gave no timestamp."""
        if self.as_of is None:
            return None
        return at.astimezone(timezone.utc) - self.as_of

    def is_stale(self, at: datetime, *, day_started: datetime | None = None) -> bool:
        """True when we cannot claim this is current, judged at the moment the pace was taken.

        Two separate ways a reading can be useless, and the second is the one that bites.

        Age is the obvious one: Amazon evaluates usage periodically, so a snapshot can be an hour
        old. Judged against the caller's `at` rather than the wall clock, so the same reading does
        not become usable or unusable depending on when the question is asked.

        The budget *day* is the other. `budgetUsagePercent` is usage within Amazon's current budget
        day, and that day resets at local midnight. At 00:05 a reading taken at 23:50 is fifteen
        minutes old and describes yesterday — so an age check alone would accept "100% of budget
        used" five minutes into a fresh day and raise a high-severity alert about a campaign with its
        whole budget still in front of it.
        """
        age = self.age(at)
        if age is None:
            return True
        if age > STALE_AFTER:
            return True
        return day_started is not None and self.as_of < day_started.astimezone(timezone.utc)

    @property
    def spent(self) -> float | None:
        if self.budget is None or self.used_pct is None:
            return None
        return round(self.budget * self.used_pct / 100.0, 2)


@dataclass
class Pace:
    """What the account's budgets look like at one moment in the marketplace's own day."""

    profile_id: str
    at: datetime
    day: date
    elapsed: float                    # share of the local day gone, 0.0 to 1.0
    readings: list[Reading]
    errors: list[dict[str, Any]]

    day_started: datetime | None = None   # local midnight that opened this budget day

    @property
    def usable(self) -> list[Reading]:
        """Readings current enough, as of the moment this pace was taken, to decide anything on."""
        return [r for r in self.readings if r.used_pct is not None
                and not r.is_stale(self.at, day_started=self.day_started)]

    @property
    def stale(self) -> list[Reading]:
        """Readings Amazon gave us that are too old, or belong to yesterday's budget day."""
        return [r for r in self.readings if r.used_pct is not None
                and r.is_stale(self.at, day_started=self.day_started)]


def _parse_timestamp(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        log.warning("budget usage: cannot read timestamp %r", raw)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def elapsed_fraction(profile: PpcProfile, at: datetime | None = None) -> float:
    """How much of today has gone, in the marketplace's clock.

    Budget pace only means anything against the clock Amazon resets on: 80% of budget spent is
    healthy at 8pm and alarming at 9am, and the same UTC instant is both depending on the country.
    """
    now = at or metrics.now_for(profile)
    if now.tzinfo is None:
        now = now.replace(tzinfo=metrics.zone_for(profile))
    local = now.astimezone(metrics.zone_for(profile))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = (midnight + timedelta(days=1, hours=12)).replace(hour=0, minute=0, second=0,
                                                                microsecond=0)
    # Both ends are converted to UTC before subtracting, and that is the whole point. Python
    # subtracts two aware datetimes that share a tzinfo in *wall-clock* terms, ignoring the zone —
    # so `local - midnight` would say twelve hours at noon on a 23-hour day, and the ratio would come
    # out as plain hour/24 however carefully the divisor was computed. Budgets spend in real time,
    # not in clock time: on a spring-forward day, eleven of the day's twenty-three hours have gone by
    # noon, and a campaign is further through its budget than the clock suggests.
    start = midnight.astimezone(timezone.utc)
    end = tomorrow.astimezone(timezone.utc)
    length = (end - start).total_seconds() or 86400.0
    return max(0.0, min(1.0, (local.astimezone(timezone.utc) - start).total_seconds() / length))


def _campaigns(session: Session, profile: PpcProfile) -> list[PpcCampaign]:
    """Campaigns worth asking about: live, not archived, and actually carrying a budget."""
    rows = session.scalars(
        select(PpcCampaign).where(PpcCampaign.profile_id == profile.id,
                                  PpcCampaign.ad_product == AD_PRODUCT,
                                  PpcCampaign.state == "enabled",
                                  PpcCampaign.archived_at.is_(None),
                                  PpcCampaign.daily_budget.isnot(None))
        .order_by(PpcCampaign.daily_budget.desc())).all()
    return list(rows)


def poll(session: Session, profile: PpcProfile, *, client=None, store: bool = True,
         at: datetime | None = None) -> Pace:
    """Ask Amazon how much of today's budget each live campaign has spent.

    Campaigns are asked about in descending budget order, so if the account is large enough that a
    later batch fails we have already learned about the budgets that matter most.
    """
    client = client or api.client()
    campaigns = _campaigns(session, profile)
    labels = {c.amazon_campaign_id: c.name or c.amazon_campaign_id for c in campaigns}
    budgets = {c.amazon_campaign_id: float(c.daily_budget) for c in campaigns
               if c.daily_budget is not None}
    ids = [c.amazon_campaign_id for c in campaigns]

    readings: list[Reading] = []
    errors: list[dict[str, Any]] = []
    for start in range(0, len(ids), MAX_IDS):
        batch = ids[start:start + MAX_IDS]
        if not batch:            # unreachable by the range, but the endpoint 422s on an empty list
            continue
        try:
            response = client.post(PATH, json={"campaignIds": batch},
                                   profile_id=profile.amazon_profile_id, content_type=MEDIA,
                                   accept=MEDIA, operation=OPERATION)
        except AdsAuthError:
            # Authorisation is an account-wide problem, not a batch problem. It belongs to the job's
            # own handler, which notices once and stops asking.
            raise
        except AdsError as exc:
            # One failed batch, not a failed hour. The descending-budget ordering above exists
            # precisely so that what we already hold is the part worth holding — and an exception
            # escaping here would have thrown it away before `record` was ever reached, which is the
            # opposite of what that ordering was for.
            log.warning("budget usage: batch of %d failed: %s", len(batch), exc)
            errors.append({"amazon_campaign_id": None, "label": f"{len(batch)} campaigns",
                           "code": "batch_failed", "details": str(exc)[:300]})
            continue
        body = response if isinstance(response, dict) else {}
        for item in body.get("success") or []:
            campaign_id = str((item or {}).get("campaignId") or "")
            if not campaign_id:
                continue
            # Prefer Amazon's own budget figure; fall back to our synced one when it omits it.
            budget = _number((item or {}).get("budget"))
            readings.append(Reading(
                amazon_campaign_id=campaign_id,
                label=labels.get(campaign_id, campaign_id),
                budget=budget if budget is not None else budgets.get(campaign_id),
                used_pct=_number((item or {}).get("budgetUsagePercent")),
                as_of=_parse_timestamp((item or {}).get("usageUpdatedTimestamp")),
                currency=profile.currency or "USD",
            ))
        for item in body.get("error") or []:
            campaign_id = str((item or {}).get("campaignId") or "")
            errors.append({"amazon_campaign_id": campaign_id,
                           "label": labels.get(campaign_id, campaign_id),
                           "code": (item or {}).get("code"),
                           "details": (item or {}).get("details")})

    # `at` is stored in the marketplace's own zone, not the caller's. Everything downstream reads
    # `pace.at.hour` — the hour a reading is filed under, the hour quoted in the alert — and a
    # caller handing in a UTC moment would otherwise file an 11am reading as 6pm.
    now = at or metrics.now_for(profile)
    local = now.astimezone(metrics.zone_for(profile))
    pace = Pace(profile_id=profile.amazon_profile_id, at=local, day=local.date(),
                elapsed=elapsed_fraction(profile, local), readings=readings, errors=errors,
                day_started=local.replace(hour=0, minute=0, second=0, microsecond=0))
    if errors:
        log.warning("budget usage: %d of %d campaigns came back as errors", len(errors), len(ids))
    if store:
        record(session, profile, pace)
    return pace


def record(session: Session, profile: PpcProfile, pace: Pace) -> int:
    """Keep each usable reading on the hour it describes, so a day's shape survives the day.

    Only `budget_consumed_pct` is written here. The metric columns belong to Marketing Stream, and
    an upsert that touched them would overwrite real hourly spend with zeros.
    """
    # `pace.usable`, not `pace.readings`. A stale reading rejected for raising an alert must not be
    # written down either: the row is attributed to *this* hour of *this* day, and `views.budgets_now`
    # reads it back with no way to know it describes an hour and a half ago — or, across local
    # midnight, yesterday's budget day. Storing it would make the view assert exactly the
    # out-of-budget claim `exhausted_early` had just declined to make.
    hour = pace.at.hour
    rows = [{"amazon_profile_id": profile.amazon_profile_id, "ad_product": AD_PRODUCT,
             "amazon_campaign_id": r.amazon_campaign_id,
             "date": pace.day, "hour": hour, "weekday": pace.day.weekday(),
             "budget_consumed_pct": round(r.used_pct, 2),
             "currency": r.currency}
            for r in pace.usable]
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), metrics.INSERT_BATCH):
        batch = rows[start:start + metrics.INSERT_BATCH]
        statement = insert(PpcHourlyMetric).values(batch)
        session.execute(statement.on_conflict_do_update(
            index_elements=["amazon_profile_id", "ad_product", "amazon_campaign_id", "date",
                            "hour"],
            set_={"budget_consumed_pct": statement.excluded.budget_consumed_pct}))
        written += len(batch)
    session.flush()
    return written


def exhausted_early(pace: Pace, *, used_pct: float | None = None,
                    before: float | None = None) -> list[Reading]:
    """Campaigns that have spent their day's budget while there is still day left to sell in."""
    settings = get_settings()
    threshold = settings.ppc_budget_exhausted_pct if used_pct is None else used_pct
    cutoff = settings.ppc_budget_early_fraction if before is None else before
    if pace.elapsed >= cutoff:
        return []
    return sorted((r for r in pace.usable if (r.used_pct or 0) >= threshold),
                  key=lambda r: -(r.budget or 0))


def summary(pace: Pace) -> str:
    """One sentence about the account's budgets, in the owner's register."""
    usable = pace.usable
    if not usable:
        if pace.stale:
            return (f"I have readings for {len(pace.stale)} campaign(s) but they're too old to quote "
                    "— Amazon only re-evaluates budget usage every so often. I'll have fresher ones "
                    "within a couple of hours.")
        if pace.errors:
            return "Amazon wouldn't tell me how the budgets are doing right now."
        return "No live campaigns with a budget to report on."
    clock = metrics.clock_of(pace.at.hour)
    out = exhausted_early(pace)
    if out:
        names = ", ".join(r.label for r in out[:3])
        tail = f" and {len(out) - 3} more" if len(out) > 3 else ""
        return f"{len(out)} campaign(s) are out of budget already at {clock}: {names}{tail}."
    spent = sum(r.spent or 0 for r in usable)
    budget = sum(r.budget or 0 for r in usable)
    share = f"{spent / budget * 100:.0f}%" if budget else "—"
    return (f"At {clock}, {share} of today's budget is spent across {len(usable)} campaign(s) "
            f"and {pace.elapsed * 100:.0f}% of the day has gone.")
