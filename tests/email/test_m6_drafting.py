import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Approval, EmailDraft, EmailThread, MemoryEntry, Task
from app.email.fake_gmail import FakeGmailClient as G
from app.email.gmail import GmailRetryable
from app.llm.fake import FakeLLMProvider
from app.services import memory, settings_store, tasks
from app.services.common import SYSTEM, Actor, utcnow
from tests.conftest import run_jobs
from tests.email.helpers import connect, receive
from tests.helpers import post, text_payload

SIG = "the owner\nCEO, Kings Distro Inc."


@pytest.fixture
def client(db, owner):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def account(db, owner):
    settings_store.set_value(db, "quiet_hours", None, SYSTEM)
    db.commit()
    return connect(db)


@pytest.fixture
def signed(db, owner):
    memory.create(db, category="owner_preference", title="Email signature", content=SIG, actor=Actor.user(owner.id))
    db.commit()


@pytest.fixture
def thread(db, account):
    m = receive(db, account, from_="Sam Rep <sam@pellon.com>", subject="Price increase", thread_id="PT",
                headers={"Cc": "ops@pellon.com, you@example.com", "Reply-To": "orders@pellon.com",
                         "References": "<first@pellon.com>"})
    receive(db, account, from_="Other <x@else.com>", subject="Unrelated secret deal", body="TOP-SECRET-OTHER")
    return m.thread


def say(client, sent, text, *llm):
    FakeLLMProvider.script(*llm)
    before = len(sent)
    assert post(client, text_payload(text)).status_code == 200
    run_jobs(limit=200)
    return "\n---\n".join(s.get("text", f"[{s['kind']}]") for s in sent[before:])


def draft_out(body="Hi Sam,\n\nWe accept the new price but need delivery by the 30th.", notes=(), **kw):
    return {"body_text": body, "notes": list(notes), "subject": kw.get("subject"), "to": kw.get("to"),
            "cc": kw.get("cc")}


def only_draft(db):
    db.expire_all()
    return db.execute(select(EmailDraft)).scalar_one()


# ---------- drafting ----------
def test_draft_asks_for_signature_first_then_continues(client, db, sent, thread):
    out = say(client, sent, "DRAFT E-1 tell them we accept the new price but need delivery by the 30th")
    assert "what email signature should I use" in out and db.execute(select(EmailDraft)).first() is None
    remember = {"actions": [{"type": "remember", "params": {"category": "owner_preference",
                                                            "title": "Email signature", "content": SIG,
                                                            "applies_to_agent_key": None}}], "reply_to_owner": "Saved."}
    out = say(client, sent, "remember my email signature is the owner, CEO", remember, draft_out())
    assert "writing the email draft you asked for now" in out
    assert "✍️ Draft D-1, reply in E-1 (v1)" in out and out.rstrip().endswith("Nothing has been sent.")
    assert settings_store.get(db, "email_pending_draft_request") is None


def test_draft_content_recipients_and_gmail_draft(client, db, sent, thread, signed):
    out = say(client, sent, "draft E-1: accept the price, need delivery by the 30th", draft_out(
        notes=["No PO number was given."]))
    d = only_draft(db)
    assert (d.to, d.cc, d.subject, d.status, d.version) == (["orders@pellon.com"], ["ops@pellon.com"],
                                                            "Re: Price increase", "draft", 1)
    assert d.body_text.endswith(SIG)  # signature added by code
    assert "To: orders@pellon.com" in out and "Cc: ops@pellon.com" in out and "- No PO number was given." in out
    gd = G.drafts[d.gmail_draft_id]
    assert gd["threadId"] == "PT" and gd["parsed"]["X-Kings-AI"] == "outbound"
    assert gd["parsed"]["In-Reply-To"].startswith("<") and "<first@pellon.com>" in gd["parsed"]["References"]
    assert G.sent == []
    prompt = FakeLLMProvider.requests[-1].prompt
    assert "<owner_instructions>accept the price, need delivery by the 30th</owner_instructions>" in prompt
    assert "TOP-SECRET-OTHER" not in prompt and "Unrelated secret deal" not in prompt


def test_natural_language_draft_resolves_thread(client, db, sent, thread, signed):
    req = {"actions": [{"type": "email_draft_request", "params": {
        "thread_ref": None, "sender": "pellon", "subject_contains": None,
        "instructions": "say we'll confirm next week"}}], "reply_to_owner": ""}
    out = say(client, sent, "draft a reply to the Pellon email saying we'll confirm next week", req, draft_out())
    assert "Writing a draft reply for E-1" in out and only_draft(db).thread.ref == "E-1"
    assert "<recent_email>" in FakeLLMProvider.requests[0].prompt


