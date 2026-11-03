"""Approval system (Section 9).

- Payload frozen at creation (canonical JSON + SHA-256), verified before execution.
- Approval requires the exact ref AND 4-character code (AND owner PIN for critical).
- Execution happens once, in the `execute_approval` job (idempotency key = approval id).
- Expired approvals can never be approved or executed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.actions.registry import check_permitted, get_spec
from app.actions.tiers import Tier, requires_approval
from app.db.models import RISK_LEVELS, Approval, Task, User
from app.jobs import queue
from app.services import agents as agents_svc
from app.services import audit, outbound, tasks, users
from app.services.common import SYSTEM, Actor, NotFound, ServiceError, utcnow
from app.services.formatting import fmt_dt

log = logging.getLogger(__name__)

CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I
CODE_LEN = 4
DEFAULT_TTL = timedelta(hours=24)
MAX_BAD_CODES = 5
REF_RE = re.compile(r"^\s*A\s*-?\s*(\d+)\s*$", re.I)     # "A-11", "A11", "a 11", "A - 11"

Executor = Callable[[Session, dict[str, Any], Approval], dict[str, Any]]
EXECUTORS: dict[str, Executor] = {}
# Called (in the failure transaction) when an executor raises: fn(session, payload, approval, error)
FAILURE_HOOKS: dict[str, Callable[[Session, dict[str, Any], Approval, str], None]] = {}


class ApprovalError(ServiceError):
    pass


def executor(action_type: str, on_failure=None):
    def register(fn: Executor) -> Executor:
        EXECUTORS[action_type] = fn
        if on_failure:
            FAILURE_HOOKS[action_type] = on_failure
        return fn

    return register


def cancel(session: Session, approval: Approval, actor: Actor, reason: str) -> bool:
    """Withdraw a pending approval (e.g. the draft it covers was edited). Returns False if not pending."""
    if approval.status != "pending":
        return False
    approval.status = "rejected"
    approval.decided_at = utcnow()
    approval.execution_result = {**(approval.execution_result or {}), "cancelled": reason}
    audit.record(session, actor, "approval_cancelled", target_type="approval", target_id=approval.ref,
                 details={"reason": reason})
    _task_event(session, approval, actor, "cancelled", reason)
    return True


def canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


# Refs we generate ourselves (FB-5, LC-3, A-9…). Everywhere else in the system "fb5" means "FB-5",
# so the typed-back confirmation accepts it too. A SKU is still matched exactly — it's the owner's string,
# not ours, and two SKUs could differ only by punctuation.
OUR_REF_RE = re.compile(r"^(FB|LC|A|L|D|E|T|C)-\d+$", re.I)


def confirmation_matches(required: str, given: str | None) -> bool:
    if not given:
        return False
    required, given = str(required).strip(), given.strip()
    if required.upper() == given.upper():
        return True
    if OUR_REF_RE.match(required):
        squash = lambda text: re.sub(r"[^A-Z0-9]", "", text.upper())  # noqa: E731
        return squash(required) == squash(given)
    return False


def normalize_ref(ref: str) -> str:
    m = REF_RE.match(ref or "")
    if not m:
        raise NotFound(f"'{ref}' is not an approval reference (expected like A-3)")
    return f"A-{int(m.group(1))}"


def get_by_ref(session: Session, ref: str, *, lock: bool = False) -> Approval:
    q = select(Approval).where(Approval.ref == normalize_ref(ref))
    if lock:
        q = q.with_for_update()
    approval = session.execute(q).scalar_one_or_none()
    if approval is None:
        raise NotFound(f"I can't find {normalize_ref(ref)}")
    return approval


# ---------------- creation ----------------
def request_approval(
    session: Session, *, action_type: str, payload: dict[str, Any], summary: str, risk_level: str,
    requested_by_agent_key: str, task: Task | None = None, ttl: timedelta = DEFAULT_TTL,
    notify: bool = True,
) -> Approval:
    spec = get_spec(action_type)
    agent = agents_svc.get_by_key(session, requested_by_agent_key)
    check_permitted(action_type, agent.permissions)  # raises for tier 4 or above the agent's tier
    if not requires_approval(spec.tier):
        raise ApprovalError(f"'{action_type}' does not need approval (tier {int(spec.tier)})")
    if action_type not in EXECUTORS:
        raise ApprovalError(f"No executor is registered for '{action_type}'")
    if risk_level not in RISK_LEVELS:
        raise ApprovalError(f"Invalid risk level '{risk_level}'")
    if spec.tier == Tier.FINANCIAL and RISK_LEVELS.index(risk_level) < RISK_LEVELS.index("high"):
        risk_level = "high"  # Tier 3 is never below high
    if RISK_LEVELS.index(risk_level) < RISK_LEVELS.index(spec.min_risk):
        risk_level = spec.min_risk

    frozen = json.loads(canonical(payload))  # deep copy, JSON-safe
    approval = Approval(
        task_id=task.id if task else None, requested_by_agent_id=agent.id, action_type=action_type,
        action_payload=frozen, payload_hash=payload_hash(frozen), risk_level=risk_level,
        summary=summary.strip(), status="pending", confirmation_code=new_code(),
        expires_at=utcnow() + ttl,
    )
    session.add(approval)
    session.flush()
    session.refresh(approval)

    actor = Actor.agent(requested_by_agent_key)
    if task is not None:
        if task.status in ("new", "queued"):
            tasks.change_status(session, task, "in_progress", actor)
        tasks.change_status(session, task, "waiting_approval", actor)
        tasks.record_event(session, task, "approval_requested", actor,
                           {"approval": approval.ref, "action_type": action_type, "risk": risk_level})
    audit.record(session, actor, "approval_requested", target_type="approval", target_id=approval.ref,
                 details={"action_type": action_type, "risk": risk_level, "payload_hash": approval.payload_hash,
                          "task": task.ref if task else None})

    owner = users.get_owner(session)
    if owner and notify:
        outbound.queue_text(session, owner, approval_message(approval, task),
                            urgent=risk_level in ("high", "critical"),
                            related_task_id=task.id if task else None)
    return approval


def approval_message(a: Approval, task: Task | None = None) -> str:
    pin = " <PIN>" if a.risk_level == "critical" else ""
    if (a.action_payload or {}).get("confirm_text"):
        pin += f" {a.action_payload['confirm_text']}"
    lines = [f"🔐 Approval needed: {a.ref}" + (f" (for {task.ref})" if task else ""),
             a.summary,
             f"Risk: {a.risk_level.upper()} · Expires {fmt_dt(a.expires_at)}",
             "",
             f"Reply  APPROVE {a.ref} {a.confirmation_code}{pin}  to approve",
             f"or  REJECT {a.ref}  to reject."]
    return "\n".join(lines)


# ---------------- decisions ----------------
def _expire_if_due(session: Session, a: Approval) -> bool:
    if a.status == "pending" and a.expires_at <= utcnow():
        _mark_expired(session, a)
        return True
    return False


def approve(session: Session, user: User, ref: str, code: str | None, pin: str | None = None,
            confirmation: str | None = None) -> Approval:
    if user.role != "owner":
        raise ApprovalError("Only the owner can approve actions.")
    a = get_by_ref(session, ref, lock=True)
    actor = Actor.user(user.id)
    if _expire_if_due(session, a):
        raise ApprovalError(f"{a.ref} has expired and can't be approved. Ask for it again if still needed.")
    if a.status != "pending":
        raise ApprovalError(f"{a.ref} is already {a.status.replace('_', ' ')}.")
    if not code:
        raise ApprovalError(f"Please include the code: APPROVE {a.ref} <code>")
    if not hmac.compare_digest(code.strip().upper(), a.confirmation_code):
        return _bad_code(session, a, actor)
    if a.risk_level == "critical":
        if not user.approval_pin_hash:
            raise ApprovalError("Critical approvals need your PIN, but no PIN is set. "
                                "Set one on the server: scripts/seed.py --set-pin")
        if not pin or not users.verify_pin(pin, user.approval_pin_hash):
            audit.record(session, actor, "approval_pin_mismatch", target_type="approval", target_id=a.ref)
            return _bad_code(session, a, actor, what="PIN")
    required = (a.action_payload or {}).get("confirm_text")
    if required:  # deletions and switching to live: typed back, so nothing rides on a mistyped ref
        if not confirmation_matches(required, confirmation):
            audit.record(session, actor, "approval_confirmation_mismatch", target_type="approval", target_id=a.ref)
            what = ("the ref" if OUR_REF_RE.match(str(required).strip()) else
                    "the SKU" if str(required)[:1].isalnum() and "-" in str(required) else f"\"{required}\"")
            raise ApprovalError(f"{a.ref} also needs {what} typed back exactly: "
                                f"APPROVE {a.ref} {a.confirmation_code} <PIN> {required}")
    a.status = "approved"
    a.decided_by_user_id = user.id
    a.decided_at = utcnow()
    audit.record(session, actor, "approval_approved", target_type="approval", target_id=a.ref,
                 details={"action_type": a.action_type, "payload_hash": a.payload_hash})
    _task_event(session, a, actor, "approved")
    queue.enqueue(session, "execute_approval", {"approval_id": str(a.id)}, priority=5,
                  idempotency_key=f"approval:{a.id}", max_attempts=1)
    return a


def _bad_code(session: Session, a: Approval, actor: Actor, what: str = "code") -> Approval:
    meta = dict(a.execution_result or {})
    meta["bad_attempts"] = int(meta.get("bad_attempts", 0)) + 1
    a.execution_result = meta
    audit.record(session, actor, "approval_code_mismatch", target_type="approval", target_id=a.ref,
                 details={"attempts": meta["bad_attempts"], "what": what})
    if meta["bad_attempts"] >= MAX_BAD_CODES:
        a.status = "rejected"
        a.decided_at = utcnow()
        audit.record(session, SYSTEM, "approval_locked", target_type="approval", target_id=a.ref)
        _task_event(session, a, SYSTEM, "locked after repeated wrong codes")
        session.commit()
        raise ApprovalError(f"Too many wrong attempts. {a.ref} has been cancelled for safety.")
    session.commit()
    raise ApprovalError(f"That {what} doesn't match {a.ref}. Nothing was approved.")


def reject(session: Session, user: User, ref: str, reason: str | None = None) -> Approval:
    a = get_by_ref(session, ref, lock=True)
    actor = Actor.user(user.id)
    if _expire_if_due(session, a):
        raise ApprovalError(f"{a.ref} had already expired; nothing will run.")
    if a.status != "pending":
        raise ApprovalError(f"{a.ref} is already {a.status.replace('_', ' ')}.")
    a.status = "rejected"
    a.decided_by_user_id = user.id
    a.decided_at = utcnow()
    audit.record(session, actor, "approval_rejected", target_type="approval", target_id=a.ref,
                 details={"reason": reason})
    _task_event(session, a, actor, "rejected", reason)
    return a


def _task_event(session: Session, a: Approval, actor: Actor, decision: str, reason: str | None = None) -> None:
    if not a.task_id:
        return
    task = session.get(Task, a.task_id)
    tasks.record_event(session, task, "approval_decided", actor,
                       {"approval": a.ref, "decision": decision, "reason": reason})
    if decision != "approved" and task.status == "waiting_approval":
        tasks.change_status(session, task, "blocked", actor, reason=f"Approval {a.ref} {decision}")


def _mark_expired(session: Session, a: Approval) -> None:
    a.status = "expired"
    audit.record(session, SYSTEM, "approval_expired", target_type="approval", target_id=a.ref)
    _task_event(session, a, SYSTEM, "expired")


def expire_due(session: Session) -> list[Approval]:
    due = list(session.execute(
        select(Approval).where(Approval.status == "pending", Approval.expires_at <= utcnow())
        .with_for_update(skip_locked=True)
    ).scalars())
    for a in due:
        _mark_expired(session, a)
    if due:
        owner = users.get_owner(session)
        if owner:
            refs = ", ".join(a.ref for a in due)
            outbound.queue_text(session, owner, f"⌛ Approval{'s' if len(due) > 1 else ''} expired without a "
                                                f"decision: {refs}. Nothing was done.")
    return due


def list_pending_text(session: Session) -> str:
    from app.services.summaries import pending_approvals

    pending = pending_approvals(session)
    if not pending:
        return "No pending approvals."
    blocks = []
    for a in pending:
        task = session.get(Task, a.task_id) if a.task_id else None
        blocks.append(approval_message(a, task))
    return "\n\n".join(blocks)


# ---------------- execution ----------------
def execute(session: Session, approval_id: uuid.UUID) -> str:
    a = session.execute(select(Approval).where(Approval.id == approval_id).with_for_update()).scalar_one_or_none()
    if a is None:
        return "missing"
    if a.status != "approved":
        return f"skipped:{a.status}"  # already executed, rejected, expired… never run twice
    if a.decided_at is None or a.decided_at > a.expires_at:
        a.status = "expired"
        audit.record(session, SYSTEM, "approval_expired", target_type="approval", target_id=a.ref)
        return "expired"

    task = session.get(Task, a.task_id) if a.task_id else None
    actor = Actor("agent", str(a.requested_by_agent_id))
    try:
        spec = get_spec(a.action_type)
        if spec.tier == Tier.PROHIBITED:
            raise ApprovalError("Prohibited action")
        if payload_hash(a.action_payload) != a.payload_hash:
            raise ApprovalError("Payload changed after approval; refusing to execute")
        run = EXECUTORS[a.action_type]
        # Mark before running so a crash mid-action can never lead to a second run.
        a.status = "executed"
        a.executed_at = utcnow()
        session.flush()
        result = run(session, json.loads(canonical(a.action_payload)), a)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        a = session.get(Approval, approval_id)
        a.status = "execution_failed"
        a.executed_at = utcnow()
        a.execution_result = {**(a.execution_result or {}), "error": str(exc)[:1000]}
        hook = FAILURE_HOOKS.get(a.action_type)
        if hook:
            try:
                hook(session, json.loads(canonical(a.action_payload)), a, str(exc))
            except Exception:  # noqa: BLE001
                log.exception("approval failure hook crashed", extra={"approval": a.ref})
        audit.record(session, SYSTEM, "approval_execution_failed", target_type="approval", target_id=a.ref,
                     details={"error": str(exc)[:500]})
        task = session.get(Task, a.task_id) if a.task_id else None
        if task and task.status in ("waiting_approval", "in_progress"):
            tasks.change_status(session, task, "failed", SYSTEM, reason=f"{a.ref} failed: {str(exc)[:120]}")
        _notify(session, f"❌ {a.ref} failed to execute: {str(exc)[:200]}", task)
        return "failed"

    a.execution_result = {**(a.execution_result or {}), **result}
    audit.record(session, actor, "approval_executed", target_type="approval", target_id=a.ref,
                 details={"action_type": a.action_type, "result": result})
    summary = result.get("summary", "Done")
    if task is not None:
        if task.status == "waiting_approval":
            tasks.change_status(session, task, "in_progress", actor)
        tasks.set_result(session, task, actor, summary)
        if result.get("complete_task", True):
            tasks.change_status(session, task, "done", actor)
    _notify(session, f"✅ {a.ref} executed: {summary}", task)
    return "executed"


def _notify(session: Session, text: str, task: Task | None) -> None:
    owner = users.get_owner(session)
    if owner:
        outbound.queue_text(session, owner, text, urgent=True, related_task_id=task.id if task else None)


# ---------------- demo action (Tier 2, only logs) ----------------
@executor("demo_external_action")
def _demo_external_action(session: Session, payload: dict[str, Any], approval: Approval) -> dict[str, Any]:
    log.info("demo_external_action executed", extra={"approval": approval.ref, "payload": payload})
    audit.record(session, SYSTEM, "demo_external_action_logged", target_type="approval", target_id=approval.ref,
                 details={"payload": payload})
    return {"summary": f"Demo action logged: \"{payload.get('message', '')}\" (nothing was sent)",
            "complete_task": True}


def create_demo(session: Session, user: User, message: str = "Hello from the approval demo") -> Approval:
    task = tasks.create_task(session, title="Demo: approval flow test", actor=Actor.user(user.id),
                             description="Created by DEMO APPROVAL to test the approval flow end to end.",
                             created_by_user_id=user.id)
    return request_approval(
        session, action_type="demo_external_action", payload={"message": message, "recipient": "nobody (demo)"},
        summary=f"Demo external action: log the message \"{message}\". Nothing is sent to anyone.",
        risk_level="medium", requested_by_agent_key="supervisor", task=task, notify=False,
    )
