from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.actions.registry import ActionNotPermitted
from app.db.models import Approval, AuditLog, Job
from app.jobs import queue
from app.llm.fake import FakeLLMProvider
from app.services import approvals, settings_store, tasks, users
from app.services.common import SYSTEM, utcnow
from tests.conftest import run_jobs
from tests.helpers import leaked, post, text_payload


@pytest.fixture
def client(db, owner):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def say(client, sent, message: str) -> str:
    before = len(sent)
    assert post(client, text_payload(message)).status_code == 200
    run_jobs(limit=100)
    return "\n---\n".join(s.get("text", f"[{s['kind']}]") for s in sent[before:])


def _approval(db, ref="A-1") -> Approval:
    db.expire_all()
    return approvals.get_by_ref(db, ref)


def _executions(db) -> int:
    return len(db.execute(select(AuditLog).where(AuditLog.action == "demo_external_action_logged")).all())


def _demo(db, owner, **kw) -> Approval:
    a = approvals.create_demo(db, owner, **kw)
    db.commit()
    return a


# ---------- creation rules ----------
def test_request_freezes_payload_and_moves_task(db, owner):
    a = _demo(db, owner, message="hi")
    assert a.ref == "A-1" and a.status == "pending" and a.risk_level == "medium"
    assert len(a.confirmation_code) == 4 and set(a.confirmation_code) <= set(approvals.CODE_ALPHABET)
    assert a.payload_hash == approvals.payload_hash({"message": "hi", "recipient": "nobody (demo)"})
    assert timedelta(hours=23) < a.expires_at - utcnow() <= timedelta(hours=24)
    task = tasks.get_by_ref(db, "T-1")
    assert task.status == "waiting_approval"
    assert "approval_requested" in [e.event_type for e in tasks.recent_events(db, task)]


def test_codes_are_random():
    assert len({approvals.new_code() for _ in range(200)}) > 190


def test_tier1_action_does_not_need_approval(db):
    with pytest.raises(approvals.ApprovalError, match="does not need approval"):
        approvals.request_approval(db, action_type="create_task", payload={}, summary="x", risk_level="low",
                                   requested_by_agent_key="supervisor")


def test_prohibited_action_never_allowed(db):
    with pytest.raises(ActionNotPermitted, match="yourself"):
        approvals.request_approval(db, action_type="fund_transfer", payload={"amount": 1}, summary="x",
                                   risk_level="critical", requested_by_agent_key="supervisor")


def test_agent_above_its_tier_refused(db):
    with pytest.raises(ActionNotPermitted):  # supervisor max_tier is 2; ppc changes are tier 3
        approvals.request_approval(db, action_type="ppc_budget_change", payload={}, summary="x",
                                   risk_level="high", requested_by_agent_key="supervisor")


def test_action_without_executor_refused(db):
    with pytest.raises(approvals.ApprovalError, match="No executor"):
        approvals.request_approval(db, action_type="send_email", payload={}, summary="x", risk_level="medium",
                                   requested_by_agent_key="supervisor")


def test_tier3_risk_is_at_least_high(db, monkeypatch):
    monkeypatch.setitem(approvals.EXECUTORS, "price_change", lambda s, p, a: {"summary": "ok"})
    from app.services import agents

    agents.get_by_key(db, "supervisor").permissions = {"max_tier": 3}
    a = approvals.request_approval(db, action_type="price_change", payload={"sku": "X", "price": 9.99},
                                   summary="Change price", risk_level="low", requested_by_agent_key="supervisor")
    assert a.risk_level == "high"


def test_owner_is_notified_with_exact_command(client, db, owner, sent):
    out = say(client, sent, "DEMO APPROVAL")
    a = _approval(db)
    assert "Demo created" in out and out.count("APPROVE A-1") == 1
    assert f"APPROVE A-1 {a.confirmation_code}" in out and "REJECT A-1" in out
    assert "Risk: MEDIUM" in out


# ---------- acceptance 6 ----------
def test_ok_yes_sure_never_approve(client, db, owner, sent):
    _demo(db, owner)
    for word in ("ok", "yes", "sure", "Approve", "go ahead 👍", "👍"):
        FakeLLMProvider.script({"actions": [{"type": "no_op", "params": {}}], "reply_to_owner": "noted"})
        say(client, sent, word)
    assert _approval(db).status == "pending"
    assert _executions(db) == 0
    assert any("Nothing was approved" in s.get("text", "") for s in sent)


def test_approve_requires_ref_and_code(client, db, owner, sent):
    a = _demo(db, owner)
    assert "include the code" in say(client, sent, "APPROVE A-1")
    wrong = "ZZZZ" if a.confirmation_code != "ZZZZ" else "YYYY"
    assert "doesn't match" in say(client, sent, f"APPROVE A-1 {wrong}")
    assert "can't find A-9" in say(client, sent, f"APPROVE A-9 {a.confirmation_code}")
    assert _approval(db).status == "pending" and _executions(db) == 0


