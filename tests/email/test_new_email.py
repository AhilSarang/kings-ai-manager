"""Brand-new emails: drafted on request, recipients typed by the owner, sent only after approval."""

from sqlalchemy import select

from app.db.models import EmailDraft, EmailThread
from app.email.fake_gmail import FakeGmailClient as G
from app.email.sync import ingest_message
from app.llm.fake import FakeLLMProvider
from tests.email.helpers import receive
from tests.email.test_m6_drafting import (SIG, account, approval_code, client, draft_out, only_draft,  # noqa: F401
                                         say, signed)


def new_out(body="Hi Sam,\n\nCould you send your fall price list?", subject="Fall price list request", **kw):
    return {**draft_out(body, **kw), "subject": subject}


def test_new_email_command_draft_and_send(client, db, sent, account, signed):
    out = say(client, sent, "NEW EMAIL sam@pellon.com ask for the fall price list", new_out())
    assert "new email to sam@pellon.com" in out
    assert "Draft D-1, new email (v1)" in out and "Subject: Fall price list request" in out
    assert SIG.splitlines()[0] in out and "Nothing has been sent" in out
    prompt = FakeLLMProvider.requests[-1].prompt
    assert "<new_email" in prompt and "fall price list" in prompt and "<email_thread" not in prompt
    d = only_draft(db)
    assert d.thread_id is None and d.to == ["sam@pellon.com"] and d.status == "draft"
    assert G.drafts[d.gmail_draft_id]["threadId"] is None

    out = say(client, sent, "SEND D-1")
    a = approval_code(db)
    assert "new email" in out and "sam@pellon.com has never emailed you" in out and a.risk_level == "high"
    assert G.sent == []
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "new email D-1 sent to sam@pellon.com" in out
    [s] = G.sent
    assert s["threadId"] is None and s["parsed"]["To"] == "sam@pellon.com"
    assert s["parsed"]["Subject"] == "Fall price list request" and s["parsed"]["In-Reply-To"] is None
    assert only_draft(db).status == "sent"

    # The next sync stores the sent message and links the draft to the new thread.
    raw = G.add_message(from_="you@example.com", to="sam@pellon.com", subject="Fall price list request",
                        labels=["SENT"], message_id=s["id"])
    ingest_message(db, account, raw)
    db.commit()
    d = only_draft(db)
    thread = db.get(EmailThread, d.thread_id)
    assert thread.status == "waiting_on_them" and thread.follow_up_due_at is not None


def test_plain_language_new_email_to_known_contact(client, db, sent, account, signed):
    receive(db, account, from_="Sam Rep <sam@pellon.com>", subject="Hello", age_seconds=30 * 86400)
    out = say(client, sent, "write an email to Sam at Pellon asking for the fall price list",
              {"actions": [{"type": "email_new_draft", "params": {
                  "to": ["sam@pellon.com"], "instructions": "ask for the fall price list"}}],
               "reply_to_owner": ""},
              new_out())
    assert "Writing a new email to sam@pellon.com" in out
    d = only_draft(db)
    assert d.to == ["sam@pellon.com"] and d.thread_id is None
    out = say(client, sent, "SEND D-1")
    assert "⚠️" not in out and approval_code(db).risk_level == "medium", out


def test_new_email_recipient_must_come_from_owner(client, db, sent, account, signed):
    # A model inventing an address the owner never typed (and nobody has emailed from) is refused.
    out = say(client, sent, "email Sam about the price list",
              {"actions": [{"type": "email_new_draft", "params": {
                  "to": ["sam@evil.io"], "instructions": "price list"}}], "reply_to_owner": ""})
    assert "give the full address" in out and "sam@evil.io" in out
    # A known contact that the owner didn't name is refused too.
    receive(db, account, from_="Joe <joe@uline.com>", subject="Hi")
    out = say(client, sent, "email Sam about the price list",
              {"actions": [{"type": "email_new_draft", "params": {
                  "to": ["joe@uline.com"], "instructions": "price list"}}], "reply_to_owner": ""})
    assert "give the full address" in out
    assert db.execute(select(EmailDraft)).first() is None


