"""Drafting replies (and brand-new emails) on request, and approval-gated sending (brief Section 8)."""

from __future__ import annotations

import html
import logging
import re
import uuid
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import (EmailAccount, EmailContact, EmailDraft, EmailMessage, EmailThread, File, MemoryEntry,
                           Task, User)
from app.email import drafts as drafts_svc
from app.email import accounts, followup, threads
from app.email.client import get_client, handle_auth_failure
from app.email.gmail import GmailAuthError, GmailError, GmailRetryable, build_raw
from app.email.parse import strip_quoted
from app.email.rules import EMAIL_RE
from app.jobs import queue
from app.llm import service as llm
from app.prompts import load as load_prompt
from app.services import approvals, audit, files, memory, outbound, settings_store, tasks
from app.services.common import Actor, NotFound, ServiceError, utcnow
from app.services.formatting import fmt_dt, truncate

log = logging.getLogger(__name__)
AGENT = Actor.agent("email")
PENDING_KEY = "email_pending_draft_request"
THREAD_CHARS = 12000
WARNING_NEW_DAYS = 7
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024  # Gmail's limit is 25 MB and base64 adds a third
RECENT_FILE_HOURS = 24


class DraftError(ServiceError):
    pass


class DraftOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body_text: str = Field(min_length=1, max_length=20000)
    notes: list[str] = Field(max_length=10)
    subject: str | None = Field(None, max_length=300)
    to: list[str] | None = Field(None, max_length=20)
    cc: list[str] | None = Field(None, max_length=20)


# ---------------- lookups ----------------
def find_threads(session: Session, *, sender: str | None = None, subject: str | None = None,
                 limit: int = 5) -> list[EmailThread]:
    q = select(EmailThread).join(EmailMessage, EmailMessage.thread_id == EmailThread.id)
    if sender:
        like = f"%{sender.strip().lower()}%"
        q = q.where(or_(EmailMessage.from_address.ilike(like), EmailMessage.from_name.ilike(like)))
    if subject:
        q = q.where(EmailThread.subject.ilike(f"%{subject.strip()}%"))
    q = q.where(EmailThread.status != "ignored").order_by(EmailThread.last_message_at.desc().nulls_last())
    seen, out = set(), []
    for t in session.execute(q.limit(50)).scalars():
        if t.id not in seen:
            seen.add(t.id)
            out.append(t)
        if len(out) >= limit:
            break
    return out


def resolve_thread(session: Session, *, ref: str | None, sender: str | None, subject: str | None) -> EmailThread:
    if ref:
        return threads.get_by_ref(session, ref)
    if not (sender or subject):
        raise DraftError("Which email? Give me the thread ref (like E-12), the sender, or the subject.")
    matches = find_threads(session, sender=sender, subject=subject)
    if not matches:
        raise NotFound("I couldn't find a matching email thread.")
    if len(matches) > 1:
        options = "\n".join(f"• {t.ref} — {truncate(t.subject, 60)} ({fmt_dt(t.last_message_at)})" for t in matches)
        raise DraftError(f"Several emails match. Which one?\n{options}\nReply e.g. \"draft {matches[0].ref} …\".")
    return matches[0]


def signature(session: Session) -> str | None:
    for m in session.execute(
        select(MemoryEntry).where(MemoryEntry.is_active.is_(True)).order_by(MemoryEntry.created_at.desc())
    ).scalars():
        if "signature" in m.title.lower():
            text = m.content.strip()
            text = re.sub(r"^(my )?(email )?signature (is|:)\s*", "", text, flags=re.I)
            return text.strip().strip('"').strip()
    return None


def last_inbound(session: Session, thread: EmailThread) -> EmailMessage | None:
    return session.execute(
        select(EmailMessage).where(EmailMessage.thread_id == thread.id, EmailMessage.is_from_owner.is_(False))
        .order_by(EmailMessage.sent_at.desc().nulls_last()).limit(1)
    ).scalar_one_or_none()


def last_message(session: Session, thread: EmailThread) -> EmailMessage | None:
    return session.execute(
        select(EmailMessage).where(EmailMessage.thread_id == thread.id)
        .order_by(EmailMessage.sent_at.desc().nulls_last()).limit(1)
    ).scalar_one_or_none()


def default_recipients(session: Session, thread: EmailThread) -> tuple[list[str], list[str]]:
    msg = last_inbound(session, thread)
    if msg is None:
        raise DraftError(f"{thread.ref} has no message from the other side to reply to.")
    own = thread.account.address.lower()
    to = [msg.reply_to or msg.from_address]
    cc = [a for a in dict.fromkeys(msg.cc or []) if a not in (own, to[0])]
    return to, cc


