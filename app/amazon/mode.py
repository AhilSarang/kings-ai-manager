"""Validation-only or live (brief 15.11).

The agent starts in `validation_only`: every change is built, validated and previewed with Amazon,
but nothing is submitted. Turning writes on is an approval with the PIN and the word LIVE typed
back. Turning them off again is immediate — safety never waits for a code.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.amazon import accounts
from app.db.models import Approval
from app.services import approvals, audit, settings_store
from app.services.common import SYSTEM, Actor, ServiceError

log = logging.getLogger(__name__)

SETTING = "amazon_write_mode"
MODES = ("validation_only", "live")
ACTION = "amazon_set_write_mode"
CONFIRM_WORD = "LIVE"


class ModeError(ServiceError):
    pass


def current(session: Session) -> str:
    account = accounts.get_primary(session)
    return account.write_mode if account else "validation_only"


def describe(session: Session) -> str:
    if current(session) == "live":
        return ("🟢 Amazon write mode: LIVE. Approved changes are submitted to Amazon.\n"
                "Send AMAZON VALIDATION ONLY to stop submitting (that takes effect immediately).")
    return ("🟡 Amazon write mode: VALIDATION ONLY. I build, check and preview everything with Amazon, "
            "but nothing is submitted.\nSend AMAZON LIVE when you want approved changes to go through.")


def set_mode(session: Session, mode: str, actor: Actor) -> str:
    if mode not in MODES:
        raise ModeError(f"Write mode is either {' or '.join(MODES)}.")
    account = accounts.require(session)
    account.write_mode = mode
    settings_store.set_value(session, SETTING, mode, actor)  # survives accounts.ensure() on restart
    audit.record(session, actor, "amazon_write_mode_changed", target_type="amazon_account",
                 target_id=account.seller_id, details={"mode": mode})
    session.flush()
    log.info("amazon write mode changed", extra={"mode": mode})
    return describe(session)


def go_validation_only(session: Session, user) -> str:
    if current(session) == "validation_only":
        return describe(session)
    return set_mode(session, "validation_only", Actor.user(user.id))


def request_live(session: Session, user) -> str:
    """Switching writes on is the one setting change that needs the PIN."""
    if current(session) == "live":
        return describe(session)
    accounts.require(session)
    approval = approvals.request_approval(
        session, action_type=ACTION, payload={"mode": "live", "confirm_text": CONFIRM_WORD},
        summary=("Switch Amazon to LIVE. After this, anything you approve is submitted to Amazon for real: "
                 "listing edits, prices, new listings and shipment confirmations. Until now they were only "
                 "validated and previewed."),
        risk_level="critical", requested_by_agent_key="amazon", notify=False)
    return approvals.approval_message(approval)


@approvals.executor(ACTION)
def _execute(session: Session, payload: dict, approval: Approval) -> dict:
    mode = payload.get("mode") or "validation_only"
    text = set_mode(session, mode, SYSTEM)
    return {"summary": text, "complete_task": False}
