from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditLog, Job, Message
from app.jobs import periodic
from app.llm import gate
from app.llm.base import LLMError, LLMUnavailableError
from app.llm.fake import FakeLLMProvider
from app.services import approvals, daily, outbound, settings_store, tasks
from app.services.common import SYSTEM, Actor, utcnow
from tests.conftest import run_jobs
from tests.helpers import post, text_payload

ET = ZoneInfo("America/New_York")


def et(hour: int, minute: int = 0, days: int = 0) -> datetime:
    today = utcnow().astimezone(ET).date() + timedelta(days=days)
    return datetime.combine(today, time(hour, minute), tzinfo=ET)


@pytest.fixture
def due_now(monkeypatch):
    monkeypatch.setattr(daily, "summary_due", lambda s, now=None: True)


@pytest.fixture
def open_window(db, owner):
    owner.last_inbound_at = utcnow()
    settings_store.set_value(db, "quiet_hours", None, SYSTEM)
    db.commit()
    return owner


def _texts(sent):
    return [s.get("text", f"[{s['kind']}]") for s in sent]


def _populate(db, owner):
    a = Actor.user(owner.id)
    done = tasks.create_task(db, title="Finished yesterday", actor=a)
    tasks.change_status(db, done, "done", a)
    done.completed_at = utcnow() - timedelta(days=1)
    tasks.create_task(db, title="Overdue thing", actor=a, priority="urgent", due_at=utcnow() - timedelta(days=2))
    tasks.create_task(db, title="Supplier search", actor=a, agent_key="sourcing")
    waiting = tasks.create_task(db, title="Need your input", actor=a)
    tasks.change_status(db, waiting, "waiting_owner", a)
    tasks.create_task(db, title="Queued work", actor=a)
    approvals.create_demo(db, owner)
    db.commit()


# ---------- content ----------
def test_daily_summary_content_is_from_db(db, owner):
    _populate(db, owner)
    text = daily.summaries.daily_summary(db, priorities_note="Top priorities: T-2.")
    assert text.startswith("☀️ Daily summary")
    for section in ("*Overdue* (1)", "*Waiting on you* (1)", "*Pending approvals* (1)", "*Blocked* (1)",
                    "*Completed yesterday* (1)", "Top priorities: T-2."):
        assert section in text
    assert "Finished yesterday" in text and "A-1" in text and "Supplier search" in text


def test_empty_summary(db, owner):
    assert "No open tasks" in daily.summaries.daily_summary(db)


# ---------- schedule ----------
def test_summary_not_started_before_time(db, owner):
    assert daily.maybe_start_daily_summary(db, now=et(7, 29)) is False
    assert db.execute(select(Job)).first() is None


def test_summary_started_once_per_day(db, owner):
    assert daily.maybe_start_daily_summary(db, now=et(7, 30)) is True
    assert daily.maybe_start_daily_summary(db, now=et(7, 31)) is False
    assert daily.maybe_start_daily_summary(db, now=et(9, 0)) is False
    assert daily.maybe_start_daily_summary(db, now=et(7, 30, days=1)) is True
    types = sorted(j.type for j in db.execute(select(Job)).scalars())
    assert types == ["daily_summary_note", "daily_summary_note", "daily_summary_send", "daily_summary_send"]


def test_summary_time_setting_overrides_env(db, owner):
    settings_store.set_value(db, "daily_summary_time", "06:15", SYSTEM)
    assert daily.maybe_start_daily_summary(db, now=et(6, 15)) is True


def test_summary_catches_up_after_downtime(db, owner):
    """Server down at 7:30 -> the summary still goes out if it's back within 4 hours."""
    assert daily.maybe_start_daily_summary(db, now=et(11, 5)) is True


def test_no_late_summary_after_catch_up_window(db, owner):
    """A server started in the evening doesn't send that morning's summary."""
    assert daily.maybe_start_daily_summary(db, now=et(11, 31)) is False
    assert daily.maybe_start_daily_summary(db, now=et(18, 54)) is False


def test_summary_with_llm_note_sent_once(db, open_window, sent, due_now):
    FakeLLMProvider.script({"note": "Top priorities: nothing urgent."})
    daily.maybe_start_daily_summary(db)
    db.commit()
    run_jobs()
    texts = _texts(sent)
    assert len(texts) == 1 and "Top priorities: nothing urgent." in texts[0]
    # fallback job fires later but nothing is sent twice
    for j in db.execute(select(Job).where(Job.status == "pending")).scalars():
        j.run_after = utcnow()
    db.commit()
    run_jobs()
    assert len(sent) == 1
    assert db.execute(select(AuditLog).where(AuditLog.action == "daily_summary_sent")).scalar_one()


def test_summary_sent_without_note_when_llm_fails(db, open_window, sent, due_now):
    FakeLLMProvider.script(LLMError("refused"))
    daily.maybe_start_daily_summary(db)
    db.commit()
    run_jobs()
    assert len(sent) == 1 and "Daily summary" in sent[0]["text"] and "Top priorities" not in sent[0]["text"]


def test_summary_sent_when_llm_paused(db, open_window, sent, due_now):
    gate.pause(db, utcnow() + timedelta(hours=1), "usage_limit")
    daily.maybe_start_daily_summary(db)
    db.commit()
    run_jobs()
    assert len(sent) == 1 and "Daily summary" in sent[0]["text"]
    assert FakeLLMProvider.requests == []


