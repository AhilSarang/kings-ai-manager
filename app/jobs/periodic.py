"""Periodic housekeeping registered with the scheduler."""

from __future__ import annotations

import logging

from app.db.session import session_scope
from app.jobs.scheduler import periodic
from app.services import approvals

log = logging.getLogger(__name__)


@periodic("expire_approvals", "interval", minutes=15)
def expire_approvals() -> None:
    with session_scope() as s:
        expired = approvals.expire_due(s)
    if expired:
        log.info("approvals expired", extra={"refs": [a.ref for a in expired]})


@periodic("daily_summary_check", "interval", minutes=1)
def daily_summary_check() -> None:
    from app.services import daily

    with session_scope() as s:
        daily.maybe_start_daily_summary(s)


@periodic("due_reminders", "interval", minutes=15)
def due_reminders() -> None:
    from app.services import daily

    with session_scope() as s:
        n = daily.send_due_reminders(s)
    if n:
        log.info("due reminders sent", extra={"count": n})


@periodic("release_quiet_holds", "interval", minutes=5)
def release_quiet_holds() -> None:
    from app.services import daily

    with session_scope() as s:
        daily.release_quiet_hour_holds(s)


@periodic("amazon_sync_listings_periodic", "interval", minutes=60)
def amazon_sync_listings_periodic() -> None:
    """Re-read our listings from Amazon so status, price and issues stay current."""
    from app.amazon import accounts
    from app.jobs import queue
    from app.jobs.handlers.amazon import amazon_paused

    with session_scope() as s:
        if amazon_paused(s) or accounts.get_primary(s) is None:
            return
        queue.enqueue(s, "amazon_sync_listings", {}, idempotency_key="amazon_sync_listings", coalesce=True)
        s.commit()


@periodic("amazon_inventory_periodic", "interval", hours=2)
def amazon_inventory_periodic() -> None:
    from app.amazon import accounts
    from app.jobs import queue
    from app.jobs.handlers.amazon import amazon_paused

    with session_scope() as s:
        if amazon_paused(s) or accounts.get_primary(s) is None:
            return
        queue.enqueue(s, "amazon_inventory_snapshot", {}, idempotency_key="amazon_inventory_snapshot", coalesce=True)
        s.commit()


@periodic("amazon_schema_refresh_weekly", "cron", day_of_week="sun", hour=5, minute=20)
def amazon_schema_refresh_weekly() -> None:
    from app.jobs import queue

    with session_scope() as s:
        queue.enqueue(s, "amazon_refresh_schemas", {}, idempotency_key="amazon_refresh_schemas", coalesce=True)
        s.commit()


@periodic("amazon_sales_daily", "cron", hour=6, minute=0)
def amazon_sales_daily() -> None:
    """Yesterday's sales, plus the previous days Amazon may have revised."""
    from app.amazon import accounts
    from app.jobs import queue
    from app.jobs.handlers.amazon import amazon_paused
    from app.services.common import utcnow

    with session_scope() as s:
        if amazon_paused(s) or accounts.get_primary(s) is None:
            return
        queue.enqueue(s, "amazon_collect_sales", {},
                      idempotency_key=f"amazon_sales:{utcnow():%Y-%m-%d}")
        s.commit()


@periodic("amazon_health_reports", "interval", hours=6)
def amazon_health_reports() -> None:
    from app.amazon import accounts
    from app.jobs import queue
    from app.jobs.handlers.amazon import amazon_paused

    with session_scope() as s:
        if amazon_paused(s) or accounts.get_primary(s) is None:
            return
        queue.enqueue(s, "amazon_collect_health", {}, idempotency_key="amazon_collect_health", coalesce=True)
        s.commit()


@periodic("fba_shipment_sync", "interval", hours=6)
def fba_shipment_sync() -> None:
    """Status and received quantities for every open inbound shipment."""
    from app.jobs import queue
    from app.jobs.handlers.amazon import amazon_paused

    with session_scope() as s:
        if amazon_paused(s):
            return
        queue.enqueue(s, "fba_sync_status", {}, idempotency_key="fba_sync_status", coalesce=True)
        s.commit()


@periodic("gmail_poll", "interval", minutes=5)
def gmail_poll() -> None:
    """Fallback for missed pushes: sync every active mailbox every 5 minutes."""
    from sqlalchemy import select

    from app.db.models import EmailAccount
    from app.email.sync import email_processing_paused, enqueue_sync, enqueue_triage

    with session_scope() as s:
        if email_processing_paused(s):
            return
        for account in s.execute(select(EmailAccount).where(EmailAccount.status == "active")).scalars():
            enqueue_sync(s, account.id)
        enqueue_triage(s)  # picks up anything left pending (e.g. after a pause)


