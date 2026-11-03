from datetime import timedelta

from sqlalchemy import text

from app.jobs import scheduler as sched
from app.services import health
from app.services.common import utcnow


def test_heartbeat_and_staleness(db):
    assert health.worker_is_stale(db)  # never seen
    health.beat(db, details={"x": 1})
    db.commit()
    assert not health.worker_is_stale(db)
    assert health.worker_is_stale(db, now=utcnow() + timedelta(minutes=16))
    sched.heartbeat_tick()
    assert health.last_seen(db) is not None


def test_notification_claimed_once(db):
    assert health.claim_notification(db, "alert:1") is True
    assert health.claim_notification(db, "alert:1") is False
    health.release_notification(db, "alert:1")
    assert health.claim_notification(db, "alert:1") is True


def test_scheduler_persists_jobs_in_postgres(db):
    s1 = sched.build_scheduler()
    s1.start(paused=True)
    s1.add_job("app.jobs.scheduler:heartbeat_tick", "interval", id="stale_entry", minutes=5)
    try:
        ids = set(db.execute(text("SELECT id FROM apscheduler_jobs")).scalars())
        assert set(sched.PERIODIC) | {"stale_entry"} <= ids
    finally:
        s1.shutdown(wait=False)

    # "restart": a new scheduler sees the persisted jobs and prunes the stale one
    s2 = sched.build_scheduler()
    s2.start(paused=True)
    try:
        sched.prune_stale(s2)
        ids = {j.id for j in s2.get_jobs()}
        assert ids == set(sched.PERIODIC)
        assert s2.get_job("heartbeat").next_run_time is not None
    finally:
        s2.shutdown(wait=False)
        db.execute(text("DELETE FROM apscheduler_jobs"))
        db.commit()