def test_fallback_sends_if_llm_hangs(db, open_window, sent, due_now):
    FakeLLMProvider.script(LLMUnavailableError("limit", "usage_limit"))
    daily.maybe_start_daily_summary(db)
    db.commit()
    run_jobs()  # note job parks; fallback not due yet
    assert not any("Daily summary" in t for t in _texts(sent))
    fallback = db.execute(select(Job).where(Job.type == "daily_summary_send")).scalar_one()
    fallback.run_after = utcnow()
    db.commit()
    run_jobs()
    assert sum("Daily summary" in t for t in _texts(sent)) == 1


def test_summary_can_be_disabled_note(db, open_window, sent, due_now):
    settings_store.set_value(db, "daily_summary_llm_note", False, SYSTEM)
    daily.maybe_start_daily_summary(db)
    db.commit()
    run_jobs()
    assert len(sent) == 1 and FakeLLMProvider.requests == []


def test_summary_outside_window_sends_template_then_summary_on_reply(db, owner, sent, due_now):
    settings_store.set_value(db, "quiet_hours", None, SYSTEM)
    settings_store.set_value(db, "daily_summary_llm_note", False, SYSTEM)
    owner.last_inbound_at = utcnow() - timedelta(days=2)
    db.commit()
    daily.maybe_start_daily_summary(db)
    db.commit()
    run_jobs()
    assert [s["kind"] for s in sent] == ["template"]
    from app.main import create_app

    with TestClient(create_app()) as client:
        post(client, text_payload("status"))
    run_jobs()
    texts = _texts(sent)
    assert any("Daily summary" in t for t in texts)


def test_no_summary_while_paused(db, owner):
    settings_store.set_value(db, "system_paused", True, SYSTEM)
    assert daily.maybe_start_daily_summary(db, now=et(9)) is False


def test_periodic_entrypoint(db, owner, monkeypatch):
    monkeypatch.setattr(daily, "summary_due", lambda s, now=None: True)
    periodic.daily_summary_check()
    assert db.execute(select(Job).where(Job.type == "daily_summary_send")).first() is not None


# ---------- reminders ----------
@pytest.fixture
def early_reminders(monkeypatch):
    monkeypatch.setattr(daily, "REMINDERS_NOT_BEFORE", time(0, 0))


def test_reminders_once_per_task_per_day(db, open_window, sent, early_reminders):
    a = Actor.user(open_window.id)
    late = tasks.create_task(db, title="Late invoice", actor=a, due_at=utcnow() - timedelta(days=1))
    soon = tasks.create_task(db, title="Due later today", actor=a,
                             due_at=min(utcnow() + timedelta(minutes=5), et(23, 59)))
    tasks.create_task(db, title="Next week", actor=a, due_at=utcnow() + timedelta(days=7))
    tasks.create_task(db, title="No due date", actor=a)
    done = tasks.create_task(db, title="Done overdue", actor=a, due_at=utcnow() - timedelta(days=1))
    tasks.change_status(db, done, "done", a)
    db.commit()
    assert daily.send_due_reminders(db) == 2
    assert daily.send_due_reminders(db) == 0
    db.commit()
    run_jobs()
    [msg] = _texts(sent)
    assert "*Overdue*" in msg and "Late invoice" in msg and "Due later today" in msg
    assert "Next week" not in msg and "Done overdue" not in msg
    assert "reminder_sent" in [e.event_type for e in tasks.recent_events(db, late)]
    _ = soon


def test_reminders_wait_for_morning_summary(db, owner):
    tasks.create_task(db, title="x", actor=Actor.user(owner.id), due_at=utcnow() - timedelta(days=1))
    now = et(8, 0)
    assert daily.send_due_reminders(db, now=now) == 0


def test_reminders_skipped_when_paused(db, owner, early_reminders):
    tasks.create_task(db, title="x", actor=Actor.user(owner.id), due_at=utcnow() - timedelta(days=1))
    settings_store.set_value(db, "system_paused", True, SYSTEM)
    assert daily.send_due_reminders(db) == 0


# ---------- quiet hours ----------
def test_non_urgent_held_overnight_and_released_in_morning(db, owner, sent):
    owner.last_inbound_at = utcnow()
    settings_store.set_value(db, "quiet_hours", {"start": "00:00", "end": "23:59"}, SYSTEM)
    outbound.queue_text(db, owner, "overnight update")
    db.commit()
    run_jobs()
    assert sent == []
    assert daily.release_quiet_hour_holds(db) == 0  # still quiet
    settings_store.set_value(db, "quiet_hours", {"start": "22:00", "end": "07:00"}, SYSTEM)
    assert daily.release_quiet_hour_holds(db, now=et(7, 5)) == 1
    db.commit()
    run_jobs()
    assert _texts(sent) == ["overnight update"]
    assert db.execute(select(Message.status).where(Message.body == "overnight update")).scalar_one() == "sent"


def test_all_periodic_jobs_registered():
    from app.jobs.scheduler import PERIODIC, _load_periodic_modules

    _load_periodic_modules()
    assert {"heartbeat", "recover_stuck_jobs", "wake_llm_jobs", "expire_approvals", "daily_summary_check",
            "due_reminders", "release_quiet_holds"} <= set(PERIODIC)
