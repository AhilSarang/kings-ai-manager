"""Attaching files the owner sends on WhatsApp to an email draft, and sending them."""

from email import policy
from email import message_from_bytes
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import File
from app.email.fake_gmail import FakeGmailClient as G
from app.email.gmail import b64url_decode
from app.llm.fake import FakeLLMProvider
from tests.conftest import run_jobs
from tests.email.test_m6_drafting import (account, approval_code, client, draft_out, only_draft,  # noqa: F401
                                          say, signed, thread)
from tests.helpers import media_payload, post

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"x" * 300 + b"\n%%EOF\n"


def send_file(client, name: str, data: bytes, caption: str = "") -> None:
    inbox = Path(get_settings().file_storage_dir) / "_sim_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_bytes(data)
    kind = "document" if name.endswith(".pdf") else "image"
    mime = "application/pdf" if name.endswith(".pdf") else "image/png"
    assert post(client, media_payload(name, mime, caption=caption, kind=kind, filename=name)).status_code == 200
    run_jobs(limit=100)


def parts_of(raw: str):
    msg = message_from_bytes(b64url_decode(raw), policy=policy.default)
    return {p.get_filename(): p.get_payload(decode=True) for p in msg.iter_attachments()}


def test_attach_send_and_gmail_draft_carries_file(client, db, sent, thread, signed):
    say(client, sent, "DRAFT E-1 accept and attach the invoice", draft_out())
    send_file(client, "invoice.pdf", PDF)
    out = say(client, sent, "ATTACH D-1")
    assert "📎 Attached to D-1: invoice.pdf (0.0 MB)" in out
    d = only_draft(db)
    assert len(d.attachment_file_ids) == 1 and d.version == 2
    assert parts_of(G.drafts[d.gmail_draft_id]["raw"]) == {"invoice.pdf": PDF}
    assert "📎 1. invoice.pdf" in say(client, sent, "D-1")

    out = say(client, sent, "SEND D-1")
    a = approval_code(db)
    assert "📎 invoice.pdf" in out
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "sent to orders@pellon.com" in out
    assert parts_of(G.sent[0]["raw"]) == {"invoice.pdf": PDF}


def test_attach_with_caption_and_second_file(client, db, sent, thread, signed):
    say(client, sent, "DRAFT E-1 accept", draft_out())
    send_file(client, "photo.png", PNG, caption="attach to D-1")
    assert "📎 Attached to D-1: photo.png" in "\n".join(s.get("text", "") for s in sent)
    send_file(client, "invoice.pdf", PDF)
    say(client, sent, "attach that to D-1", {"actions": [{"type": "email_attach_file",
                                                          "params": {"draft_ref": "D-1"}}], "reply_to_owner": ""})
    d = only_draft(db)
    assert len(d.attachment_file_ids) == 2
    assert set(parts_of(G.drafts[d.gmail_draft_id]["raw"])) == {"photo.png", "invoice.pdf"}
    out = say(client, sent, "DETACH D-1 photo.png")
    assert "Removed from D-1: photo.png" in out and "Still attached: invoice.pdf" in out
    assert len(only_draft(db).attachment_file_ids) == 1


def test_attach_needs_a_file_and_respects_limits(client, db, sent, thread, signed, monkeypatch):
    say(client, sent, "DRAFT E-1 accept", draft_out())
    assert "Send me the file on WhatsApp first" in say(client, sent, "ATTACH D-1")
    send_file(client, "invoice.pdf", PDF)
    say(client, sent, "ATTACH D-1")
    assert "already attached" in say(client, sent, "ATTACH D-1")
    from app.email import drafting

    monkeypatch.setattr(drafting, "MAX_ATTACHMENT_BYTES", 100)
    send_file(client, "photo.png", PNG)
    assert "the limit is 0 MB" in say(client, sent, "ATTACH D-1")
    assert len(only_draft(db).attachment_file_ids) == 1


def test_attaching_cancels_a_pending_approval(client, db, sent, thread, signed):
    say(client, sent, "DRAFT E-1 accept", draft_out())
    say(client, sent, "SEND D-1")
    first = approval_code(db)
    send_file(client, "invoice.pdf", PDF)
    out = say(client, sent, "ATTACH D-1")
    assert f"approval {first.ref} was cancelled" in out
    db.expire_all()
    assert db.get(type(first), first.id).status == "rejected" and only_draft(db).status == "draft"
    say(client, sent, "SEND D-1")
    second = approval_code(db)
    assert second.id != first.id
    say(client, sent, f"APPROVE {second.ref} {second.confirmation_code}")
    assert parts_of(G.sent[0]["raw"]) == {"invoice.pdf": PDF}


def test_tampered_file_is_not_sent(client, db, sent, thread, signed):
    say(client, sent, "DRAFT E-1 accept", draft_out())
    send_file(client, "invoice.pdf", PDF)
    say(client, sent, "ATTACH D-1")
    say(client, sent, "SEND D-1")
    a = approval_code(db)
    f = db.execute(select(File).where(File.original_name == "invoice.pdf")).scalar_one()
    f.sha256 = "0" * 64  # as if the stored file changed after the owner approved it
    db.commit()
    out = say(client, sent, f"APPROVE {a.ref} {a.confirmation_code}")
    assert "attachment changed" in out and G.sent == []