def test_correct_approval_executes_exactly_once(client, db, owner, sent):
    a = _demo(db, owner, message="ship it")
    out = say(client, sent, f"approve a-1 {a.confirmation_code.lower()}")
    assert "A-1 approved" in out and "A-1 executed" in out and "nothing was sent" in out
    a = _approval(db)
    assert a.status == "executed" and a.decided_by_user_id == owner.id and a.executed_at
    assert _executions(db) == 1
    task = tasks.get_by_ref(db, "T-1")
    assert task.status == "done" and "ship it" in task.result_summary

    # repeating the approval, re-running the job, or enqueueing again never re-executes
    assert "already executed" in say(client, sent, f"APPROVE A-1 {a.confirmation_code}")
    assert queue.enqueue(db, "execute_approval", {"approval_id": str(a.id)},
                         idempotency_key=f"approval:{a.id}") is None
    queue.enqueue(db, "execute_approval", {"approval_id": str(a.id)})
    db.commit()
    run_jobs()
    assert approvals.execute(db, a.id) == "skipped:executed"
    db.rollback()
    assert _executions(db) == 1


def test_expired_approval_cannot_be_approved_or_run(client, db, owner, sent):
    a = _demo(db, owner)
    a.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    assert "expired" in say(client, sent, f"APPROVE A-1 {a.confirmation_code}")
    assert _approval(db).status == "expired"
    assert approvals.execute(db, a.id) == "skipped:expired"
    db.rollback()
    assert _executions(db) == 0
    assert tasks.get_by_ref(db, "T-1").status == "blocked"


def test_expiry_job_marks_and_notifies(db, owner, sent):
    from app.jobs.periodic import expire_approvals

    owner.last_inbound_at = utcnow()  # 24h window open
    a = _demo(db, owner)
    a.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    expire_approvals()
    assert _approval(db).status == "expired"
    run_jobs(["send_messages"])
    assert any("expired without a decision: A-1" in s.get("text", "") for s in sent)


def test_approved_but_decided_after_expiry_is_not_run(db, owner):
    a = _demo(db, owner)
    a.status, a.decided_at = "approved", a.expires_at + timedelta(seconds=1)
    db.commit()
    assert approvals.execute(db, a.id) == "expired"
    assert _executions(db) == 0


def test_reject(client, db, owner, sent):
    _demo(db, owner)
    out = say(client, sent, "REJECT A-1 not now")
    assert "A-1 rejected" in out
    a = _approval(db)
    assert a.status == "rejected" and approvals.execute(db, a.id) == "skipped:rejected"
    db.rollback()  # release the row lock taken by execute()
    assert tasks.get_by_ref(db, "T-1").status == "blocked"
    assert "already rejected" in say(client, sent, "REJECT A-1")


def test_tampered_payload_is_not_executed(client, db, owner, sent):
    a = _demo(db, owner)
    db.execute(text("UPDATE approvals SET action_payload = '{\"message\": \"evil\"}' WHERE id = :id"), {"id": a.id})
    db.commit()
    out = say(client, sent, f"APPROVE A-1 {a.confirmation_code}")
    assert "failed to execute" in out and "Payload changed" in out
    assert _approval(db).status == "execution_failed"
    assert _executions(db) == 0
    assert tasks.get_by_ref(db, "T-1").status == "failed"


def test_executor_error_marks_failed_once(client, db, owner, sent, monkeypatch):
    calls = []

    def boom(session, payload, approval):
        calls.append(1)
        raise RuntimeError("remote API down")

    monkeypatch.setitem(approvals.EXECUTORS, "demo_external_action", boom)
    a = _demo(db, owner)
    say(client, sent, f"APPROVE A-1 {a.confirmation_code}")
    assert _approval(db).status == "execution_failed" and len(calls) == 1
    assert "remote API down" in _approval(db).execution_result["error"]
    run_jobs()
    assert len(calls) == 1


def test_brute_force_locks_approval(client, db, owner, sent):
    a = _demo(db, owner)
    wrong = [c for c in ("AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF") if c != a.confirmation_code][:5]
    for code in wrong[:4]:
        assert "doesn't match" in say(client, sent, f"APPROVE A-1 {code}")
    assert "cancelled for safety" in say(client, sent, f"APPROVE A-1 {wrong[4]}")
    assert "already rejected" in say(client, sent, f"APPROVE A-1 {a.confirmation_code}")
    assert _executions(db) == 0


def test_critical_requires_pin(client, db, owner, sent):
    a = _demo(db, owner)
    a.risk_level = "critical"
    db.commit()
    assert "no PIN is set" in say(client, sent, f"APPROVE A-1 {a.confirmation_code}")
    owner.approval_pin_hash = users.hash_pin("4821")
    db.commit()
    assert "PIN doesn't match" in say(client, sent, f"APPROVE A-1 {a.confirmation_code}")
    assert "PIN doesn't match" in say(client, sent, f"APPROVE A-1 {a.confirmation_code} 1111")
    out = say(client, sent, f"APPROVE A-1 {a.confirmation_code} 4821")
    assert "executed" in out and _executions(db) == 1
    # Walked rather than substring-scanned: UUID keys, SHA-256 payload hashes and `wamid.` ids are
    # random hex, and a four-digit PIN turns up inside one of them often enough that `"4821" not in
    # str(rows)` failed about one run in thirty for no reason at all. `leaked` skips exactly those
    # fields, so a PIN stored in anything readable still fails this.
    assert not leaked("4821", *db.execute(select(AuditLog.details)).scalars().all())
    from app.db.models import Message

    stored = db.execute(select(Message.body, Message.raw_payload).where(Message.direction == "inbound")).all()
    assert stored
    for secret in ("4821", "1111"):
        assert not leaked(secret, [list(row) for row in stored]), f"{secret} was stored"


