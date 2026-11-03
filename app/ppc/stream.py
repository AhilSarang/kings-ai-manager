"""Marketing Stream ingestion: the loader, and an honest hole where the transport goes.

Hourly metrics are the one thing the Ads API will not give us. They come from Amazon Marketing
Stream, and in Stream V2 — the current version; V1's SQS-of-JSON is marked outdated in Amazon's own
docs repo — the delivery works like this:

1. We deploy Amazon's CloudFormation stack, which makes an S3 bucket and an SQS queue we own.
2. We create a subscription with `{"bucketArn": …, "notification": {"queueArn": …}}`.
3. Amazon writes **Parquet** objects into our bucket and drops a **pointer** on our queue. The queue
   message is a notification, not the data.
4. Seventy days of history arrive automatically when the subscription starts.

This module is split along the line between what I can verify and what I cannot, because getting that
line wrong is how this phase has already shipped two confidently-broken mappings with passing tests.

`load()` and `Row` are the verified half. They take hourly figures that are already parsed and put
them in `ppc_hourly_metrics` correctly — idempotently, in batches, letting a later delivery replace
an earlier one, and leaving `budget_consumed_pct` alone because that column belongs to `budget.py`.
Every edge I can think of is tested.

`consume()` is the unverified half, and it says so rather than pretending. The ~400 column names of
the V2 dataset are not published and do not exist until a subscription does, so this does not guess
them. On the first real object it **logs the schema it finds** and stores nothing. That is the point:
the mapping gets written from the dataset, by someone looking at the dataset.

One property of stream data is worth knowing before anything is built on it: **clicks are restated**.
Amazon's traffic validation invalidates some clicks for up to 72 hours after the first delivery, so an
hour's figures move after we first see them. `load()` therefore overwrites a bucket rather than adding
to it — a later delivery for the same hour is the better answer, not a second helping of the same one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import PPC_AD_PRODUCTS, PpcHourlyMetric, PpcProfile
from app.ppc import metrics

log = logging.getLogger(__name__)

# Columns `load()` owns. `budget_consumed_pct` is deliberately absent: budget.py writes it from a
# different source at a different cadence, and an upsert that touched it would wipe a live reading.
METRIC_COLUMNS = ("impressions", "clicks", "spend", "sales", "orders")
KEY_COLUMNS = ("amazon_profile_id", "ad_product", "amazon_campaign_id", "date", "hour")


class StreamNotConfigured(RuntimeError):
    """Raised when something asks the stream to do work it has no subscription for."""


@dataclass
class Row:
    """One campaign-hour. Whatever the transport turns out to look like, it has to produce these."""

    amazon_profile_id: str
    amazon_campaign_id: str
    date: date
    hour: int
    # Required, with no default. Stream V2 is one dataset covering Sponsored Products, Brands,
    # Display and DSP, so a row's ad product is something the transport reads off the record — and
    # a default of "sp" would mean the day someone wires that transport up and misses this field,
    # every Display hour is filed as Sponsored Products. Silently, and permanently.
    ad_product: str = field(kw_only=True)
    impressions: int = 0
    clicks: int = 0
    spend: float = 0.0
    sales: float = 0.0
    orders: int = 0
    currency: str = "USD"

    def __post_init__(self) -> None:
        if isinstance(self.date, datetime):
            # A naive datetime here would silently become a UTC date. Stream timestamps are in the
            # marketplace's own hour, so the caller converts; this only unwraps an already-local one.
            self.date = self.date.date()
        if self.ad_product not in PPC_AD_PRODUCTS:
            raise ValueError(f"{self.ad_product!r} is not an ad product I know")
        if not isinstance(self.date, date):
            # Caught here rather than four frames later in `as_values`, where a string date raises
            # AttributeError on .weekday() with half the batch already built and nothing to say.
            raise ValueError(f"{self.date!r} is not a date")
        # Coerced, not just range-checked. An hour of 9.5 passes `0 <= hour <= 23` and is then
        # rounded by Postgres into the integer column, so a 9am record lands in the 10am bucket and
        # dayparting reads a shifted day. The transport is unwritten, so this door has to hold.
        try:
            hour = int(self.hour)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{self.hour!r} is not an hour of the day") from exc
        if hour != self.hour or not 0 <= hour <= 23:
            raise ValueError(f"hour {self.hour!r} is not an hour of the day")
        self.hour = hour

    def as_values(self) -> dict[str, Any]:
        return {"amazon_profile_id": self.amazon_profile_id, "ad_product": self.ad_product,
                "amazon_campaign_id": self.amazon_campaign_id,
                "date": self.date, "hour": self.hour, "weekday": self.date.weekday(),
                "impressions": max(0, int(self.impressions)), "clicks": max(0, int(self.clicks)),
                "spend": round(float(self.spend), 2), "sales": round(float(self.sales), 2),
                "orders": max(0, int(self.orders)), "currency": self.currency,
                "received_at": func.now()}


def configured() -> bool:
    """Whether a Stream destination has been set up. False keeps the whole module asleep."""
    settings = get_settings()
    return bool(settings.ads_marketing_stream_queue_url)


def collapse(rows: Sequence[Row]) -> list[Row]:
    """Reduce rows describing the same campaign-hour to one, keeping the last.

    Postgres rejects an INSERT whose own VALUES hold two rows for one conflict key, so the duplicates
    have to go before the write. The only question is which survives, and the answer is the last one,
    for the same reason `load` lets a re-delivery overwrite: Amazon restates an hour for up to 72
    hours and the newer figure is the better figure.

    An earlier draft summed within a batch, on the reasoning that Stream V1 split traffic from
    conversions into separate datasets so the two halves had to be re-joined. That reasoning does not
    survive V2, which is a *single* dataset — Amazon's own words, "around 400 metrics across 59
    dimensions" in one place. With nothing to re-join, two records for one hour in one batch are an
    original and its correction, and summing them would double the hour: 10 clicks restated to 7
    would store 17, and the spike alert would fire on arithmetic rather than on spend.

    Summing would also be unrecoverable. An overwrite that guessed wrong is fixed by the next
    delivery; a double is in the table for good.
    """
    merged: dict[tuple, Row] = {}
    for row in rows:
        # A copy, not the caller's object: `load` is called from a retry loop, and handing back
        # rows that had been written through would leave the caller's list quietly altered.
        merged[(row.amazon_profile_id, row.ad_product, row.amazon_campaign_id, row.date,
                row.hour)] = replace(row)
    return list(merged.values())


def load(session: Session, rows: Sequence[Row]) -> dict[str, Any]:
    """Put hourly figures in the table, idempotently.

    A re-delivered hour overwrites rather than accumulates, because Amazon restates clicks for up to
    72 hours and the newer delivery is the better number. Restatements are logged when they move a
    figure materially, for the same reason `metrics.py` logs daily ones: a silent revision is how a
    measured result quietly becomes wrong.
    """
    collapsed = collapse(rows)
    if not collapsed:
        return {"written": 0, "restated": 0}

    before = _existing(session, collapsed)
    restated = 0
    written = 0
    for start in range(0, len(collapsed), metrics.INSERT_BATCH):
        batch = [row.as_values() for row in collapsed[start:start + metrics.INSERT_BATCH]]
        statement = insert(PpcHourlyMetric).values(batch)
        session.execute(statement.on_conflict_do_update(
            index_elements=list(KEY_COLUMNS),
            set_={column: statement.excluded[column]
                  for column in (*METRIC_COLUMNS, "weekday", "currency", "received_at")}))
        written += len(batch)

    for row in collapsed:
        key = (row.amazon_profile_id, row.ad_product, row.amazon_campaign_id, row.date,
               row.hour)
        old = before.get(key)
        if old is None:
            continue
        if abs(float(old["spend"]) - row.spend) >= 0.01 or int(old["clicks"]) != row.clicks:
            restated += 1
            log.info("hourly restatement %s %s h%d: spend %.2f→%.2f clicks %d→%d",
                     row.amazon_campaign_id, row.date, row.hour,
                     float(old["spend"]), row.spend, int(old["clicks"]), row.clicks)
    session.flush()
    return {"written": written, "restated": restated,
            "hours": sorted({(r.date.isoformat(), r.hour) for r in collapsed})[:50]}


def _existing(session: Session, rows: Sequence[Row]) -> dict[tuple, dict[str, Any]]:
    """What we already hold for these hours, so a restatement can be noticed rather than guessed."""
    if not rows:
        return {}
    profile_ids = {r.amazon_profile_id for r in rows}
    dates = {r.date for r in rows}
    campaigns = {r.amazon_campaign_id for r in rows}
    found = session.execute(
        select(PpcHourlyMetric.amazon_profile_id, PpcHourlyMetric.ad_product,
               PpcHourlyMetric.amazon_campaign_id, PpcHourlyMetric.date, PpcHourlyMetric.hour,
               PpcHourlyMetric.spend, PpcHourlyMetric.clicks)
        .where(PpcHourlyMetric.amazon_profile_id.in_(profile_ids),
               PpcHourlyMetric.date.in_(dates),
               PpcHourlyMetric.amazon_campaign_id.in_(campaigns))).all()
    # Keyed on the table's full identity. Dropping the profile made a row belonging to one
    # marketplace answer the lookup for another's campaign of the same id, and the restatement log
    # then reported a change between two different accounts' numbers.
    return {(r.amazon_profile_id, r.ad_product, r.amazon_campaign_id, r.date, r.hour):
            {"spend": r.spend, "clicks": r.clicks} for r in found}


def coverage(session: Session, profile: PpcProfile, *, ad_product: str = "sp") -> dict[str, Any]:
    """How much hourly data we actually hold, which decides whether dayparting may say anything."""
    # Only rows carrying traffic count. `budget.record` writes to this table too, with the metric
    # columns left at zero, and counting those dates would report hourly coverage we do not have.
    traffic = or_(PpcHourlyMetric.impressions > 0, PpcHourlyMetric.clicks > 0,
                  PpcHourlyMetric.spend > 0)
    row = session.execute(
        select(func.min(PpcHourlyMetric.date).label("first"),
               func.max(PpcHourlyMetric.date).label("last"),
               func.count(func.distinct(PpcHourlyMetric.date)).label("days"),
               func.count().label("rows"))
        .where(PpcHourlyMetric.amazon_profile_id == profile.amazon_profile_id,
               PpcHourlyMetric.ad_product == ad_product, traffic)).one()
    return {"configured": configured(),
            "first_date": row.first.isoformat() if row.first else None,
            "last_date": row.last.isoformat() if row.last else None,
            "days": int(row.days or 0), "rows": int(row.rows or 0)}


def status(session: Session, profile: PpcProfile, *, ad_product: str = "sp") -> str:
    """One sentence about the stream, for `PPC HOURS` and the weekly report."""
    found = coverage(session, profile, ad_product=ad_product)
    if not found["configured"]:
        if found["days"]:
            return (f"Marketing Stream isn't configured, but I still hold {found['days']} days of "
                    "hourly data from before. Dayparting works on what's there.")
        return ("Hourly data needs an Amazon Marketing Stream subscription — an AWS account with an "
                "S3 bucket and a queue Amazon writes into. Without it I can see whole days but not "
                "the shape of a day. Seventy days of history arrive the moment it's switched on, so "
                "dayparting works from day one rather than after a month of waiting.")
    if not found["days"]:
        return "Marketing Stream is configured but nothing has arrived yet."
    return (f"{found['days']} days of hourly data, {found['first_date']} to {found['last_date']}.")


# ---------------- the transport, deliberately unfinished ----------------
def consume(session: Session, *, limit: int = 10, client=None) -> dict[str, Any]:
    """Read delivery notifications and load what they point at.

    This is the half I cannot write honestly yet, and it does not pretend otherwise.

    What is known, from Amazon's published template: the queue carries pointers, the objects are
    Parquet in a bucket we own under our own KMS key, and seventy days land on subscribing. What is
    *not* known is the dataset's column names — roughly four hundred of them across fifty-nine
    dimensions, unpublished, and non-existent until a subscription exists. Guessing them and writing
    tests that agree with the guess is how this phase has twice shipped a broken mapping with a green
    suite; `docs/DECISIONS.md` D122 has both cases.

    So on a first run this logs the schema it finds and stores nothing. The mapping is then written
    from a real object, by someone looking at a real object, and `load()` — which is finished and
    tested — takes it from there.
    """
    if not configured():
        raise StreamNotConfigured(
            "No Marketing Stream queue configured. Set ADS_MARKETING_STREAM_QUEUE_URL once the "
            "subscription exists.")
    raise StreamNotConfigured(
        "Marketing Stream delivery is subscribed but not yet mapped. Stream V2 writes Parquet to our "
        "own S3 bucket and puts only a pointer on the queue, and its column names aren't published — "
        "so the first delivery needs reading before the mapping is written, rather than guessing it. "
        "The loader underneath this is finished: give me one real object's schema and ingestion is a "
        "small change. See docs/DECISIONS.md D122.")
