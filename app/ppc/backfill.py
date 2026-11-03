"""Loading as much history as Amazon still holds — which is 95 days, not twelve months.

The brief asks for a twelve-month backfill "or as far as Amazon allows". Amazon allows 95 days for
most Sponsored Products reports and 65 for search terms, and there is no other way to get older
Sponsored Products data through this API. So the honest answer is:

* backfill everything Amazon still has, on day one;
* keep it, because from now on the agent's own history is the long record;
* say plainly, in the result and to the owner, how much history it actually got.

That matters for expectations more than for code: seasonality and year-on-year comparisons cannot be
learned from advertising data until the agent has been running for a year. Phase 3's sales history
covers total sales over a longer period, which is what TACOS work leans on in the meantime.

A full backfill is about twenty-five report requests, and Amazon recommends one or two per report
type per day, so the chunks are enqueued as separate jobs spread over hours rather than fired at
once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import PpcMetricsDaily, PpcProfile, PpcSearchTerm
from app.jobs import queue
from app.ppc import metrics, profiles, reports

log = logging.getLogger(__name__)

# Amazon recommends 1–2 report requests per report type per day, so the chunks are spaced out.
SPACING_MINUTES = 12
REQUESTED_DAYS = 365          # what the brief asks for; Amazon's retention is what we get


@dataclass(frozen=True)
class Chunk:
    report: str
    start: date
    end: date

    def key(self, profile: PpcProfile) -> str:
        return f"ppc_backfill:{profile.amazon_profile_id}:{self.report}:{self.start:%Y%m%d}"

    def as_payload(self, profile: PpcProfile) -> dict[str, Any]:
        return {"profile_id": str(profile.id), "report": self.report,
                "start": self.start.isoformat(), "end": self.end.isoformat()}


def plan(profile: PpcProfile, *, days: int = REQUESTED_DAYS, today: date | None = None,
         only: list[str] | None = None) -> dict[str, Any]:
    """What can actually be loaded, and what had to be given up on.

    Oldest chunks first: if a backfill is interrupted the history that exists is contiguous from the
    far edge rather than full of holes.
    """
    today = today or metrics.today_for(profile)
    wanted_start = today - timedelta(days=days)
    chunks: list[Chunk] = []
    coverage: dict[str, Any] = {}
    for key in (only or reports.DAILY_ORDER):
        spec = reports.SPECS[key]
        trimmed = reports.clamp(spec, wanted_start, today - timedelta(days=1), today)
        if trimmed is None:
            coverage[key] = {"days": 0, "reason": "nothing inside Amazon's retention window"}
            continue
        start, end = trimmed
        coverage[key] = {"start": start.isoformat(), "end": end.isoformat(),
                         "days": (end - start).days + 1, "retention_days": spec.retention_days}
        chunks.extend(Chunk(key, s, e) for s, e in reports.chunks(start, end))
    chunks.sort(key=lambda c: (c.start, c.report))
    available = max((c["days"] for c in coverage.values()), default=0)
    return {"requested_days": days, "available_days": available, "chunks": chunks,
            "coverage": coverage, "requests": len(chunks),
            "note": (f"Amazon keeps {reports.RETENTION_DAYS} days of Sponsored Products reporting "
                     f"({reports.SEARCH_TERM_RETENTION_DAYS} for search terms), so {available} days "
                     f"is the whole history available; {days} days was asked for. Everything from "
                     "here is accumulated and kept.")}


def enqueue(session: Session, profile: PpcProfile | None = None, *, days: int = REQUESTED_DAYS,
            today: date | None = None, only: list[str] | None = None) -> dict[str, Any]:
    """Queue one job per chunk, spread out, and hand back what was planned."""
    profile = profile or profiles.require(session)
    planned = plan(profile, days=days, today=today, only=only)
    from app.services.common import utcnow

    now = utcnow()
    queued = 0
    for index, chunk in enumerate(planned["chunks"]):
        job = queue.enqueue(session, "ppc_backfill_chunk", chunk.as_payload(profile),
                            run_after=now + timedelta(minutes=SPACING_MINUTES * index),
                            # coalesce: a chunk that failed can be retried by running the
                            # backfill again, and re-loading one that succeeded is harmless.
                            idempotency_key=chunk.key(profile), coalesce=True, priority=200)
        queued += 1 if job else 0
    session.flush()
    result = {k: v for k, v in planned.items() if k != "chunks"}
    result["queued"] = queued
    log.info("ppc backfill queued", extra={"queued": queued, "requests": planned["requests"]})
    return result


def run_chunk(session: Session, payload: dict[str, Any], client=None) -> dict[str, Any]:
    """One report, one date range. Backfilled days are loaded exactly as the daily job loads them."""
    profile = profiles.get_by_id(session, payload["profile_id"])
    spec = reports.SPECS[payload["report"]]
    return reports.load(session, profile, spec, date.fromisoformat(payload["start"]),
                        date.fromisoformat(payload["end"]), client=client)


def coverage(session: Session, profile: PpcProfile) -> dict[str, Any]:
    """How much history the agent actually holds, which is what its confidence should rest on."""
    out: dict[str, Any] = {}
    for label, model, where in (
            ("metrics", PpcMetricsDaily, PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id),
            ("search_terms", PpcSearchTerm, PpcSearchTerm.amazon_profile_id == profile.amazon_profile_id)):
        first, last, rows = session.execute(select(
            func.min(model.date), func.max(model.date), func.count()).where(where)).one()
        out[label] = {"first": first.isoformat() if first else None,
                      "last": last.isoformat() if last else None, "rows": rows,
                      "days": (last - first).days + 1 if first and last else 0}
    return out
