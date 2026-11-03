import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.models import Agent, AuditLog, Setting, TaskEvent
from app.services import audit
from app.services.common import SYSTEM


def test_all_tables_exist(db):
    names = set(db.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars())
    expected = {"users", "messages", "agents", "tasks", "task_events", "approvals", "jobs", "files",
                "memory_entries", "audit_log", "llm_calls", "settings", "heartbeats", "notifications"}
    assert expected <= names


def test_six_agents_seeded_supervisor_and_email_active(db):
    agents = {a.key: a.status for a in db.execute(select(Agent)).scalars()}
    assert agents == {"supervisor": "active", "email": "active", "amazon": "inactive",
                      "ppc": "inactive", "sourcing": "inactive", "reporting": "inactive"}


def test_default_settings_seeded(db):
    keys = set(db.execute(select(Setting.key)).scalars())
    assert {"system_paused", "daily_summary_time", "quiet_hours", "llm_concurrency"} <= keys


def test_audit_log_is_append_only(db):
    entry = audit.record(db, SYSTEM, "test_action", details={"a": 1})
    db.commit()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("UPDATE audit_log SET action='x' WHERE id=:id"), {"id": entry.id})
    db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("DELETE FROM audit_log WHERE id=:id"), {"id": entry.id})
    db.rollback()
    assert db.get(AuditLog, entry.id).action == "test_action"


def test_task_events_are_append_only(db, owner):
    from app.services import tasks
    from app.services.common import Actor

    t = tasks.create_task(db, title="x", actor=Actor.user(owner.id))
    db.commit()
    ev = db.execute(select(TaskEvent).where(TaskEvent.task_id == t.id)).scalars().first()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("DELETE FROM task_events WHERE id=:id"), {"id": ev.id})
    db.rollback()


def test_check_constraints_reject_bad_status(db, owner):
    with pytest.raises(IntegrityError):
        db.execute(text("INSERT INTO tasks (title, status, priority, description) "
                        "VALUES ('x', 'bogus', 'normal', '')"))
    db.rollback()


def test_error_refs_increment(db):
    a, b = audit.next_error_ref(db), audit.next_error_ref(db)
    assert a.startswith("ERR-") and int(b[4:]) == int(a[4:]) + 1
