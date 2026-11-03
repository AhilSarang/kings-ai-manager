"""Daily summary jobs."""

from __future__ import annotations

import logging

from app.jobs.handlers import handler
from app.llm import service as llm
from app.llm.base import LLMError
from app.prompts import load as load_prompt
from app.services import daily, summaries

log = logging.getLogger(__name__)


@handler("daily_summary_note")
def daily_summary_note(session, payload, job) -> None:
    day = payload["day"]
    if daily.already_sent(session, day):
        return
    note = None
    try:
        result, _ = llm.complete_json(
            daily.PrioritiesNote, system=load_prompt("daily_priorities"),
            prompt=summaries.daily_summary(session), purpose="daily_priorities", max_tokens=2000,
        )
        note = result.note.strip() or None
    except LLMError as exc:
        log.warning("daily note failed; sending without it", extra={"error": str(exc)})
    daily.send_daily_summary(session, day, note)


@handler("daily_summary_send")
def daily_summary_send(session, payload, job) -> None:
    daily.send_daily_summary(session, payload["day"], None)