@periodic("gmail_watch_renew", "cron", hour=4, minute=10)
def gmail_watch_renew_daily() -> None:
    from sqlalchemy import select

    from app.db.models import EmailAccount
    from app.jobs import queue
    from app.services.common import utcnow

    with session_scope() as s:
        for account in s.execute(select(EmailAccount).where(EmailAccount.status == "active")).scalars():
            queue.enqueue(s, "gmail_watch_renew", {"account_id": str(account.id)},
                          idempotency_key=f"gmail_watch:{account.id}:{utcnow():%Y%m%d}", priority=50)


# ---------------- PPC (Phase 4) ----------------
def _ppc_ready(session) -> bool:
    """Only schedule PPC work once Ads is connected, a profile exists, and nothing is paused."""
    from app.jobs.handlers.ppc import ads_configured, ppc_paused
    from app.ppc import profiles

    return ads_configured() and not ppc_paused(session) and bool(profiles.active(session))


@periodic("ppc_profile_discovery", "cron", hour=5, minute=40)
def ppc_profile_discovery() -> None:
    """Notice a new advertising profile (Canada, say) without waiting to be told about it."""
    from app.jobs import queue
    from app.jobs.handlers.ppc import ads_configured, ppc_paused

    with session_scope() as s:
        if ppc_paused(s) or not ads_configured():
            return
        queue.enqueue(s, "ppc_sync_profiles", {}, idempotency_key="ppc_sync_profiles", coalesce=True)
        s.commit()


@periodic("ppc_structure_sync", "interval", hours=6)
def ppc_structure_sync() -> None:
    """Campaigns, ad groups, ads, targets and negatives, so the metrics have something to hang on."""
    from app.jobs import queue

    with session_scope() as s:
        if not _ppc_ready(s):
            return
        queue.enqueue(s, "ppc_sync_structure", {}, idempotency_key="ppc_sync_structure", coalesce=True)
        s.commit()


@periodic("ppc_daily_load", "cron", hour=7, minute=30)
def ppc_daily_load() -> None:
    """Re-read the last fourteen days. Amazon's numbers for a day keep changing, so we overwrite."""
    from app.jobs import queue
    from app.services.common import utcnow

    with session_scope() as s:
        if not _ppc_ready(s):
            return
        # Same key as the Monday deep load: on Mondays that one is already queued and wins, so the
        # account is never asked for the same report type twice in a day.
        queue.enqueue(s, "ppc_load_reports", {}, idempotency_key=f"ppc_load:{utcnow():%Y-%m-%d}")
        s.commit()


@periodic("ppc_deep_load", "cron", day_of_week="mon", hour=6, minute=30)
def ppc_deep_load() -> None:
    """Once a week, reach back over the whole restatement horizon.

    Conversions restate up to 28 days after the conversion event, so a fortnight of re-reading isn't
    enough to catch every late correction — but 42 days is two report requests, which is more than
    Amazon wants every day. Weekly is the compromise, and the restatements it finds are logged.
    """
    from app.jobs import queue
    from app.ppc import metrics
    from app.services.common import utcnow

    with session_scope() as s:
        if not _ppc_ready(s):
            return
        queue.enqueue(s, "ppc_load_reports", {"days": metrics.DEEP_RESTATEMENT_DAYS},
                      idempotency_key=f"ppc_load:{utcnow():%Y-%m-%d}")
        s.commit()


@periodic("ppc_economics_daily", "cron", hour=9, minute=15)
def ppc_economics_daily() -> None:
    """Margins and target ACOS, once a day, after the listing sync has fresh prices."""
    from app.jobs import queue
    from app.services.common import utcnow

    with session_scope() as s:
        if not _ppc_ready(s):
            return
        queue.enqueue(s, "ppc_economics", {}, idempotency_key=f"ppc_economics:{utcnow():%Y-%m-%d}")
        s.commit()


@periodic("ppc_daily_analysis", "cron", hour=10, minute=0)
def ppc_daily_analysis() -> None:
    """One analysis cycle a day, after the metrics load and the economics refresh."""
    from app.jobs import queue
    from app.services.common import utcnow

    with session_scope() as s:
        if not _ppc_ready(s):
            return
        queue.enqueue(s, "ppc_analyse", {}, idempotency_key=f"ppc_analyse:{utcnow():%Y-%m-%d}",
                      uses_llm=False)
        s.commit()


@periodic("ppc_expire_recommendations", "interval", hours=6)
def ppc_expire_recommendations() -> None:
    """Recommendations nobody answered stop being proposals, but stay as a record."""
    from app.ppc import recommendations

    with session_scope() as s:
        expired = recommendations.expire_due(s)
        if expired:
            s.commit()
            log.info("ppc recommendations expired", extra={"refs": [r.ref for r in expired]})


