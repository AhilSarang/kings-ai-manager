"""Inbound message pipeline (Section 8)."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select

from app.agents.registry import get_agent
from app.agents.supervisor import SupervisorAgent
from app.db.models import Job, Message, Task, User
from app.db.session import session_scope
from app.jobs import queue
from app.jobs.errors import PermanentError
from app.jobs.handlers import handler
from app.llm import gate
from app.llm.base import LLMError
from app.services import audit, commands, outbound, settings_store, users
from app.services.common import SYSTEM, Actor

log = logging.getLogger(__name__)

NOT_UNDERSTOOD = "I didn't understand that — could you rephrase?"


def _load(session, payload) -> tuple[Message, User]:
    msg = session.get(Message, uuid.UUID(payload["message_id"]))
    if msg is None:
        raise PermanentError("message not found")
    user = session.get(User, msg.user_id)
    if user is None or not user.is_active:
        raise PermanentError("sender no longer active")
    return msg, user


def _reply(session, user: User, msg: Message, text: str, *, related_task_id=None) -> None:
    # Direct replies to something the owner just sent are not held for quiet hours.
    outbound.queue_text(session, user, text, urgent=True, related_task_id=related_task_id)
    msg.status = "processed"
    if related_task_id and msg.related_task_id is None:
        msg.related_task_id = related_task_id


def _error_reply(job: Job, error: str) -> None:
    """Final-failure hook: never leave the owner in silence."""
    with session_scope() as s:
        ref = audit.next_error_ref(s)
        audit.record(s, SYSTEM, "processing_error", target_type="job", target_id=job.id,
                     details={"error_ref": ref, "job_type": job.type, "error": error[:1000]})
        msg = s.get(Message, uuid.UUID(job.payload["message_id"])) if job.payload.get("message_id") else None
        owner = s.get(User, msg.user_id) if msg else users.get_owner(s)
        if owner is None:
            return
        if msg:
            msg.status = "failed"
        outbound.queue_text(s, owner, f"Something went wrong processing that; logged as error {ref}.",
                            urgent=True)
    log.error("processing failed; owner told", extra={"error_ref": ref})


@handler("process_inbound_message", critical=True, on_final_failure=_error_reply)
def process_inbound_message(session, payload, job) -> None:
    msg, user = _load(session, payload)
    if msg.status == "processed":
        return
    result = commands.run(session, user, msg)
    if result is not None:
        audit.record(session, Actor.user(user.id), "command_executed", target_type="message",
                     target_id=msg.id, details={"text": commands.mask_codes(msg.body)[:100]},
                     trigger_message_id=msg.id)
        _reply(session, user, msg, result.text, related_task_id=result.related_task_id)
        return

    paused = settings_store.is_paused(session)
    backlog = session.execute(
        select(func.count()).select_from(Job)
        .where(Job.type == "supervisor_interpret", Job.status.in_(("pending", "running", "waiting_llm")))
    ).scalar_one()
    queue.enqueue(session, "supervisor_interpret", {"message_id": str(msg.id)}, uses_llm=True,
                  max_attempts=3, idempotency_key=f"interpret:{msg.id}", priority=20)
    if paused:
        outbound.queue_text(session, user, "⏸️ The system is paused. I saved your message and will handle it "
                                           "after you send RESUME.", urgent=True)
    elif backlog and not gate.is_llm_paused(session):
        outbound.queue_text(session, user, "⏳ Got it — I'm working through a few messages and will reply shortly.",
                            urgent=True)


@handler("supervisor_interpret", on_final_failure=_error_reply)
def supervisor_interpret(session, payload, job) -> None:
    msg, user = _load(session, payload)
    if msg.status == "processed":
        return
    try:
        reply, result, call_id = SupervisorAgent().interpret(session, user, msg)
    except LLMError as exc:  # includes invalid output after the retry
        session.rollback()
        msg, user = _load(session, payload)
        audit.record(session, SYSTEM, "supervisor_not_understood", target_type="message", target_id=msg.id,
                     details={"error": str(exc)[:500]}, trigger_message_id=msg.id)
        _reply(session, user, msg, NOT_UNDERSTOOD)
        return
    audit.record(session, Actor.agent("supervisor"), "supervisor_replied", target_type="message",
                 target_id=msg.id, trigger_message_id=msg.id, llm_call_id=call_id,
                 details={"created": [t.ref for t in result.created], "errors": result.errors})
    _reply(session, user, msg, reply, related_task_id=result.related_task_id)


def _task_failure(job: Job, error: str) -> None:
    with session_scope() as s:
        task = s.get(Task, uuid.UUID(job.payload["task_id"]))
        if task is None:
            return
        from app.services import tasks

        if task.status in ("queued", "in_progress"):
            tasks.change_status(s, task, "failed", Actor.agent("supervisor"), reason="Could not complete the work")
        ref = audit.next_error_ref(s)
        audit.record(s, SYSTEM, "processing_error", target_type="task", target_id=task.ref,
                     details={"error_ref": ref, "error": error[:1000]})
        owner = users.get_owner(s)
        if owner:
            outbound.queue_text(s, owner, f"⚠️ I couldn't finish {task.ref} ({task.title}); logged as error {ref}. "
                                          f"Ask me to retry {task.ref} when you're ready.", related_task_id=task.id)


@handler("supervisor_run_task", on_final_failure=_task_failure)
def supervisor_run_task(session, payload, job) -> None:
    task = session.get(Task, uuid.UUID(payload["task_id"]))
    if task is None or task.status not in ("queued", "in_progress"):
        return
    agent = get_agent(session, task.agent.key)
    if agent is None or not agent.can_handle(task):
        return
    try:
        result = agent.run(session, task)
    except LLMError as exc:
        raise PermanentError(str(exc)) from exc
    owner = users.get_owner(session)
    if owner is None:
        return
    if result.status == "waiting_owner":
        text = f"❓ {task.ref} ({task.title}) — I need one thing from you:\n\n{result.text}"
    else:
        text = f"✅ {task.ref} done — {task.title}\n\n{result.text}\n\n(Draft only — nothing was sent.)"
    outbound.queue_text(session, owner, text, related_task_id=task.id)