def test_edit_new_email_and_recipient_guard(client, db, sent, account, signed):
    say(client, sent, "new email sam@pellon.com: ask for the fall price list", new_out())
    say(client, sent, "EDIT D-1 also cc amy@pellon.com and make it shorter",
        new_out(body="Hi Sam, fall price list please?", subject=None, cc=["amy@pellon.com", "x@evil.io"]))
    d = only_draft(db)
    assert d.cc == ["amy@pellon.com"] and d.version == 2 and "x@evil.io" in d.notes
    assert d.subject == "Fall price list request"
    assert "Draft D-1, new email (v2)" in say(client, sent, "D-1")


def test_new_email_needs_signature_first(client, db, sent, account):
    out = say(client, sent, "NEW EMAIL sam@pellon.com ask for the price list")
    assert "what email signature" in out and db.execute(select(EmailDraft)).first() is None
    out = say(client, sent, "remember my email signature is the owner",
              {"actions": [{"type": "remember", "params": {"category": "owner_preference",
                                                           "title": "Email signature", "content": "the owner"}}],
               "reply_to_owner": "Saved."},
              new_out())
    d = only_draft(db)
    assert d.thread_id is None and d.to == ["sam@pellon.com"]


def test_new_email_discard(client, db, sent, account, signed):
    say(client, sent, "NEW EMAIL sam@pellon.com hello", new_out())
    assert "discarded" in say(client, sent, "DISCARD D-1")
    assert G.drafts == {} and G.sent == []
    assert "no open drafts" in say(client, sent, "DRAFTS").lower()


def test_open_new_email_draft_does_not_break_the_supervisor(client, db, sent, account, signed):
    say(client, sent, "NEW EMAIL sam@pellon.com ask for the fall price list", new_out())
    out = say(client, sent, "what's going on?", {"actions": [{"type": "no_op", "params": {}}],
                                                "reply_to_owner": "All quiet."})
    assert "All quiet." in out
    prompt = FakeLLMProvider.requests[-1].prompt
    assert "draft D-1 (new email) [draft] to sam@pellon.com" in prompt


def test_edit_a_new_email_draft(client, db, sent, account, signed):
    say(client, sent, "NEW EMAIL sam@pellon.com ask for the fall price list", new_out())
    out = say(client, sent, "write a longer and more respectful email",
              {"actions": [{"type": "email_edit_draft", "params": {
                  "draft_ref": "D-1", "changes": "longer and more respectful"}}], "reply_to_owner": ""},
              new_out(body="Dear Sam,\n\nI hope you are well. Could you kindly send the fall price list?"))
    assert "Revising D-1" in out
    d = only_draft(db)
    assert d.version == 2 and "Dear Sam" in d.body_text


def test_loosely_typed_address_is_accepted(client, db, sent, account, signed):
    out = say(client, sent, "email shivali.chatlap @gmail. com write a long sweet letter",
              {"actions": [{"type": "email_new_draft", "params": {
                  "to": ["shivali.chatlap@gmail.com"], "instructions": "long sweet letter"}}],
               "reply_to_owner": "On it."},
              new_out(subject="Hello"))
    assert "Writing a new email to shivali.chatlap@gmail.com" in out
    assert only_draft(db).to == ["shivali.chatlap@gmail.com"]


def test_refusal_replaces_the_models_optimistic_reply(client, db, sent, account, signed):
    out = say(client, sent, "email my friend about the party",
              {"actions": [{"type": "email_new_draft", "params": {
                  "to": ["guessed@nowhere.com"], "instructions": "party"}}],
               "reply_to_owner": "Drafting it now to guessed@nowhere.com!"})
    assert "Drafting it now" not in out and out.startswith("⚠️ Nothing drafted")
    assert db.execute(select(EmailDraft)).first() is None