@periodic("ppc_measure_outcomes_daily", "cron", hour=11, minute=0)
def ppc_measure_outcomes_daily() -> None:
    """After the day's data has loaded, work out what the changes we made actually did."""
    from app.jobs import queue
    from app.services.common import utcnow

    with session_scope() as s:
        if not _ppc_ready(s):
            return
        queue.enqueue(s, "ppc_measure_outcomes", {},
                      idempotency_key=f"ppc_outcomes:{utcnow():%Y-%m-%d}")
        s.commit()


@periodic("ppc_watch_experiments", "interval", hours=6)
def ppc_watch_experiments_periodic() -> None:
    """Guardrails every six hours: the point of a limit is to stop the damage, not to note it."""
    from app.jobs import queue

    with session_scope() as s:
        if not _ppc_ready(s):
            return
        queue.enqueue(s, "ppc_watch_experiments", {}, idempotency_key="ppc_watch_experiments",
                      coalesce=True)
        s.commit()


@periodic("ppc_playbook_review_weekly", "cron", day_of_week="mon", hour=12, minute=0)
def ppc_playbook_review_weekly() -> None:
    """Once a week: score the learned rules, retire the ones that stopped working, propose new ones."""
    from app.jobs import queue
    from app.services.common import utcnow

    with session_scope() as s:
        if not _ppc_ready(s):
            return
        queue.enqueue(s, "ppc_playbook_review", {},
                      idempotency_key=f"ppc_playbook:{utcnow():%Y-%W}")
        s.commit()


@periodic("ppc_research_weekly", "cron", day_of_week="sun", hour=16, minute=0)
def ppc_research_weekly() -> None:
    """Read the sources once a week, the day before the strategy report."""
    from app.jobs import queue
    from app.services.common import utcnow

    with session_scope() as s:
        if not _ppc_ready(s):
            return
        queue.enqueue(s, "ppc_research_scan", {}, idempotency_key=f"ppc_research:{utcnow():%Y-%W}",
                      uses_llm=True)
        s.commit()


@periodic("ppc_weekly_report", "cron", day_of_week="mon", hour=8, minute=0)
def ppc_weekly_report_periodic() -> None:
    """08:00 Monday, as `ppc_report_time` says."""
    from app.jobs import queue
    from app.services.common import utcnow

    with session_scope() as s:
        if not _ppc_ready(s):
            return
        queue.enqueue(s, "ppc_weekly_report", {},
                      idempotency_key=f"ppc_weekly_report:{utcnow():%Y-%W}")
        s.commit()


@periodic("ppc_budget_pace", "interval", hours=2)
def ppc_budget_pace_periodic() -> None:
    """Every two hours through the day.

    The budget-usage reading is a snapshot Amazon overwrites, so an hour nobody asked about is gone.
    Two-hourly is a compromise: fine enough to catch a campaign going dark before the day is over,
    coarse enough that the quota is spent on data collection rather than on polling.
    """
    from app.jobs import queue
    from app.services.common import utcnow

    with session_scope() as s:
        if not _ppc_ready(s):
            return
        queue.enqueue(s, "ppc_budget_pace", {},
                      idempotency_key=f"ppc_budget_pace:{utcnow():%Y-%m-%dT%H}")
        s.commit()


@periodic("ppc_alert_sweep", "interval", hours=3)
def ppc_alert_sweep_periodic() -> None:
    """Three-hourly, which is as often as there is anything new to say.

    Not hourly: the daily figures this mostly reads change once a day, and the intraday half is
    already covered by `ppc_budget_pace` writing its readings down for the sweep to find.
    """
    from app.jobs import queue
    from app.services.common import utcnow

    with session_scope() as s:
        if not _ppc_ready(s):
            return
        queue.enqueue(s, "ppc_alert_sweep", {},
                      idempotency_key=f"ppc_alert_sweep:{utcnow():%Y-%m-%dT%H}")
        s.commit()


@periodic("ppc_stream_poll", "interval", minutes=15)
def ppc_stream_poll_periodic() -> None:
    """Quarter-hourly, and a no-op until there is a Marketing Stream subscription to drain.

    Checked here rather than inside the job so that a dormant stream costs nothing at all — not even
    a queued row — in the months before the subscription exists.
    """
    from app.jobs import queue
    from app.ppc import stream
    from app.services.common import utcnow

    if not stream.configured():
        return
    with session_scope() as s:
        if _ppc_paused_only(s):
            return
        queue.enqueue(s, "ppc_stream_poll", {},
                      idempotency_key=f"ppc_stream_poll:{utcnow():%Y-%m-%dT%H:%M}")
        s.commit()


def _ppc_paused_only(session) -> bool:
    """Stream ingestion needs no Ads credentials — the data arrives from AWS, not from the API.

    So this is deliberately a weaker gate than `_ppc_ready`: hourly data should keep landing even
    while the Ads application is being re-authorised, because losing it loses it for good.
    """
    from app.jobs.handlers.ppc import ppc_paused

    return ppc_paused(session)
