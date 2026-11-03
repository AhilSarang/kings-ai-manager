from datetime import timedelta

import pytest

from app.db.models import TaskEvent
from app.services import agents, tasks
from app.services.common import Actor, NotFound, ServiceError, utcnow
from app.services.tasks import InvalidTransition


@pytest.fixture
def actor(owner):
    return Actor.user(owner.id)


def test_refs_are_sequential_and_unique(db, actor):
    refs = [tasks.create_task(db, title=f"t{i}", actor=actor).ref for i in range(3)]
    assert refs == ["T-1", "T-2", "T-3"]


def test_supervisor_task_is_queued(db, actor):
    t = tasks.create_task(db, title="Draft supplier note", actor=actor)
    assert t.status == "queued"
    assert t.agent.key == "supervisor"
    assert t.priority == "normal"


def test_inactive_agent_task_is_blocked_with_reason(db, actor):
    t = tasks.create_task(db, title="Find 20 suppliers", actor=actor, agent_key="sourcing", priority="high")
    assert t.status == "blocked"
    assert t.priority == "high"
    assert tasks.status_reason(t) == "Agent not active yet (planned for Phase 5)"


def test_unknown_agent_falls_back_to_supervisor(db, actor):
    t = tasks.create_task(db, title="x", actor=actor, agent_key="marketing")
    assert t.agent.key == "supervisor"


def test_created_task_has_events_and_audit(db, actor):
    from sqlalchemy import select

    from app.db.models import AuditLog

    t = tasks.create_task(db, title="x", actor=actor)
    types = [e.event_type for e in db.execute(select(TaskEvent).where(TaskEvent.task_id == t.id)).scalars()]
    assert types == ["created", "assigned", "status_changed"]
    actions = set(db.execute(select(AuditLog.action).where(AuditLog.target_id == t.ref)).scalars())
    assert {"task_created", "task_status_changed"} <= actions


@pytest.mark.parametrize("path", [
    ["in_progress", "waiting_approval", "in_progress", "done"],
    ["waiting_owner", "in_progress", "done"],
    ["in_progress", "failed", "queued", "in_progress", "done"],
])
def test_valid_transition_paths(db, actor, path):
    t = tasks.create_task(db, title="x", actor=actor)
    for status in path:
        reason = "because" if status in tasks.REASON_REQUIRED else None
        tasks.change_status(db, t, status, actor, reason=reason)
    assert t.status == "done"
    assert t.completed_at is not None


@pytest.mark.parametrize("start,target", [
    ("done", "in_progress"),
    ("cancelled", "queued"),
    ("queued", "waiting_approval"),
    ("failed", "done"),
])
def test_invalid_transitions_rejected(db, actor, start, target):
    t = tasks.create_task(db, title="x", actor=actor)
    if start == "failed":
        tasks.change_status(db, t, "failed", actor, reason="boom")
    else:
        tasks.change_status(db, t, start, actor, reason="r")
    with pytest.raises(InvalidTransition):
        tasks.change_status(db, t, target, actor)
    assert t.status == start


def test_unknown_status_rejected(db, actor):
    t = tasks.create_task(db, title="x", actor=actor)
    with pytest.raises(InvalidTransition):
        tasks.change_status(db, t, "archived", actor)


@pytest.mark.parametrize("status", ["blocked", "failed", "cancelled"])
def test_reason_required(db, actor, status):
    t = tasks.create_task(db, title="x", actor=actor)
    with pytest.raises(InvalidTransition, match="reason"):
        tasks.change_status(db, t, status, actor)


def test_blocked_task_can_be_marked_done_and_reason_cleared(db, actor):
    t = tasks.create_task(db, title="x", actor=actor, agent_key="amazon")
    tasks.change_status(db, t, "done", actor)
    assert t.status == "done"
    assert tasks.status_reason(t) is None


def test_update_task_records_changes(db, actor):
    t = tasks.create_task(db, title="x", actor=actor)
    due = utcnow() + timedelta(days=1)
    tasks.update_task(db, t, actor, priority="urgent", due_at=due)
    assert t.priority == "urgent" and t.due_at == due
    with pytest.raises(ServiceError):
        tasks.update_task(db, t, actor, priority="asap")


def test_terminal_task_cannot_be_edited(db, actor):
    t = tasks.create_task(db, title="x", actor=actor)
    tasks.change_status(db, t, "done", actor)
    with pytest.raises(InvalidTransition):
        tasks.update_task(db, t, actor, priority="low")


def test_get_by_ref_normalizes(db, actor):
    t = tasks.create_task(db, title="x", actor=actor)
    assert tasks.get_by_ref(db, "t-1").id == t.id
    assert tasks.get_by_ref(db, " T1 ").id == t.id
    with pytest.raises(NotFound):
        tasks.get_by_ref(db, "T-999")
    with pytest.raises(NotFound):
        tasks.get_by_ref(db, "hello")


def test_list_open_orders_by_priority_then_due(db, actor):
    now = utcnow()
    low = tasks.create_task(db, title="low", actor=actor, priority="low")
    urgent = tasks.create_task(db, title="urgent", actor=actor, priority="urgent")
    high_late = tasks.create_task(db, title="hl", actor=actor, priority="high", due_at=now + timedelta(days=3))
    high_soon = tasks.create_task(db, title="hs", actor=actor, priority="high", due_at=now + timedelta(days=1))
    done = tasks.create_task(db, title="d", actor=actor)
    tasks.change_status(db, done, "done", actor)
    assert [t.title for t in tasks.list_open(db)] == ["urgent", "hs", "hl", "low"]
    assert done not in tasks.list_open(db)
    _ = (low, urgent, high_late, high_soon)


def test_list_open_by_agent(db, actor):
    tasks.create_task(db, title="a", actor=actor, agent_key="amazon")
    tasks.create_task(db, title="s", actor=actor)
    assert [t.title for t in tasks.list_open(db, agent_key="amazon")] == ["a"]


def test_subtasks(db, actor):
    parent = tasks.create_task(db, title="p", actor=actor)
    tasks.create_task(db, title="c", actor=actor, parent_task_id=parent.id)
    db.refresh(parent)
    assert [c.title for c in parent.subtasks] == ["c"]


def test_inactive_reason_without_phase(db):
    a = agents.get_by_key(db, "email")
    a.config = {}
    assert agents.inactive_reason(a) == "Agent not active yet"


def test_events_keep_order_within_one_transaction(db, actor):
    t = tasks.create_task(db, title="x", actor=actor)
    for status in ["in_progress", "waiting_approval", "in_progress", "done"]:
        tasks.change_status(db, t, status, actor)
    db.commit()
    moves = [(e.data["from"], e.data["to"]) for e in tasks.recent_events(db, t, limit=20)
             if e.event_type == "status_changed"]
    assert moves == [("new", "queued"), ("queued", "in_progress"), ("in_progress", "waiting_approval"),
                     ("waiting_approval", "in_progress"), ("in_progress", "done")]
