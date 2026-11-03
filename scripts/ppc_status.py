"""What the PPC agent currently knows: profiles, account shape, how much history, what restated.

    docker compose exec worker python scripts/ppc_status.py

Reads only. Run it after the backfill to see how much history Amazon actually gave us.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import (PpcAdGroup, PpcCampaign, PpcMetricsDaily, PpcNegative, PpcPortfolio,  # noqa: E402
                           PpcProductAd, PpcProfile, PpcSearchTerm, PpcTarget)
from app.db.session import session_scope  # noqa: E402
from app.ppc import backfill, metrics, profiles, reports  # noqa: E402


def main() -> None:
    with session_scope() as s:
        rows = list(s.execute(select(PpcProfile).order_by(PpcProfile.created_at)).scalars())
        if not rows:
            print("No advertising profiles yet. Run the ppc_sync_profiles job, or set ADS_PROFILE_ID_US.")
            return
        for profile in rows:
            mark = {"active": "✅", "paused": "⏸️ ", "auth_error": "⛔"}.get(profile.status, "  ")
            print(f"{mark} {profile.country_code} profile {profile.amazon_profile_id} "
                  f"({profile.currency}, {profile.timezone})  writes: {profile.write_mode}")
            if profile.status_reason:
                print(f"     {profile.status_reason}")
            if profile.status != "active":
                continue
            print(f"     today there: {metrics.today_for(profile)}")

            counts = []
            for label, model in (("portfolios", PpcPortfolio), ("campaigns", PpcCampaign),
                                 ("ad groups", PpcAdGroup), ("ads", PpcProductAd),
                                 ("targets", PpcTarget), ("negatives", PpcNegative)):
                live = s.execute(select(func.count()).select_from(model).where(
                    model.profile_id == profile.id, model.state != "archived")).scalar_one()
                counts.append(f"{live} {label}")
            print("     " + ", ".join(counts))
            print(f"     last structure sync: {profile.last_synced_at or 'never'}")

            coverage = backfill.coverage(s, profile)
            for label in ("metrics", "search_terms"):
                got = coverage[label]
                print(f"     {label}: {got['rows']} rows, {got['days']} days "
                      f"({got['first'] or '—'} → {got['last'] or '—'})")
            print(f"     Amazon keeps {reports.RETENTION_DAYS} days "
                  f"({reports.SEARCH_TERM_RETENTION_DAYS} for search terms); everything older than "
                  "that only exists here.")

            today = metrics.today_for(profile)
            provisional = s.execute(select(func.count()).select_from(PpcMetricsDaily).where(
                PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
                PpcMetricsDaily.is_provisional.is_(True))).scalar_one()
            settled = s.execute(select(func.count()).select_from(PpcMetricsDaily).where(
                PpcMetricsDaily.amazon_profile_id == profile.amazon_profile_id,
                PpcMetricsDaily.date <= metrics.settled_before(today))).scalar_one()
            print(f"     {provisional} rows still provisional (not safe to act on), "
                  f"{settled} settled (safe to conclude from)")

            recent = metrics.restatements(s, profile, limit=5)
            print(f"     restatements logged: {len(metrics.restatements(s, profile, limit=10000))}")
            for change in recent:
                print(f"       {change.date} {change.entity_type} {change.amazon_entity_id}: "
                      f"sales {change.old_sales} → {change.new_sales}, "
                      f"orders {change.old_orders} → {change.new_orders}")

            stale = s.execute(select(func.count()).select_from(PpcSearchTerm).where(
                PpcSearchTerm.amazon_profile_id == profile.amazon_profile_id)).scalar_one()
            print(f"     search term rows: {stale}")
            print()

        print("Write mode is read only until it is switched with a PIN. "
              "Nothing in Phase 4 changes the account without an approval.")


if __name__ == "__main__":
    main()
