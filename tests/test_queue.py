import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import Job
from app.db.session import get_sessionmaker
from app.jobs import queue
from app.jobs.errors import LLMUnavailable, PermanentError
from app.jobs.handlers import CRITICAL_TYPES, handler
from app.jobs.worker import Worker
from app.llm import gate
from app.services import settings_store
from app.services.common import SYSTEM, utcnow

CALLS: list[tuple[str, dict]] = []
FAILURES: list[str] = []


@handler("test_ok")
def _ok(session, payload, job):
    CALLS.append(("ok", payload))


@handler("test_flaky")
def _flaky(session, payload, job):
    if job.attempts < payload.get("succeed_on", 99):
        raise RuntimeError("temporary")
    CALLS.append(("flaky", payload))


@handler("test_permanent", on_final_failure=lambda job, err: FAILURES.append(job.type))
def _permanent(session, payload, job):
    raise PermanentError("bad input")


@handler("test_llm")
def _llm(session, payload, job):
    if payload.get("limited"):
        raise LLMUnavailable("usage limit", retry_at=utcnow() + timedelta(minutes=30))
    CALLS.append(("llm", payload))


@handler("test_critical", critical=True)
def _critical(session, payload, job):
    CALLS.append(("critical", payload))


@handler("test_rollback")
def _rollback(session, payload, job):
    from app.services import audit

    audit.record(session, SYSTEM, "should_not_persist")
    raise RuntimeError("after write")


@pytest.fixture(autouse=True)
def _reset():
    CALLS.clear()
    FAILURES.clear()


def _job(db, job_id):
    db.expire_all()
    return db.get(Job, job_id)


def test_enqueue_claim_complete(db):
    job = queue.enqueue(db, "test_ok", {"n": 1})
    db.commit()
    assert Worker().run_one() is True
    assert CALLS == [("ok", {"n": 1})]
    j = _job(db, job.id)
    assert j.status == "done" and j.attempts == 1 and j.locked_by is None
    assert Worker().run_one() is False


def test_idempotency_key_dedupes(db):
    assert queue.enqueue(db, "test_ok", idempotency_key="k1") is not None
    assert queue.enqueue(db, "test_ok", idempotency_key="k1") is None
    db.commit()
    assert db.execute(select(Job)).scalars().all().__len__() == 1


def test_priority_and_run_after_ordering(db):
    later = queue.enqueue(db, "test_ok", {"n": "future"}, run_after=utcnow() + timedelta(hours=1))
    low = queue.enqueue(db, "test_ok", {"n": "low"}, priority=200)
    high = queue.enqueue(db, "test_ok", {"n": "high"}, priority=10)
    db.commit()
    w = Worker()
    while w.run_one():
        pass
    assert [p["n"] for _, p in CALLS] == ["high", "low"]
    assert _job(db, later.id).status == "pending"
    _ = (low, high)


def test_retry_with_backoff_then_success(db):
    job = queue.enqueue(db, "test_flaky", {"succeed_on": 2})
    db.commit()
    Worker().run_one()
    j = _job(db, job.id)
    assert j.status == "pending" and j.attempts == 1 and "temporary" in j.last_error
    assert j.run_after > utcnow() + timedelta(seconds=10)
    j.run_after = utcnow()
    db.commit()
    Worker().run_one()
    assert _job(db, job.id).status == "done"


def test_exhausted_retries_fail(db):
    job = queue.enqueue(db, "test_flaky", max_attempts=2)
    db.commit()
    w = Worker()
    for _ in range(2):
        j = _job(db, job.id)
        j.run_after = utcnow()
        db.commit()
        w.run_one()
    assert _job(db, job.id).status == "failed"


def test_permanent_error_fails_immediately_and_runs_hook(db):
    job = queue.enqueue(db, "test_permanent")
    db.commit()
    Worker().run_one()
    assert _job(db, job.id).status == "failed"
    assert FAILURES == ["test_permanent"]


def test_unknown_job_type_fails(db):
    job = queue.enqueue(db, "no_such_type")
    db.commit()
    Worker().run_one()
    j = _job(db, job.id)
    assert j.status == "failed" and "No handler" in j.last_error


def test_handler_writes_roll_back_on_error(db):
    from app.db.models import AuditLog

    queue.enqueue(db, "test_rollback")
    db.commit()
    Worker().run_one()
    assert db.execute(select(AuditLog).where(AuditLog.action == "should_not_persist")).first() is None


def test_backoff_is_capped():
    assert queue.backoff_delay(1) == timedelta(seconds=15)
    assert queue.backoff_delay(2) == timedelta(seconds=30)
    assert queue.backoff_delay(50) == timedelta(minutes=30)


