"""PPC jobs: profile discovery, structure sync, the daily metrics load, and the backfill.

Reads only. Nothing in Phase 4 writes to Amazon Ads without an approval, and none of these jobs is
an approval path.
"""

from __future__ import annotations

import logging

from datetime import date, timedelta

from app.jobs import queue
from app.jobs.errors import PermanentError, RetryableError
from app.jobs.handlers import handler
from app.ppc import (api, apply, backfill, economics, experiments as _experiments,
                     metrics, playbook as _playbook, profiles, reports, structure)

# Imported for the side effect of registering the `ppc_start_experiment` executor,
# the same way `apply` registers `ppc_apply_change`.
_ = (_experiments, _playbook)
from app.ppc.errors import AdsAuthError, AdsError, AdsReportFailed, AdsReportInFlight, AdsRetryable
from app.ppc.profiles import PpcNotConnected
from app.config import get_settings
from app.services import settings_store
from app.services.common import utcnow

log = logging.getLogger(__name__)

SPACING_SECONDS = 20        # Amazon's reporting limits move with its queue depth; don't burst
POLL_SECONDS = 60
MAX_POLL_SECONDS = 300
MAX_POLLS = 60              # about three hours, which is how long Amazon says a report can take


def ppc_paused(session) -> bool:
    return bool(settings_store.is_paused(session) or settings_store.get(session, "ppc_paused", False))


def ads_configured() -> bool:
    """Whether the Ads application has been registered at all.

    Without this, deploying Phase 4 before the owner has an Ads API application would have him told
    that "Amazon Ads access stopped working" — about something he had not yet connected.
    """
    s = get_settings()
    return bool(s.ads_lwa_client_id and s.ads_lwa_client_secret.get_secret_value()
                and s.ads_lwa_refresh_token.get_secret_value())


def _auth_failed(session, exc: AdsAuthError) -> None:
    """One notice, then stop trying until the owner re-authorises the Ads application."""
    from app.services.notify import notify_owner

    if getattr(exc, "missing_scope", False):
        session.rollback()
        notify_owner(session, f"📊⚠️ Amazon Ads won't let this app do one thing: {exc} Everything "
                              "else keeps working.", key=f"ppc_missing_scope:{str(exc)[:40]}")
        session.commit()
        return
    session.rollback()
    for profile in profiles.active(session):
        profiles.set_status(session, profile, "auth_error", str(exc)[:300])
    notify_owner(session, f"📊⚠️ Amazon Ads access stopped working ({exc}). PPC data collection is "
                          "paused. Re-authorise the Ads application and I'll pick it back up on my "
                          "own within the day — nothing to send me.",
                 key="ppc_auth_error")
    session.commit()


def _for_each_profile(session, work, *, what: str) -> list:
    """Run `work(profile)` for every enabled marketplace.

    Not "the first active profile": that ordering is alphabetical, so the day Canada was switched on,
    every sync and every report load would quietly have moved to CA and the US account — the one with
    all the spend — would have stopped being collected at all.
    """
    results = []
    for profile in profiles.active(session):
        result = _guarded(session, lambda p=profile: work(p), what=f"{what} ({profile.country_code})")
        if result is not None:
            results.append(result)
    return results


def _guarded(session, work, *, what: str):
    """Every PPC job fails the same way: auth once, retry on Amazon's problems, stop on ours."""
    try:
        return work()
    except AdsAuthError as exc:
        _auth_failed(session, exc)
        return None
    except PpcNotConnected as exc:
        log.info("%s skipped: %s", what, exc)
        return None
    except AdsReportInFlight as exc:
        # Amazon is already generating this report. Come back to it rather than asking again.
        raise RetryableError(str(exc)) from exc
    except AdsReportFailed as exc:
        if exc.timed_out:
            raise RetryableError(str(exc)) from exc
        raise PermanentError(str(exc)) from exc
    except AdsRetryable as exc:
        raise RetryableError(str(exc)) from exc
    except AdsError as exc:
        raise PermanentError(str(exc)) from exc


@handler("ppc_sync_profiles")
def ppc_sync_profiles(session, payload, job) -> None:
    if ppc_paused(session) or not ads_configured():
        return
    stats = _guarded(session, lambda: profiles.sync(session), what="ppc profile sync")
    if stats and stats.get("recovered"):
        from app.services.notify import notify_owner

        session.commit()
        notify_owner(session, "📊 Amazon Ads access is working again — advertising data collection "
                              "has restarted.", key=None)
    if stats and stats.get("new"):
        from app.services.notify import notify_owner

        notify_owner(session, "📊 Amazon Ads has advertising profiles this app can see that aren't "
                              f"switched on yet: {', '.join(stats['new'])}. They stay off until you "
                              "turn them on.", key=f"ppc_new_profiles:{','.join(stats['new'])}")


