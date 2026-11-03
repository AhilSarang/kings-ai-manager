"""Email drafts: ref lookup and the draft status machine."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EMAIL_DRAFT_STATUSES, EmailDraft
from app.services import audit
from app.services.common import Actor, NotFound, ServiceError

REF_RE = re.compile(r"^\s*D-?(\d+)\s*$", re.I)

TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"pending_approval", "discarded"}),
    # Editing a draft that awaits approval sends it back to "draft" (the approval is cancelled).
    "pending_approval": frozenset({"draft", "sent", "send_failed", "discarded"}),
    "send_failed": frozenset({"draft", "pending_approval", "discarded"}),
    "sent": frozenset(),
    "discarded": frozenset(),
}
assert set(TRANSITIONS) == set(EMAIL_DRAFT_STATUSES)


class InvalidDraftTransition(ServiceError):
    pass


def normalize_ref(ref: str) -> str:
    m = REF_RE.match(ref or "")
    if not m:
        raise NotFound(f"'{ref}' is not a draft reference (expected like D-3)")
    return f"D-{int(m.group(1))}"


def get_by_ref(session: Session, ref: str, *, lock: bool = False) -> EmailDraft:
    norm = normalize_ref(ref)
    q = select(EmailDraft).where(EmailDraft.ref == norm)
    if lock:
        q = q.with_for_update(of=EmailDraft)
    draft = session.execute(q).scalar_one_or_none()
    if draft is None:
        raise NotFound(f"I can't find draft {norm}")
    return draft


def change_status(session: Session, draft: EmailDraft, status: str, actor: Actor, *, reason: str | None = None) -> None:
    if status not in EMAIL_DRAFT_STATUSES:
        raise InvalidDraftTransition(f"Unknown draft status '{status}'")
    old = draft.status
    if old == status:
        return
    if status not in TRANSITIONS[old]:
        raise InvalidDraftTransition(f"{draft.ref} is {old.replace('_', ' ')}; it can't become {status.replace('_', ' ')}")
    draft.status = status
    audit.record(session, actor, "email_draft_status_changed", target_type="email_draft", target_id=draft.ref,
                 details={"from": old, "to": status, "reason": reason, "version": draft.version})
    session.flush()