def test_approve_blocked_while_paused_and_runs_after_resume(client, db, owner, sent):
    a = _demo(db, owner)
    say(client, sent, "STOP ALL")
    assert "paused" in say(client, sent, f"APPROVE A-1 {a.confirmation_code}")
    say(client, sent, "RESUME")
    say(client, sent, f"APPROVE A-1 {a.confirmation_code}")
    assert _executions(db) == 1


def test_approved_job_waits_during_pause(db, owner):
    a = _demo(db, owner)
    approvals.approve(db, owner, "A-1", a.confirmation_code)
    settings_store.set_value(db, "system_paused", True, SYSTEM)
    from app.services.commands import PRESERVED_ON_STOP
    from app.jobs.handlers import CRITICAL_TYPES

    queue.cancel_pending(db, keep_types=CRITICAL_TYPES | PRESERVED_ON_STOP)
    db.commit()
    run_jobs()
    assert _executions(db) == 0
    job = db.execute(select(Job).where(Job.type == "execute_approval")).scalar_one()
    assert job.status == "pending"
    settings_store.set_value(db, "system_paused", False, SYSTEM)
    db.commit()
    run_jobs()
    assert _executions(db) == 1


def test_approvals_command_and_non_owner(client, db, owner, sent):
    a = _demo(db, owner)
    out = say(client, sent, "APPROVALS")
    assert f"APPROVE A-1 {a.confirmation_code}" in out
    manager = users.User(name="M", whatsapp_number="+15550002222", role="manager")
    db.add(manager)
    db.commit()
    with pytest.raises(approvals.ApprovalError, match="Only the owner"):
        approvals.approve(db, manager, "A-1", a.confirmation_code)
    db.rollback()


def test_end_to_end_demo(client, db, owner, sent):
    """Acceptance 6: 'ok' does nothing; correct APPROVE + code executes once; expired can't run."""
    say(client, sent, "DEMO APPROVAL")
    FakeLLMProvider.script({"actions": [{"type": "no_op", "params": {}}], "reply_to_owner": "?"})
    say(client, sent, "ok")
    assert _executions(db) == 0
    code = _approval(db).confirmation_code
    say(client, sent, f"APPROVE A-1 {code}")
    say(client, sent, f"APPROVE A-1 {code}")
    assert _executions(db) == 1
    say(client, sent, "DEMO APPROVAL")
    a2 = _approval(db, "A-2")
    a2.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert "expired" in say(client, sent, f"APPROVE A-2 {a2.confirmation_code}")
    assert _executions(db) == 1


def test_supervisor_prompt_lists_pending_approvals(client, db, owner, sent):
    _demo(db, owner)
    FakeLLMProvider.script({"actions": [{"type": "no_op", "params": {}}], "reply_to_owner": "x"})
    say(client, sent, "what needs my attention?")
    assert "A-1: Demo external action" in FakeLLMProvider.requests[0].prompt
    assert _approval(db).confirmation_code not in FakeLLMProvider.requests[0].prompt

def test_our_own_refs_are_accepted_however_they_are_typed():
    """The owner typed FB5 to cancel FB-5 and was refused; every other command accepts fb5."""
    from app.services.approvals import confirmation_matches

    assert confirmation_matches("FB-5", "FB5") and confirmation_matches("FB-5", "fb 5")
    assert not confirmation_matches("FB-5", "FB-6")
    assert not confirmation_matches("FB-5", "")
    # A SKU is the owner's string, not ours: two SKUs could differ only by a hyphen, so it stays exact.
    assert confirmation_matches("SKZ-001", "skz-001")
    assert not confirmation_matches("SKZ-001", "SKZ001")
    assert confirmation_matches("LIVE", "live") and not confirmation_matches("LIVE", "life")


def test_the_ref_is_read_however_it_is_spaced():
    """The owner sent "approve a 11 dslx" and the model narrated an approval that never happened."""
    from app.services.approvals import normalize_ref

    assert [normalize_ref(r) for r in ("A-11", "a11", "a 11", "A - 11")] == ["A-11"] * 4


def test_an_unreadable_approval_never_reaches_the_model(client, db, owner, sent):
    from app.services import commands

    for text in ("approve", "approve the shipment", "aprove a-3 now please"):
        matched = commands.match(text)
        assert matched and matched[0].name in ("APPROVE", "APPROVE_MALFORMED"), text