@handler("ppc_sync_structure")
def ppc_sync_structure(session, payload, job) -> None:
    if ppc_paused(session) or not ads_configured():
        return
    for stats in _for_each_profile(session, lambda profile: structure.sync_all(session, profile),
                                   what="ppc structure sync"):
        session.commit()      # each marketplace's shape is kept even if the next one fails
        log.info("ppc structure sync", extra={k: v for k, v in stats.items() if not isinstance(v, set)})


@handler("ppc_load_reports")
def ppc_load_reports(session, payload, job) -> None:
    """Queue one job per report and date chunk. `days` widens the window for the weekly deep pull.

    This only plans the work. Each report is asked for, waited on and stored by its own short job,
    because Amazon can take up to three hours to build one: a single job doing all seven would hold a
    worker and an open transaction for that whole time, and the ten-minute stuck-job reclaim would
    declare it dead and start a second copy while the first was still running.
    """
    if ppc_paused(session):
        return
    days = int(payload.get("days") or 0) or metrics.RESTATEMENT_DAYS
    only = payload.get("only") or None
    queued = 0
    for profile in profiles.active(session):
        today = metrics.today_for(profile)
        for index, chunk in enumerate(reports.plan(profile, days=days, today=today, only=only)):
            job_queued = queue.enqueue(
                session, "ppc_report", chunk,
                run_after=utcnow() + timedelta(seconds=SPACING_SECONDS * index),
                idempotency_key=(f"ppc_report:{profile.amazon_profile_id}:{chunk['report']}:"
                                 f"{chunk['start']}:{today:%Y%m%d}"), coalesce=True, priority=150)
            queued += 1 if job_queued else 0
    session.commit()
    log.info("ppc report jobs queued", extra={"queued": queued, "days": days})


@handler("ppc_report")
def ppc_report(session, payload, job) -> None:
    """One report, one date range: ask Amazon for it, then come back for it until it is ready."""
    if ppc_paused(session) or not ads_configured():
        return
    profile = profiles.get_by_id(session, payload["profile_id"])
    spec = reports.SPECS[payload["report"]]
    start = date.fromisoformat(payload["start"])
    end = date.fromisoformat(payload["end"])
    client = api.client()
    polls = int(payload.get("polls") or 0)

    if not payload.get("report_id"):
        report_id = _guarded(session, lambda: reports.ask_for(client, spec, profile, start, end),
                             what=f"ppc {spec.key} request")
        if report_id is None:
            return
        _poll_again(session, profile, payload, report_id=report_id, polls=0)
        return

    collected = _guarded(session, lambda: reports.collect(client, spec, profile, payload["report_id"]),
                         what=f"ppc {spec.key} collect")
    if collected is None:
        if polls >= MAX_POLLS:
            raise PermanentError(
                f"Amazon is still building the {spec.key} report for {start}..{end} after "
                f"{MAX_POLLS} checks; nothing was loaded.")
        _poll_again(session, profile, payload, report_id=payload["report_id"], polls=polls + 1)
        return
    rows, report = collected
    result = reports.store(session, profile, spec, rows, report, start, end)
    session.commit()
    log.info("ppc report loaded", extra=result)


def _poll_again(session, profile, payload: dict, *, report_id: str, polls: int) -> None:
    """Hand the waiting back to the queue, with the interval growing as Amazon takes longer."""
    wait = min(POLL_SECONDS * (2 ** min(polls, 3)), MAX_POLL_SECONDS)
    queue.enqueue(session, "ppc_report", {**payload, "report_id": report_id, "polls": polls},
                  run_after=utcnow() + timedelta(seconds=wait),
                  idempotency_key=(f"ppc_report:{profile.amazon_profile_id}:{payload['report']}:"
                                   f"{payload['start']}:{report_id}:{polls}"), priority=150)
    session.commit()