def reply_subject(subject: str) -> str:
    return subject if re.match(r"^\s*re:", subject or "", re.I) else f"Re: {subject}".strip()


def draft_account(session: Session, draft: EmailDraft) -> EmailAccount:
    return draft.thread.account if draft.thread else accounts.get_primary(session)


def where(draft: EmailDraft) -> str:
    return f"reply in {draft.thread.ref}" if draft.thread else "new email"


_TYPED_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def _contact_named(contact: EmailContact, text: str) -> bool:
    words = {w for w in re.findall(r"[a-z0-9]{3,}", text)}
    local, domain = contact.address.split("@", 1)
    names = set(re.findall(r"[a-z0-9]{3,}", (contact.name or "").lower()))
    labels = {domain.split(".")[0], *re.findall(r"[a-z0-9]{3,}", local)}
    return bool((names | labels) & words)


def normalize_typed(text: str) -> str:
    """The owner types addresses loosely: "sam @pellon. com" is still an address he typed."""
    return re.sub(r"\s*([@.])\s*", r"\1", (text or "").lower())


def check_new_recipients(session: Session, addresses: list[str] | None, owner_text: str) -> list[str]:
    """Recipients of a new email must be typed by the owner, or be a known contact he named."""
    text = normalize_typed(owner_text)
    typed = {a.lower() for a in _TYPED_ADDRESS.findall(text)}
    out, rejected = [], []
    for a in addresses or []:
        a = a.strip().lower()
        ok = False
        if EMAIL_RE.match(a):
            if a in typed:
                ok = True
            else:
                c = session.execute(select(EmailContact).where(EmailContact.address == a)).scalar_one_or_none()
                ok = c is not None and c.message_count > 0 and _contact_named(c, text)
        (out if ok else rejected).append(a)
    if rejected:
        raise DraftError("Nothing drafted. I can only start a new email to addresses you type yourself, or "
                         "people who have emailed you before and you name in your message. Please give the "
                         f"full address for: {', '.join(rejected)}")
    return list(dict.fromkeys(out))


def request_new_email(session: Session, user: User, *, to: list[str], cc: list[str] | None, subject: str | None,
                      instructions: str, owner_text: str) -> str:
    instructions = (instructions or "").strip()
    if not instructions:
        raise DraftError("What should the email say?")
    to = check_new_recipients(session, to, owner_text)
    cc = [a for a in check_new_recipients(session, cc, owner_text) if a not in to]
    if not to:
        raise DraftError("Who should the email go to? Give me their email address.")
    account = accounts.get_primary(session)
    if account is None or account.status != "active":
        raise DraftError("Gmail isn't connected, so I can't prepare emails right now.")
    payload = {"new": True, "to": to, "cc": cc, "subject": (subject or "").strip()[:300] or None,
               "instructions": instructions, "user_id": str(user.id)}
    if signature(session) is None:
        settings_store.set_value(session, PENDING_KEY, payload, Actor.user(user.id))
        return ("Before I write the first draft: what email signature should I use? Reply like "
                "\"remember my email signature is the owner, CEO, Kings Distro Inc., (804) …\" "
                "and I'll write the email straight after.")
    queue.enqueue(session, "draft_email_reply", payload, uses_llm=True, max_attempts=3, priority=20)
    return f"✍️ Writing a new email to {', '.join(to)}. Nothing will be sent."


# ---------------- attachments ----------------
def draft_files(session: Session, draft: EmailDraft) -> list[File]:
    ids = [uuid.UUID(str(i)) for i in (draft.attachment_file_ids or [])]
    by_id = {f.id: f for f in session.execute(select(File).where(File.id.in_(ids))).scalars()} if ids else {}
    return [by_id[i] for i in ids if i in by_id]


def file_label(f: File) -> str:
    return f"{f.original_name or 'file'} ({f.size_bytes / 1024 / 1024:.1f} MB)"