def test_ambiguous_thread_asks_which(client, db, sent, account, signed):
    receive(db, account, from_="a@pellon.com", subject="One")
    receive(db, account, from_="b@pellon.com", subject="Two")
    req = {"actions": [{"type": "email_draft_request", "params": {
        "thread_ref": None, "sender": "pellon", "subject_contains": None, "instructions": "thanks"}}],
        "reply_to_owner": ""}
    out = say(client, sent, "reply to pellon saying thanks", req)
    assert "Several emails match" in out and "E-1" in out and "E-2" in out
    assert db.execute(select(EmailDraft)).first() is None


def test_thread_without_inbound_message(client, db, sent, account, signed):
    receive(db, account, from_="you@example.com", labels=["SENT"], subject="Outbound only")
    out = say(client, sent, "DRAFT E-1 follow up")
    assert "has no message from the other side" in out
    assert db.execute(select(EmailDraft)).first() is None


def test_draft_job_error_tells_owner_once(db, owner, thread, signed, sent):
    from app.jobs import queue

    owner.last_inbound_at = utcnow()
    queue.enqueue(db, "draft_email_reply", {"thread_id": "00000000-0000-0000-0000-000000000000",
                                            "instructions": "x", "user_id": str(owner.id)}, uses_llm=True)
    db.commit()
    run_jobs(limit=50)
    assert [s["text"] for s in sent] == ["⚠️ I couldn't write that email draft this time. Please ask again."]


def test_edit_applies_changes_and_guards_recipients(client, db, sent, thread, signed):
    say(client, sent, "DRAFT E-1 accept", draft_out())
    out = say(client, sent, "EDIT D-1 make it shorter and cc buyer@kingsdistro.com",
              draft_out(body="Hi Sam, accepted.", cc=["ops@pellon.com", "buyer@kingsdistro.com", "attacker@evil.com"],
                        to=["hacker@evil.com"]))
    d = only_draft(db)
    assert d.version == 2 and d.body_text.startswith("Hi Sam, accepted.")
    assert d.to == ["orders@pellon.com"] and d.cc == ["ops@pellon.com", "buyer@kingsdistro.com"]
    assert "Ignored recipient changes" in d.notes and "attacker@evil.com" in d.notes
    assert "(v2)" in out and G.drafts[d.gmail_draft_id]["parsed"]["Cc"] == "ops@pellon.com, buyer@kingsdistro.com"


# ---------- sending ----------
def approval_code(db):
    db.expire_all()
    return db.execute(select(Approval).order_by(Approval.created_at.desc())).scalars().first()


def test_full_send_flow_exactly_once(client, db, sent, thread, signed, owner):
    t = tasks.create_task(db, title="Reply to E-1", actor=Actor.agent("email"), agent_key="email")
    tasks.change_status(db, t, "waiting_owner", Actor.agent("email"))
    thread.linked_task_id = t.id
    db.commit()
    say(client, sent, "DRAFT E-1 accept", draft_out())
    out = say(client, sent, "SEND D-1")
    a = approval_code(db)
    assert a.action_type == "email_send" and a.status == "pending"
    assert "To: orders@pellon.com" in out and "Cc: ops@pellon.com" in out and "We accept the new price" in out
    assert f"APPROVE {a.ref} {a.confirmation_code}" in out
    assert only_draft(db).status == "pending_approval"

    for word in ("ok", "send it", "yes"):
        assert "Nothing was approved" in say(client, sent, word)
    assert G.sent == []

    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "executed" in out and "sent to orders@pellon.com" in out
    assert len(G.sent) == 1
    s = G.sent[0]
    assert s["threadId"] == "PT" and s["parsed"]["To"] == "orders@pellon.com"
    assert s["parsed"]["X-Kings-AI"] == "outbound" and s["parsed"]["In-Reply-To"]
    d = only_draft(db)
    assert d.status == "sent" and d.sent_provider_message_id == s["id"] and d.gmail_draft_id not in G.drafts
    th = db.get(EmailThread, thread.id)
    assert th.status == "waiting_on_them" and th.follow_up_due_at > utcnow() and th.last_message_from_owner
    assert db.get(Task, t.id).status == "done"

    assert "already executed" in say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "can't be sent" in say(client, sent, "SEND D-1")
    run_jobs(limit=50)
    assert len(G.sent) == 1


def test_send_it_without_approval_points_to_send_command(client, db, sent, thread, signed):
    say(client, sent, "DRAFT E-1 accept", draft_out())
    out = say(client, sent, "send it")
    assert "Nothing was sent" in out and "SEND D-1" in out and G.sent == []


def test_new_recipient_warning(client, db, sent, thread, signed):
    say(client, sent, "DRAFT E-1 accept", draft_out())
    say(client, sent, "EDIT D-1 also send to boss@other-company.com",
        draft_out(to=["orders@pellon.com", "boss@other-company.com"]))
    out = say(client, sent, "SEND D-1")
    assert "⚠️ boss@other-company.com is not part of this email thread" in out
    assert "not at pellon.com" in out and approval_code(db).risk_level == "high"