@handler("ppc_economics")
def ppc_economics(session, payload, job) -> None:
    """Recompute what every ASIN can afford: price and cost from us, fees from Amazon.

    Runs after the metrics load rather than before it, because a price Amazon changed overnight is
    read by the Phase 3 listing sync, and a stale price makes every target ACOS wrong in the same
    direction.
    """
    if ppc_paused(session) or not ads_configured():
        return
    only = payload.get("asins") or None
    for stats in _for_each_profile(session,
                                   lambda profile: economics.refresh(session, profile, asins=only),
                                   what="ppc economics"):
        session.commit()
        log.info("ppc economics", extra={k: (len(v) if isinstance(v, list) else v)
                                         for k, v in stats.items()})
    if only:
        return          # one ASIN's price moved; the daily run is where the chasing happens
    for profile in profiles.active(session):
        asked = economics.ask_for_missing_costs(session, profile)
        if asked:
            session.commit()
            log.info("asked for missing unit costs", extra={"asins": asked})


@handler("ppc_analyse")
def ppc_analyse(session, payload, job) -> None:
    """The daily cycle (brief 7.1): analyse, store the batch, ask the owner.

    Economics first, then the analysers, then one approval for the whole digest. The digest is sent
    only when there is something in it — a daily message saying "nothing to change" trains people to
    stop reading, and `PPC FINDINGS` answers that question on demand.
    """
    if ppc_paused(session) or not ads_configured():
        return
    from app.ppc import analysers, recommendations
    from app.services.notify import notify_owner

    for profile in profiles.active(session):
        result = _guarded(session, lambda p=profile: analysers.run_all(session, p),
                          what=f"ppc analysis ({profile.country_code})")
        if result is None:
            continue
        start, end = analysers.aggregates.window(profile, result["window_days"])
        run = recommendations.store_run(session, profile,
                                       {**result, "window_start": start, "window_end": end})
        if not recommendations.in_batch(session, run, ("proposed",)):
            session.commit()
            log.info("ppc analysis found nothing", extra={"batch": run.ref})
            continue
        approval = recommendations.request_batch_approval(session, run)
        text = recommendations.digest(session, run, approval=approval)
        session.commit()
        notify_owner(session, text, key=f"ppc_digest:{run.ref}",
                     urgent=approval.risk_level in ("high", "critical"))
        run.digest_sent_at = utcnow()
        session.commit()
        log.info("ppc digest sent", extra={"batch": run.ref, "approval": approval.ref})


@handler("ppc_measure_outcomes")
def ppc_measure_outcomes(session, payload, job) -> None:
    """Measure what every applied change actually did, at 7, 14 and 28 days.

    Re-measures until Amazon stops restating the window, so an early read is available and only the
    settled one teaches the playbook anything (D101).
    """
    if ppc_paused(session) or not ads_configured():
        return
    from app.ppc import outcomes

    for profile in profiles.active(session):
        stats = _guarded(session, lambda p=profile: outcomes.evaluate_due(session, p),
                         what=f"ppc outcomes ({profile.country_code})")
        if stats:
            session.commit()
            log.info("ppc outcomes measured", extra={"profile": profile.amazon_profile_id, **stats})


@handler("ppc_watch_experiments")
def ppc_watch_experiments(session, payload, job) -> None:
    """Check every running experiment's guardrails, and evaluate the ones whose window has closed.

    Guardrails are checked often because the point of them is to stop damage early; evaluation waits
    for the data to be readable.
    """
    if ppc_paused(session) or not ads_configured():
        return
    from app.ppc import experiments
    from app.services.notify import notify_owner

    for profile in profiles.active(session):
        for experiment in experiments.running(session, profile):
            watched = _guarded(session, lambda e=experiment: experiments.monitor(session, e),
                               what=f"watching {experiment.ref}")
            session.commit()
            if watched and watched.get("stopped"):
                notify_owner(session, f"🧪 *{experiment.ref} stopped early* — {watched['stopped']}. "
                                      "The treated bids have been put back to what they were.",
                             key=f"ppc_experiment_stopped:{experiment.ref}", urgent=True)
                session.commit()
        for experiment in experiments.due_for_evaluation(session, profile):
            done = _guarded(session, lambda e=experiment: experiments.evaluate(session, e),
                            what=f"evaluating {experiment.ref}")
            session.commit()
            if done is None or not (done.result or {}).get("settled"):
                continue
            from app.ppc import views

            notify_owner(session, views.experiment_detail(session, done.ref),
                         key=f"ppc_experiment_result:{done.ref}")
            session.commit()