def recent_whatsapp_file(session: Session, user: User, message) -> File | None:
    """The file the owner just sent: on this message, or the last one he sent recently."""
    if message is not None and message.media_file_id:
        return session.get(File, message.media_file_id)
    return session.execute(
        select(File).where(File.source == "whatsapp", File.uploaded_by_user_id == user.id,
                           File.created_at >= utcnow() - timedelta(hours=RECENT_FILE_HOURS))
        .order_by(File.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def attach_file(session: Session, user: User, draft_ref: str, file: File | None) -> str:
    draft = drafts_svc.get_by_ref(session, draft_ref, lock=True)
    if draft.status in ("sent", "discarded"):
        raise DraftError(f"{draft.ref} is {draft.status} and can't be changed.")
    if file is None:
        raise DraftError(f"Send me the file on WhatsApp first, then reply \"ATTACH {draft.ref}\" "
                         "(or send the file with that as the caption).")
    current = draft_files(session, draft)
    if any(f.id == file.id for f in current):
        raise DraftError(f"{file.original_name or 'That file'} is already attached to {draft.ref}.")
    if len(current) >= MAX_ATTACHMENTS:
        raise DraftError(f"{draft.ref} already has {MAX_ATTACHMENTS} attachments.")
    total = sum(f.size_bytes for f in current) + file.size_bytes
    if total > MAX_ATTACHMENT_BYTES:
        raise DraftError(f"That would make the attachments {total / 1024 / 1024:.1f} MB; "
                         f"the limit is {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB.")
    actor = Actor.user(user.id)
    cancelled = _withdraw_approval(session, draft, actor, "attachment added")
    if draft.status in ("pending_approval", "send_failed"):
        drafts_svc.change_status(session, draft, "draft", actor, reason="attachment added")
    draft.attachment_file_ids = [*(draft.attachment_file_ids or []), str(file.id)]
    draft.version += 1
    audit.record(session, actor, "email_draft_attachment_added", target_type="email_draft", target_id=draft.ref,
                 details={"file": str(file.id), "name": file.original_name, "size": file.size_bytes})
    _sync_gmail_draft(session, draft)
    note = f" The pending approval {cancelled} was cancelled; send again after reviewing." if cancelled else ""
    names = ", ".join(file_label(f) for f in draft_files(session, draft))
    return f"📎 Attached to {draft.ref}: {names}.{note}\nReply SEND {draft.ref} when it looks right."


def detach_file(session: Session, user: User, draft_ref: str, which: str | None) -> str:
    draft = drafts_svc.get_by_ref(session, draft_ref, lock=True)
    if draft.status in ("sent", "discarded"):
        raise DraftError(f"{draft.ref} is {draft.status} and can't be changed.")
    current = draft_files(session, draft)
    if not current:
        raise DraftError(f"{draft.ref} has no attachments.")
    which = (which or "").strip()
    if not which:
        keep, removed = [], current
    elif which.isdigit() and 1 <= int(which) <= len(current):
        removed = [current[int(which) - 1]]
        keep = [f for f in current if f.id != removed[0].id]
    else:
        removed = [f for f in current if which.lower() in (f.original_name or "").lower()]
        if not removed:
            raise DraftError(f"{draft.ref} has no attachment matching \"{which}\". "
                             f"Attached: {', '.join(f.original_name or 'file' for f in current)}.")
        keep = [f for f in current if f not in removed]
    actor = Actor.user(user.id)
    cancelled = _withdraw_approval(session, draft, actor, "attachment removed")
    if draft.status in ("pending_approval", "send_failed"):
        drafts_svc.change_status(session, draft, "draft", actor, reason="attachment removed")
    draft.attachment_file_ids = [str(f.id) for f in keep]
    draft.version += 1
    audit.record(session, actor, "email_draft_attachment_removed", target_type="email_draft", target_id=draft.ref,
                 details={"files": [str(f.id) for f in removed]})
    _sync_gmail_draft(session, draft)
    note = f" The pending approval {cancelled} was cancelled." if cancelled else ""
    left = ", ".join(file_label(f) for f in keep) or "none"
    return f"📎 Removed from {draft.ref}: {', '.join(f.original_name or 'file' for f in removed)}. " \
           f"Still attached: {left}.{note}"


def _attachment_data(session: Session, draft: EmailDraft) -> list[tuple[str, str, bytes]]:
    store = files.get_store()
    out = []
    for f in draft_files(session, draft):
        out.append((f.original_name or f"attachment{files.ALLOWED.get(f.mime_type, '')}", f.mime_type,
                    store.get(f.storage_key)))
    return out


# ---------------- request ----------------
def request_draft(session: Session, user: User, thread: EmailThread, instructions: str) -> str:
    instructions = (instructions or "").strip()
    if not instructions:
        raise DraftError(f"What should the reply to {thread.ref} say?")
    default_recipients(session, thread)  # fail now if there's nobody to reply to
    if signature(session) is None:
        settings_store.set_value(session, PENDING_KEY, {"thread_id": str(thread.id), "instructions": instructions,
                                                         "user_id": str(user.id)}, Actor.user(user.id))
        return ("Before I write the first draft: what email signature should I use? Reply like "
                "\"remember my email signature is the owner, CEO, Kings Distro Inc., (804) …\" "
                f"and I'll write the {thread.ref} draft straight after.")
    queue.enqueue(session, "draft_email_reply", {"thread_id": str(thread.id), "instructions": instructions,
                                                 "user_id": str(user.id)}, uses_llm=True, max_attempts=3,
                  priority=20)
    return f"✍️ Writing a draft reply for {thread.ref} ({truncate(thread.subject, 60)}). Nothing will be sent."


def resume_pending_after_signature(session: Session) -> bool:
    pending = settings_store.get(session, PENDING_KEY)
    if not pending or signature(session) is None:
        return False
    queue.enqueue(session, "draft_email_reply", dict(pending), uses_llm=True, max_attempts=3, priority=20)
    settings_store.set_value(session, PENDING_KEY, None, AGENT)
    return True


def request_edit(session: Session, user: User, draft_ref: str, changes: str) -> str:
    draft = drafts_svc.get_by_ref(session, draft_ref, lock=True)
    if draft.status in ("sent", "discarded"):
        raise DraftError(f"{draft.ref} is {draft.status} and can't be edited.")
    if not (changes or "").strip():
        raise DraftError(f"What should I change in {draft.ref}?")
    actor = Actor.user(user.id)
    cancelled = _withdraw_approval(session, draft, actor, "draft edited")
    if draft.status in ("pending_approval", "send_failed"):
        drafts_svc.change_status(session, draft, "draft", actor, reason="edit requested")
    queue.enqueue(session, "draft_email_reply", {"draft_id": str(draft.id), "changes": changes.strip(),
                                                 "user_id": str(user.id)}, uses_llm=True, max_attempts=3,
                  priority=20)
    note = f" The pending approval {cancelled} was cancelled; send again after reviewing." if cancelled else ""
    return f"✏️ Revising {draft.ref}.{note}"


def discard(session: Session, user: User, draft_ref: str) -> str:
    draft = drafts_svc.get_by_ref(session, draft_ref, lock=True)
    if draft.status in ("sent", "discarded"):
        raise DraftError(f"{draft.ref} is already {draft.status}.")
    actor = Actor.user(user.id)
    _withdraw_approval(session, draft, actor, "draft discarded")
    drafts_svc.change_status(session, draft, "discarded", actor)
    if draft.gmail_draft_id:
        try:
            get_client(draft_account(session, draft)).delete_draft(draft.gmail_draft_id)
        except (GmailError, GmailRetryable) as exc:
            log.warning("could not delete gmail draft", extra={"error": str(exc)[:120]})
    return f"🗑️ {draft.ref} discarded. Nothing was sent."


def _withdraw_approval(session: Session, draft: EmailDraft, actor: Actor, reason: str) -> str | None:
    if not draft.approval_id:
        return None
    from app.db.models import Approval

    a = session.get(Approval, draft.approval_id)
    if a and approvals.cancel(session, a, actor, reason):
        return a.ref
    return None


# ---------------- generation (LLM job) ----------------
def _thread_text(session: Session, thread: EmailThread) -> str:
    msgs = list(session.execute(
        select(EmailMessage).where(EmailMessage.thread_id == thread.id)
        .order_by(EmailMessage.sent_at.desc().nulls_last())
    ).scalars())
    out, used = [], 0
    for m in msgs:
        who = "Kings Distro (the owner)" if m.is_from_owner else f"{m.from_name or ''} <{m.from_address}>"
        body = strip_quoted(m.body_text) or m.snippet
        block = f"--- {fmt_dt(m.sent_at)} from {who}\nSubject: {m.subject}\n{body}"
        if used + len(block) > THREAD_CHARS:
            block = block[: max(THREAD_CHARS - used, 0)]
        out.append(block)
        used += len(block)
        if used >= THREAD_CHARS:
            break
    return html.escape("\n\n".join(out), quote=False)


def _prompt(session: Session, thread: EmailThread, *, instructions: str, current: EmailDraft | None) -> str:
    esc = lambda s: html.escape(s or "", quote=False)  # noqa: E731
    to, _ = (current.to, current.cc) if current else default_recipients(session, thread)
    contact = session.execute(select(EmailContact).where(EmailContact.address == to[0])).scalar_one_or_none()
    mem = memory.active_for(session, "email")
    parts = [
        "<company_memory>\n" + ("\n".join(f"- {esc(m.title)}: {esc(truncate(m.content, 300))}" for m in mem
                                          if "signature" not in m.title.lower()) or "(none)") + "\n</company_memory>",
        f"<signature>\n{esc(signature(session) or '')}\n</signature>",
        f"<recipient>{esc(to[0])} (contact type: {esc(contact.type if contact else 'unknown')})</recipient>",
        f"<email_thread ref=\"{thread.ref}\" subject=\"{esc(thread.subject)}\">\n{_thread_text(session, thread)}\n</email_thread>",
    ]
    if current:
        parts.append(f"<current_draft version=\"{current.version}\">\nTo: {esc(', '.join(current.to))}\n"
                     f"Cc: {esc(', '.join(current.cc))}\nSubject: {esc(current.subject)}\n\n"
                     f"{esc(current.body_text)}\n</current_draft>")
        parts.append(f"<owner_revision_instructions>{esc(instructions)}</owner_revision_instructions>")
    else:
        parts.append(f"<owner_instructions>{esc(instructions)}</owner_instructions>")
    return "\n\n".join(parts)


def _prompt_new(session: Session, *, to: list[str], subject: str | None, instructions: str,
                current: EmailDraft | None) -> str:
    esc = lambda s: html.escape(s or "", quote=False)  # noqa: E731
    contact = session.execute(select(EmailContact).where(EmailContact.address == to[0])).scalar_one_or_none()
    mem = memory.active_for(session, "email")
    parts = [
        "<company_memory>\n" + ("\n".join(f"- {esc(m.title)}: {esc(truncate(m.content, 300))}" for m in mem
                                          if "signature" not in m.title.lower()) or "(none)") + "\n</company_memory>",
        f"<signature>\n{esc(signature(session) or '')}\n</signature>",
        f"<recipient>{esc(to[0])} (name: {esc(contact.name if contact and contact.name else 'unknown')}, "
        f"contact type: {esc(contact.type if contact else 'unknown')})</recipient>",
        f"<new_email subject=\"{esc(subject or '')}\">This is a new email, not a reply.</new_email>",
    ]
    if current:
        parts.append(f"<current_draft version=\"{current.version}\">\nTo: {esc(', '.join(current.to))}\n"
                     f"Cc: {esc(', '.join(current.cc))}\nSubject: {esc(current.subject)}\n\n"
                     f"{esc(current.body_text)}\n</current_draft>")
        parts.append(f"<owner_revision_instructions>{esc(instructions)}</owner_revision_instructions>")
    else:
        parts.append(f"<owner_instructions>{esc(instructions)}</owner_instructions>")
    return "\n\n".join(parts)


def _allowed_new_addresses(requested: list[str] | None, changes: str, current: list[str],
                           thread: EmailThread | None) -> tuple[list[str] | None, list[str]]:
    """Only accept recipients the owner typed himself (or already in the thread/draft)."""
    if requested is None:
        return None, []
    known = {a.lower() for a in (current or [])} | {a.lower() for a in ((thread.participants if thread else None) or [])}
    typed = {a.lower() for a in re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", changes or "")}
    ok, rejected = [], []
    for a in requested:
        a = a.strip().lower()
        if EMAIL_RE.match(a) and (a in typed or a in known):
            ok.append(a)
        else:
            rejected.append(a)
    return list(dict.fromkeys(ok)), rejected


def generate(session: Session, payload: dict) -> EmailDraft:
    user_id = uuid.UUID(payload["user_id"]) if payload.get("user_id") else None
    if payload.get("draft_id"):
        draft = session.get(EmailDraft, uuid.UUID(payload["draft_id"]))
        if draft is None or draft.status != "draft":
            raise NotFound("Draft no longer editable")
        thread = draft.thread
        prompt = (_prompt(session, thread, instructions=payload["changes"], current=draft) if thread else
                  _prompt_new(session, to=draft.to, subject=draft.subject, instructions=payload["changes"],
                              current=draft))
        output, _ = llm.complete_json(DraftOutput, system=load_prompt("email_draft"), prompt=prompt,
                                      purpose="email_draft_edit")
        to, bad_to = _allowed_new_addresses(output.to, payload["changes"], draft.to, thread)
        cc, bad_cc = _allowed_new_addresses(output.cc, payload["changes"], draft.cc, thread)
        notes = list(output.notes)
        if bad_to or bad_cc:
            notes.append("Ignored recipient changes you didn't type yourself: " + ", ".join(bad_to + bad_cc))
        draft.body_text = _ensure_signature(session, output.body_text)
        if output.subject:
            draft.subject = output.subject.strip()
        if to:
            draft.to = to
        if cc is not None:
            draft.cc = cc
        draft.notes = "\n".join(notes) or None
        draft.version += 1
        draft.owner_instructions = f"{draft.owner_instructions}\n[v{draft.version}] {payload['changes']}".strip()
        audit.record(session, AGENT, "email_draft_revised", target_type="email_draft", target_id=draft.ref,
                     details={"version": draft.version})
    elif payload.get("new"):
        to, cc = list(payload["to"]), list(payload.get("cc") or [])
        output, _ = llm.complete_json(DraftOutput, system=load_prompt("email_draft"),
                                      prompt=_prompt_new(session, to=to, subject=payload.get("subject"),
                                                         instructions=payload["instructions"], current=None),
                                      purpose="email_draft_new")
        subject = payload.get("subject") or (output.subject or "").strip() or truncate(payload["instructions"], 60)
        draft = EmailDraft(thread_id=None, requested_by_user_id=user_id, owner_instructions=payload["instructions"],
                           to=to, cc=cc, subject=subject[:300],
                           body_text=_ensure_signature(session, output.body_text),
                           notes="\n".join(output.notes) or None, status="draft", version=1)
        session.add(draft)
        session.flush()
        session.refresh(draft)
        audit.record(session, AGENT, "email_draft_created", target_type="email_draft", target_id=draft.ref,
                     details={"new_email": True, "to": to})
    else:
        thread = session.get(EmailThread, uuid.UUID(payload["thread_id"]))
        if thread is None:
            raise NotFound("Email thread not found")
        to, cc = default_recipients(session, thread)
        output, _ = llm.complete_json(DraftOutput, system=load_prompt("email_draft"),
                                      prompt=_prompt(session, thread, instructions=payload["instructions"],
                                                     current=None),
                                      purpose="email_draft")
        draft = EmailDraft(thread_id=thread.id, requested_by_user_id=user_id,
                           owner_instructions=payload["instructions"], to=to, cc=cc,
                           subject=reply_subject(thread.subject),
                           body_text=_ensure_signature(session, output.body_text),
                           notes="\n".join(output.notes) or None, status="draft", version=1)
        session.add(draft)
        session.flush()
        session.refresh(draft)
        audit.record(session, AGENT, "email_draft_created", target_type="email_draft", target_id=draft.ref,
                     details={"thread": thread.ref})
    _sync_gmail_draft(session, draft)
    return draft


def _ensure_signature(session: Session, body: str) -> str:
    sig = signature(session)
    body = body.rstrip()
    if sig and sig.splitlines()[0].strip() not in body[-(len(sig) + 200):]:
        body = f"{body}\n\n{sig}"
    return body


def _headers_for(session: Session, thread: EmailThread) -> tuple[str | None, str | None]:
    msg = last_message(session, thread)
    if msg is None:
        return None, None
    return msg.rfc_message_id, (msg.headers or {}).get("References")


def _sync_gmail_draft(session: Session, draft: EmailDraft) -> None:
    thread = draft.thread
    account = draft_account(session, draft)
    in_reply_to, references = _headers_for(session, thread) if thread else (None, None)
    raw = build_raw(from_address=account.address, to=draft.to, cc=draft.cc, subject=draft.subject,
                    body=draft.body_text, in_reply_to=in_reply_to, references=references,
                    attachments=_attachment_data(session, draft))
    provider_thread = thread.provider_thread_id if thread else None
    client = get_client(account)
    try:
        if draft.gmail_draft_id:
            client.update_draft(draft.gmail_draft_id, raw, provider_thread)
        else:
            draft.gmail_draft_id = client.create_draft(raw, provider_thread)
    except GmailAuthError as exc:
        handle_auth_failure(session, account, exc)
        draft.notes = ((draft.notes or "") + "\n(Not saved to Gmail Drafts: Gmail access needs re-connecting.)").strip()
    except (GmailError, GmailRetryable) as exc:
        log.warning("gmail draft save failed", extra={"error": str(exc)[:150]})
        draft.notes = ((draft.notes or "") + "\n(Not saved to Gmail Drafts this time.)").strip()


def draft_message(session: Session, draft: EmailDraft) -> str:
    lines = [f"✍️ Draft {draft.ref}, {where(draft)} (v{draft.version})",
             f"To: {', '.join(draft.to)}"]
    if draft.cc:
        lines.append(f"Cc: {', '.join(draft.cc)}")
    lines += [f"Subject: {draft.subject}", "", draft.body_text, ""]
    attached = draft_files(session, draft)
    if attached:
        lines.append("📎 " + ", ".join(f"{i}. {file_label(f)}" for i, f in enumerate(attached, start=1)))
        lines.append("")
    if draft.notes:
        lines.append("Notes:\n" + "\n".join(f"- {n}" for n in draft.notes.splitlines() if n.strip()))
        lines.append("")
    lines.append(f"Reply SEND {draft.ref} to request sending, EDIT {draft.ref} <changes> to revise, "
                 f"ATTACH {draft.ref} to add a file you sent me, or DISCARD {draft.ref}. Nothing has been sent.")
    return "\n".join(lines)


# ---------------- sending ----------------
def recipient_warnings(session: Session, draft: EmailDraft) -> list[str]:
    thread = draft.thread
    if thread is None:
        warnings = []
        for addr in [*draft.to, *draft.cc]:
            a = addr.lower()
            c = session.execute(select(EmailContact).where(EmailContact.address == a)).scalar_one_or_none()
            if c is None or c.message_count == 0:
                warnings.append(f"{a} has never emailed you; check the address")
            elif c.first_seen_at and c.first_seen_at > utcnow() - timedelta(days=WARNING_NEW_DAYS):
                warnings.append(f"{a} first emailed you in the last {WARNING_NEW_DAYS} days")
        return list(dict.fromkeys(warnings))
    participants = {a.lower() for a in thread.participants or []}
    original = last_inbound(session, thread)
    original_domain = original.from_address.split("@")[-1] if original else None
    warnings = []
    for addr in [*draft.to, *draft.cc]:
        a = addr.lower()
        if a not in participants:
            warnings.append(f"{a} is not part of this email thread")
        c = session.execute(select(EmailContact).where(EmailContact.address == a)).scalar_one_or_none()
        if c and c.first_seen_at and c.first_seen_at > utcnow() - timedelta(days=WARNING_NEW_DAYS):
            warnings.append(f"{a} first emailed you in the last {WARNING_NEW_DAYS} days")
        if original_domain and a.split("@")[-1] != original_domain:
            warnings.append(f"{a} is not at {original_domain} (the original sender's domain)")
    return list(dict.fromkeys(warnings))


def request_send(session: Session, user: User, draft_ref: str) -> str:
    draft = drafts_svc.get_by_ref(session, draft_ref, lock=True)
    if draft.status == "pending_approval" and draft.approval_id:
        from app.db.models import Approval

        a = session.get(Approval, draft.approval_id)
        if a and a.status == "pending" and a.expires_at > utcnow():
            return "Already waiting for approval:\n\n" + approvals.approval_message(a)
        drafts_svc.change_status(session, draft, "draft", Actor.user(user.id), reason="approval lapsed")
    if draft.status not in ("draft", "send_failed"):
        raise DraftError(f"{draft.ref} is {draft.status.replace('_', ' ')} and can't be sent.")
    thread = draft.thread
    account: EmailAccount = draft_account(session, draft)
    if account is None or account.status != "active":
        raise DraftError("Gmail isn't connected; nothing can be sent right now.")
    in_reply_to, references = _headers_for(session, thread) if thread else (None, None)
    warnings = recipient_warnings(session, draft)
    payload = {
        "account": account.address, "thread_provider_id": thread.provider_thread_id if thread else None,
        "thread_ref": thread.ref if thread else None,
        "draft_ref": draft.ref, "draft_version": draft.version, "to": draft.to, "cc": draft.cc,
        "subject": draft.subject, "body": draft.body_text, "in_reply_to": in_reply_to, "references": references,
        "attachments": [{"file_id": str(f.id), "name": f.original_name, "sha256": f.sha256, "size": f.size_bytes}
                        for f in draft_files(session, draft)],
    }
    summary_lines = [f"Send email {draft.ref} ({where(draft)}, v{draft.version})",
                     f"To: {', '.join(draft.to)}"]
    if draft.cc:
        summary_lines.append(f"Cc: {', '.join(draft.cc)}")
    summary_lines.append(f"Subject: {draft.subject}")
    summary_lines += [f"📎 {file_label(f)}" for f in draft_files(session, draft)]
    summary_lines += [f"⚠️ {w}" for w in warnings]
    summary_lines += ["--- Message ---", draft.body_text, "--- End ---"]
    approval = approvals.request_approval(
        session, action_type="email_send", payload=payload, summary="\n".join(summary_lines),
        risk_level="high" if warnings else "medium", requested_by_agent_key="email", notify=False,
    )
    draft.approval_id = approval.id
    drafts_svc.change_status(session, draft, "pending_approval", Actor.user(user.id))
    return approvals.approval_message(approval)


def _send_failed(session: Session, payload: dict, approval, error: str) -> None:
    draft = session.execute(select(EmailDraft).where(EmailDraft.ref == payload["draft_ref"])).scalar_one_or_none()
    if draft and draft.status == "pending_approval" and draft.approval_id == approval.id:
        drafts_svc.change_status(session, draft, "send_failed", AGENT, reason=error[:200])


@approvals.executor("email_send", on_failure=_send_failed)
def execute_send(session: Session, payload: dict, approval) -> dict:
    draft = session.execute(
        select(EmailDraft).where(EmailDraft.ref == payload["draft_ref"]).with_for_update(of=EmailDraft)
    ).scalar_one_or_none()
    if draft is None or draft.status != "pending_approval" or draft.approval_id != approval.id \
            or draft.version != payload["draft_version"]:
        raise DraftError("The draft changed after this approval was requested; nothing was sent")
    if settings_store.is_paused(session) or settings_store.get(session, "email_paused", False):
        raise DraftError("Email is paused; nothing was sent. After resuming, send again with SEND " + draft.ref)
    thread = draft.thread
    account = draft_account(session, draft)
    if account is None or account.status != "active" or account.address != payload["account"]:
        raise DraftError("Gmail isn't connected; nothing was sent")
    attachments = []
    for item in payload.get("attachments") or []:
        f = session.get(File, uuid.UUID(item["file_id"]))
        if f is None or f.sha256 != item["sha256"]:
            raise DraftError("An attachment changed after this approval was requested; nothing was sent")
        attachments.append((f.original_name or "attachment", f.mime_type, files.get_store().get(f.storage_key)))
    raw = build_raw(from_address=account.address, to=payload["to"], cc=payload["cc"], subject=payload["subject"],
                    body=payload["body"], in_reply_to=payload["in_reply_to"], references=payload["references"],
                    attachments=attachments)
    client = get_client(account)
    try:
        resp = client.send(raw, payload["thread_provider_id"])
    except GmailAuthError as exc:
        raise DraftError(f"Gmail access failed ({exc}); nothing was sent") from exc
    except GmailRetryable as exc:
        raise DraftError(f"Gmail was unavailable ({exc}); nothing was sent. Try SEND {draft.ref} again") from exc

    draft.sent_provider_message_id = resp.get("id")
    drafts_svc.change_status(session, draft, "sent", AGENT)
    if draft.gmail_draft_id:
        try:
            client.delete_draft(draft.gmail_draft_id)
        except (GmailError, GmailRetryable):
            pass
    who = ", ".join(payload["to"])
    if thread is None:  # new conversation: the next sync stores it and links this draft to the new thread
        return {"summary": f"new email {draft.ref} sent to {who}. Nothing else was sent.",
                "complete_task": False, "gmail_message_id": resp.get("id")}
    now = utcnow()
    thread.last_message_at = now
    thread.last_message_from_owner = True
    thread.needs_reply = False
    thread.status = "waiting_on_them"
    if thread.follow_up_tracking:
        days = int(settings_store.get(session, "email_followup_business_days", 3) or 3)
        thread.follow_up_due_at = followup.add_business_days(now, days)
    if thread.linked_task_id:
        task = session.get(Task, thread.linked_task_id)
        if task and task.status in ("waiting_owner", "queued", "blocked"):
            tasks.change_status(session, task, "in_progress", AGENT)
        if task and task.status == "in_progress":
            tasks.set_result(session, task, AGENT, f"Reply {draft.ref} sent")
            tasks.change_status(session, task, "done", AGENT)
    return {"summary": f"email {draft.ref} sent to {who} (thread {thread.ref}). Nothing else was sent.",
            "complete_task": False, "gmail_message_id": resp.get("id")}


def deliver_draft_message(session: Session, draft: EmailDraft) -> None:
    from app.services import users

    owner = users.get_owner(session)
    if owner:
        outbound.queue_text(session, owner, draft_message(session, draft), urgent=True)