# ---- LLM lane ----
def test_llm_unavailable_parks_job_without_using_attempts(db):
    job = queue.enqueue(db, "test_llm", {"limited": True}, uses_llm=True, max_attempts=1)
    db.commit()
    Worker().run_one(uses_llm=True)
    j = _job(db, job.id)
    assert j.status == "waiting_llm" and j.attempts == 0
    assert queue.wake_waiting_llm(db) == 0  # retry time not reached
    assert queue.wake_waiting_llm(db, now=utcnow() + timedelta(hours=1)) == 1
    db.commit()
    assert _job(db, job.id).status == "pending"


def test_lanes_are_separate(db):
    queue.enqueue(db, "test_llm", {"x": 1}, uses_llm=True)
    db.commit()
    assert Worker().run_one(uses_llm=False) is False
    assert Worker().run_one(uses_llm=True) is True


def test_llm_gate_blocks_llm_lane_only(db):
    gate.pause(db, utcnow() + timedelta(minutes=10), "usage limit")
    queue.enqueue(db, "test_llm", {"x": 1}, uses_llm=True)
    queue.enqueue(db, "test_ok", {"x": 2})
    db.commit()
    w = Worker()
    assert w.run_one(uses_llm=True) is False
    assert w.run_one(uses_llm=False) is True
    gate.clear(db)
    db.commit()
    assert w.run_one(uses_llm=True) is True


# ---- kill switch ----
def test_paused_system_runs_only_critical_jobs(db):
    assert "test_critical" in CRITICAL_TYPES
    settings_store.set_value(db, "system_paused", True, SYSTEM)
    queue.enqueue(db, "test_ok", priority=1)
    queue.enqueue(db, "test_critical", priority=50)
    db.commit()
    w = Worker()
    assert w.run_one() is True
    assert w.run_one() is False
    assert [c for c, _ in CALLS] == ["critical"]


def test_cancel_pending_keeps_given_types(db):
    a = queue.enqueue(db, "test_ok")
    b = queue.enqueue(db, "test_critical")
    assert queue.cancel_pending(db, keep_types={"test_critical"}) == 1
    db.commit()
    assert _job(db, a.id).status == "cancelled"
    assert _job(db, b.id).status == "pending"


# ---- concurrency & restart ----
def test_concurrent_claims_never_double_process(db):
    n_jobs, n_threads = 60, 8
    for i in range(n_jobs):
        queue.enqueue(db, "test_ok", {"i": i})
    db.commit()
    claimed: list = []
    lock = threading.Lock()
    start = threading.Barrier(n_threads)

    def grab(tid):
        Session = get_sessionmaker()
        start.wait()
        while True:
            with Session() as s:
                job = queue.claim(s, f"t{tid}")
                if job is None:
                    return
                with lock:
                    claimed.append(job.id)

    threads = [threading.Thread(target=grab, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == n_jobs
    assert len(set(claimed)) == n_jobs


def test_threaded_worker_processes_everything(db):
    for i in range(10):
        queue.enqueue(db, "test_ok", {"i": i})
    for i in range(3):
        queue.enqueue(db, "test_llm", {"i": i}, uses_llm=True)
    db.commit()
    w = Worker(poll_interval=0.05)
    w.start(llm_lanes=1, general_lanes=2)
    try:
        import time

        deadline = time.time() + 10
        while time.time() < deadline and len(CALLS) < 13:
            time.sleep(0.05)
    finally:
        w.stop()
    assert len(CALLS) == 13
    assert queue.counts(db) == {"done": 13}


def test_crashed_worker_job_is_recovered(db):
    """Simulates a reboot mid-job: the lock is abandoned, then released and re-run."""
    job = queue.enqueue(db, "test_ok", {"survives": True})
    db.commit()
    with get_sessionmaker()() as s:
        assert queue.claim(s, "dead-worker") is not None
    # "reboot": new engine/worker; the job is stuck in running
    from app.db.session import get_engine

    get_engine().dispose()
    assert Worker().run_one() is False
    assert queue.release_stuck(db) == 0  # not old enough yet
    assert queue.release_stuck(db, now=utcnow() + timedelta(minutes=11)) == 1
    db.commit()
    assert Worker().run_one() is True
    j = _job(db, job.id)
    assert j.status == "done" and j.attempts == 2
    assert CALLS == [("ok", {"survives": True})]


def test_stuck_job_without_attempts_left_fails(db):
    job = queue.enqueue(db, "test_ok", max_attempts=1)
    db.commit()
    with get_sessionmaker()() as s:
        queue.claim(s, "dead-worker")
    queue.release_stuck(db, now=utcnow() + timedelta(minutes=11))
    db.commit()
    assert _job(db, job.id).status == "failed"