@handler("ppc_playbook_review")
def ppc_playbook_review(session, payload, job) -> None:
    """The weekly learning loop: score the live rules, then propose whatever the evidence supports.

    Scoring first on purpose — a rule that has stopped working should be moved before new ones are
    proposed on top of it.
    """
    if ppc_paused(session) or not ads_configured():
        return
    from app.ppc import playbook
    from app.ppc import views
    from app.services.notify import notify_owner

    for profile in profiles.active(session):
        moved = _guarded(session, lambda p=profile: playbook.review(session, p),
                         what=f"playbook review ({profile.country_code})")
        session.commit()
        for change in (moved or {}).get("moved", []):
            rule = playbook.get_by_ref(session, change["rule"])
            notify_owner(session, f"📘 *{rule.ref} moved to {change['to'].replace('_', ' ')}* — "
                                  f"{rule.probation_reason or rule.title}",
                         key=f"ppc_playbook_moved:{rule.ref}:{change['to']}")
            session.commit()

        fresh = _guarded(session, lambda p=profile: playbook.propose_all(session, p),
                         what=f"playbook proposals ({profile.country_code})")
        session.commit()
        for rule in fresh or []:
            approval = playbook.request_approval(session, rule)
            session.commit()
            notify_owner(session, views.playbook_proposal(session, rule.ref, approval=approval),
                         key=f"ppc_playbook_proposed:{rule.ref}")
            session.commit()


@handler("ppc_research_scan")
def ppc_research_scan(session, payload, job) -> None:
    """Read the sources once a week. Nothing read here can change the account.

    Anything in a fetched document that reads as an instruction is reported to the owner rather than
    acted on — and could not be acted on from here even if it were believed, because this path has
    no write access to anything.
    """
    if ppc_paused(session) or not ads_configured():
        return
    from app.ppc import research
    from app.services.notify import notify_owner

    result = _guarded(session, lambda: research.scan(session), what="ppc research scan")
    if result is None:
        return
    session.commit()
    log.info("ppc research scan", extra={"kept": result["kept"], "read": result.get("read"),
                                         "attempted": result.get("attempted")})

    # A source that has gone for good needs a person to find where it went. Logging it meant the
    # scan could read nothing week after week and still look healthy — and when this was checked,
    # two of three sources had been answering 404 for some time. One notice per URL.
    for dead in result.get("gone") or []:
        notify_owner(session,
                     "📚 One of the pages I read each week has moved and I can't find it: "
                     f"{dead['source']}.\n{dead['url']}\n"
                     f"I'm still reading {result.get('read', 0)} of "
                     f"{result.get('attempted', 0)} sources. If you come across the new address, "
                     "send it to me — otherwise I'll keep going without it rather than guess.",
                     key=f"ppc_research_gone:{dead['url'][:60]}")
        session.commit()
    for found in result["suspicious"]:
        notify_owner(session,
                     "🚩 Something I read tried to give me instructions rather than information.\n"
                     f"Source: {found['source']}\n"
                     f'It said: "{found["text"]}"\n'
                     "I've recorded it and ignored it — I don't take instructions from documents. "
                     "Nothing was changed.",
                     key=f"ppc_research_injection:{found['url'][:60]}", urgent=True)
        session.commit()


@handler("ppc_weekly_report")
def ppc_weekly_report(session, payload, job) -> None:
    """The Monday strategy report."""
    if ppc_paused(session) or not ads_configured():
        return
    from app.ppc import report
    from app.services.notify import notify_owner

    for profile in profiles.active(session):
        text = _guarded(session, lambda p=profile: report.weekly(session, p),
                        what=f"weekly report ({profile.country_code})")
        if not text:
            continue
        from app.services.common import utcnow as _now

        notify_owner(session, text, key=f"ppc_weekly:{profile.amazon_profile_id}:{_now():%Y-%W}")
        session.commit()