def test_reply_to_address_outside_thread_is_flagged(client, db, sent, thread, signed):
    say(client, sent, "DRAFT E-1 accept", draft_out())
    out = say(client, sent, "SEND D-1")
    # The draft replies to the Reply-To address, which never wrote in the thread itself
    assert "orders@pellon.com is not part of this email thread" in out


def test_recently_seen_contact_warning(client, db, sent, account, signed):
    receive(db, account, from_="new@freshvendor.com", subject="Hello", thread_id="NV")
    say(client, sent, "DRAFT E-1 thanks", draft_out())
    out = say(client, sent, "SEND D-1")
    assert "new@freshvendor.com first emailed you in the last 7 days" in out


def test_edit_after_send_request_cancels_approval(client, db, sent, thread, signed):
    say(client, sent, "DRAFT E-1 accept", draft_out())
    say(client, sent, "SEND D-1")
    a = approval_code(db)
    out = say(client, sent, "EDIT D-1 add a thank you", draft_out(body="Thanks! Accepted."))
    assert f"approval {a.ref} was cancelled" in out
    assert approval_code(db).status == "rejected" and only_draft(db).status == "draft"
    assert "already rejected" in say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert G.sent == []
    say(client, sent, "SEND D-1")
    a2 = approval_code(db)
    say(client, sent, f"APPROVE {a2.ref} {a2.confirmation_code}")
    assert len(G.sent) == 1 and "Thanks! Accepted." in G.sent[0]["parsed"].get_content()


def test_send_failure_marks_draft_and_can_retry(client, db, sent, thread, signed, monkeypatch):
    from app.email import fake_gmail

    say(client, sent, "DRAFT E-1 accept", draft_out())
    say(client, sent, "SEND D-1")
    a = approval_code(db)
    orig = fake_gmail.FakeGmailClient.send

    def down(self, raw, tid):
        raise GmailRetryable("503 backend error")

    monkeypatch.setattr(fake_gmail.FakeGmailClient, "send", down)
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "failed to execute" in out and "nothing was sent" in out
    assert only_draft(db).status == "send_failed" and approval_code(db).status == "execution_failed"
    monkeypatch.setattr(fake_gmail.FakeGmailClient, "send", orig)
    say(client, sent, "SEND D-1")
    a2 = approval_code(db)
    say(client, sent, f"APPROVE {a2.ref} {a2.confirmation_code}")
    assert len(G.sent) == 1 and only_draft(db).status == "sent"


def test_email_pause_blocks_send(client, db, sent, thread, signed):
    say(client, sent, "DRAFT E-1 accept", draft_out())
    say(client, sent, "SEND D-1")
    a = approval_code(db)
    settings_store.set_value(db, "email_paused", True, SYSTEM)
    db.commit()
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "Email is paused" in out and G.sent == [] and only_draft(db).status == "send_failed"


def test_discard(client, db, sent, thread, signed):
    say(client, sent, "DRAFT E-1 accept", draft_out())
    gid = only_draft(db).gmail_draft_id
    say(client, sent, "SEND D-1")
    out = say(client, sent, "DISCARD D-1")
    assert "discarded" in out and only_draft(db).status == "discarded" and gid not in G.drafts
    assert approval_code(db).status == "rejected"
    assert "can't be sent" in say(client, sent, "SEND D-1")


def test_payload_tamper_blocked(client, db, sent, thread, signed):
    from sqlalchemy import text

    say(client, sent, "DRAFT E-1 accept", draft_out())
    say(client, sent, "SEND D-1")
    a = approval_code(db)
    db.execute(text("UPDATE approvals SET action_payload = jsonb_set(action_payload, '{to}', "
                    "'[\"thief@evil.com\"]') WHERE id = :id"), {"id": a.id})
    db.commit()
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "Payload changed" in out and G.sent == []


def test_stale_approval_after_version_change(db, owner, thread, signed):
    from app.email import drafting
    from app.services import approvals

    FakeLLMProvider.script(draft_out())
    d = drafting.generate(db, {"thread_id": str(thread.id), "instructions": "x", "user_id": str(owner.id)})
    drafting.request_send(db, owner, d.ref)
    a = db.get(Approval, d.approval_id)
    approvals.approve(db, owner, a.ref, a.confirmation_code)
    d.version += 1  # simulate a change that slipped past the edit flow
    db.commit()
    assert approvals.execute(db, a.id) == "failed" and G.sent == []


def test_unknown_draft_ref(client, db, sent, account):
    assert "can't find draft D-9" in say(client, sent, "SEND D-9")


def test_draft_request_expires_pending_signature_overwritten(db, owner, thread):
    from app.email import drafting

    drafting.request_draft(db, owner, thread, "first")
    drafting.request_draft(db, owner, thread, "second")
    assert settings_store.get(db, "email_pending_draft_request")["instructions"] == "second"
    assert db.execute(select(MemoryEntry)).first() is None
