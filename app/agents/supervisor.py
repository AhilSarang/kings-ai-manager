"""The Supervisor: interprets the owner's messages and completes simple tasks itself."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.actions import executor
from app.actions.schemas import SupervisorOutput, TaskWorkOutput
from app.agents.base import AgentResult, BaseAgent
from app.agents.context import build_supervisor_prompt, build_task_prompt
from app.db.models import Message, Task, User
from app.llm import service as llm
from app.prompts import load as load_prompt
from app.services import tasks
from app.services.common import Actor


class SupervisorAgent(BaseAgent):
    key = "supervisor"

    def can_handle(self, task: Task) -> bool:
        return bool(task.agent and task.agent.key == self.key
                    and (task.metadata_ or {}).get("supervisor_can_complete"))

    # ---- conversation ----
    def interpret(self, session: Session, user: User, message: Message) -> tuple[str, executor.ExecutionResult,
                                                                                   uuid.UUID]:
        prompt = build_supervisor_prompt(session, user, message)
        output, call_id = llm.complete_json(
            SupervisorOutput, system=load_prompt("supervisor_system"), prompt=prompt, purpose="supervisor_interpret",
        )
        ctx = executor.ExecutionContext(session=session, user=user, message=message, llm_call_id=call_id)
        result = executor.execute(ctx, output)
        return executor.compose_reply(output, result), result, call_id

    # ---- task work ----
    def run(self, session: Session, task: Task) -> AgentResult:
        actor = Actor.agent(self.key)
        if task.status == "queued":
            tasks.change_status(session, task, "in_progress", actor)
        output, _ = llm.complete_json(
            TaskWorkOutput, system=load_prompt("supervisor_task"), prompt=build_task_prompt(session, task),
            purpose="supervisor_task",
        )
        if output.outcome == "needs_owner_input":
            tasks.add_note(session, task, actor, f"Question for the owner: {output.result_text}")
            tasks.change_status(session, task, "waiting_owner", actor)
            return AgentResult("waiting_owner", output.result_text, output.summary)
        tasks.set_result(session, task, actor, output.result_text)
        tasks.change_status(session, task, "done", actor)
        return AgentResult("done", output.result_text, output.summary)