@handler("ppc_alert_sweep")
def ppc_alert_sweep(session, payload, job) -> None:
    """Look for the few things worth interrupting the day for, and tell the owner about the new ones.

    Only *newly opened* alerts are sent. An alert that has been open since yesterday is still true,
    and messaging it again every hour is how an alerting system teaches people to ignore it — the
    open ones are always there in `PPC ALERTS`.
    """
    if ppc_paused(session) or not ads_configured():
        return
    from app.ppc import alerts
    from app.services.notify import notify_owner

    for profile in profiles.active(session):
        sweep = _guarded(session, lambda p=profile: alerts.run(session, p),
                         what=f"ppc alerts ({profile.country_code})")
        if sweep is None:
            continue
        # "Has the owner been told" is `alerted_at`, not `seen_count`. Gating on seen_count == 1 meant
        # that if anything failed between the commit below and the notify — the commit itself, a
        # worker kill, an exception out of notify_owner — the retry found the row already at
        # seen_count 2 and the message was lost for good. `alerted_at` is the flag that actually
        # records the thing, and it survives the retry because it is only set once sent.
        # Muted alerts are never messaged, however new they are: that is what muting is.
        fresh = [row for row in sweep.raised if row.alerted_at is None and row.status != "muted"]
        session.commit()
        if sweep.errors:
            log.warning("ppc alert sweep had failures", extra={"errors": sweep.errors})
        if not fresh:
            continue
        urgent = any(row.severity == "high" for row in fresh)
        notify_owner(session, _alert_message(fresh, profile),
                     key=f"ppc_alerts:{profile.amazon_profile_id}:"
                         f"{':'.join(sorted(str(r.id) for r in fresh))[:80]}",
                     urgent=urgent)
        for row in fresh:
            row.alerted_at = utcnow()
        session.commit()
        log.info("ppc alerts raised", extra={"profile": profile.amazon_profile_id,
                                             "new": len(fresh), "closed": len(sweep.resolved)})


def _alert_message(rows, profile) -> str:
    """One message for the batch. Five separate notifications about one bad morning is noise."""
    order = {"high": 0, "medium": 1, "low": 2}
    rows = sorted(rows, key=lambda r: order.get(r.severity, 3))
    head = "📊🚨" if rows[0].severity == "high" else "📊⚠️"
    lines = [f"{head} {(rows[0].details or {}).get('message', rows[0].type)}"]
    for row in rows[1:6]:
        lines.append(f"• {(row.details or {}).get('message', row.type)}")
    if len(rows) > 6:
        lines.append(f"• …and {len(rows) - 6} more — `PPC ALERTS` for all of them.")
    return "\n".join(lines)


@handler("ppc_budget_pace")
def ppc_budget_pace(session, payload, job) -> None:
    """Record where every campaign's budget stands, hour by hour.

    Kept separate from the sweep because it is the one PPC job worth running through the day: the
    reading is a snapshot Amazon overwrites, so an hour not asked about is an hour lost for good.
    """
    if ppc_paused(session) or not ads_configured():
        return
    from app.ppc import budget

    for profile in profiles.active(session):
        pace = _guarded(session, lambda p=profile: budget.poll(session, p),
                        what=f"ppc budget pace ({profile.country_code})")
        if pace is None:
            continue
        session.commit()
        log.info("ppc budget pace", extra={"profile": profile.amazon_profile_id,
                                           "readings": len(pace.readings),
                                           "errors": len(pace.errors)})


@handler("ppc_stream_poll")
def ppc_stream_poll(session, payload, job) -> None:
    """Drain the Marketing Stream notification queue, when there is one to drain.

    Dormant by design: without a subscription this returns without touching anything, exactly as the
    rest of Phase 4 sleeps without Ads credentials. See docs/DECISIONS.md D122 for why the transport
    is deliberately unfinished rather than guessed.
    """
    if ppc_paused(session):
        return
    from app.ppc import stream

    if not stream.configured():
        return
    try:
        result = stream.consume(session)
    except stream.StreamNotConfigured as exc:
        # Configured but unmapped. Logged once a day rather than every poll, because it is a state
        # of the world, not an error in this run.
        log.info("marketing stream not ingesting: %s", exc)
        return
    session.commit()
    log.info("marketing stream loaded", extra=result)


@handler("ppc_backfill")
def ppc_backfill(session, payload, job) -> None:
    """Queue the history load. What Amazon still holds is 95 days, so that is what this gets."""
    if ppc_paused(session) or not ads_configured():
        return
    days = int(payload.get("days") or backfill.REQUESTED_DAYS)
    results = _for_each_profile(session, lambda profile: backfill.enqueue(session, profile, days=days),
                                what="ppc backfill")
    if not results:
        return
    result = {"available_days": max(r["available_days"] for r in results),
              "requests": sum(r["requests"] for r in results)}
    if payload.get("notify"):
        from app.services.notify import notify_owner

        notify_owner(session, f"📊 Loading the advertising history now — {result['available_days']} "
                              f"days of it, in {result['requests']} report requests spread over the "
                              "next few hours. Amazon doesn't keep more than that, so the longer "
                              "history is the one we build from here.", key="ppc_backfill_started")


@handler("ppc_backfill_chunk")
def ppc_backfill_chunk(session, payload, job) -> None:
    """A history chunk goes through the same short-job report path as the daily load."""
    if ppc_paused(session) or not ads_configured():
        return
    ppc_report(session, payload, job)
