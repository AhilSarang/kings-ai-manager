"""Queue messages to the owner. Sending happens in the `send_owner_message` job."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.jobs import queue
from app.services import health


def notify_owner(
    session: Session, text: str, *, key: str | None = None, urgent: bool = False,
    related_task_id: uuid.UUID | None = None, respect_quiet_hours: bool = True,
) -> bool:
    """Queue a WhatsApp message to the owner.

    With `key`, the message is sent at most once for that key. Returns False
    if it was already sent.
    """
    if key and not health.claim_notification(session, key):
        return False
    queue.enqueue(
        session, "send_owner_message",
        {"text": text, "urgent": urgent, "related_task_id": str(related_task_id) if related_task_id else None,
         "respect_quiet_hours": respect_quiet_hours},
        priority=10 if urgent else 50,
    )
    return True
