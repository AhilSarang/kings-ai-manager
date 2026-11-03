"""Approval execution job."""

from __future__ import annotations

import logging
import uuid

from app.jobs.handlers import handler
from app.amazon import changes  # noqa: F401  (registers the Amazon listing executors)
from app.amazon import fba_confirm  # noqa: F401  (registers the FBA shipment executors)
from app.amazon import mode as amazon_mode  # noqa: F401  (registers the write-mode executor)
from app.email import drafting  # noqa: F401  (registers the email_send executor)
from app.services import approvals

log = logging.getLogger(__name__)


@handler("execute_approval")
def execute_approval(session, payload, job) -> None:
    outcome = approvals.execute(session, uuid.UUID(payload["approval_id"]))
    log.info("approval execution", extra={"approval_id": payload["approval_id"], "outcome": outcome})
