from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.channels.base import ChannelError
from app.channels.console import ConsoleChannel
from app.channels.meta_whatsapp import MetaWhatsAppChannel, parse_meta_payload
from app.config import get_settings
from app.db.seed_data import DEFAULT_SETTINGS
from app.db.models import AuditLog, File, Job, Message
from app.services import files, health, monitor, outbound, settings_store
from app.services.common import SYSTEM, Actor, utcnow
from app.services.notify import notify_owner
from tests.conftest import run_jobs
from tests.helpers import media_payload, post, status_payload, text_payload

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"x" * 64


@pytest.fixture
def client(db, owner):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _jobs(db, type_=None):
    db.expire_all()
    q = select(Job)
    if type_:
        q = q.where(Job.type == type_)
    return db.execute(q).scalars().all()


def _inbound(db):
    db.expire_all()
    return db.execute(select(Message).where(Message.direction == "inbound")).scalars().all()


# ---------- webhook verification ----------
def test_verify_handshake(client):
    r = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "test-verify-token",
                                                 "hub.challenge": "12345"})
    assert r.status_code == 200 and r.text == "12345"
    r = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                                 "hub.challenge": "12345"})
    assert r.status_code == 403


@pytest.mark.parametrize("sig", [None, "sha256=deadbeef", "garbage"])
def test_bad_signature_rejected(client, db, sig):
    r = post(client, text_payload("hi"), signature=sig)
    assert r.status_code == 403
    assert _inbound(db) == []


def test_owner_message_stored_and_enqueued(client, db, owner):
    r = post(client, text_payload("Find suppliers", msg_id="wamid.A1"))
    assert r.status_code == 200 and r.json()["accepted"] == 1
    [msg] = _inbound(db)
    assert msg.body == "Find suppliers" and msg.user_id == owner.id and msg.status == "received"
    assert msg.provider_message_id == "wamid.A1"
    types = sorted(j.type for j in _jobs(db))
    assert types == ["process_inbound_message", "release_held_messages"]
    db.refresh(owner)
    assert owner.last_inbound_at is not None
    assert db.execute(select(AuditLog).where(AuditLog.action == "message_received")).first()


def test_duplicate_delivery_ignored(client, db):
    post(client, text_payload("once", msg_id="wamid.D1"))
    r = post(client, text_payload("once", msg_id="wamid.D1"))
    assert r.json()["duplicates"] == 1
    assert len(_inbound(db)) == 1
    assert len(_jobs(db, "process_inbound_message")) == 1


def test_unknown_sender_not_processed_but_audited(client, db, sent):
    """Acceptance 7."""
    r = post(client, text_payload("hello", sender="+15550009999"))
    assert r.status_code == 200 and r.json()["rejected"] == 1
    assert _inbound(db) == []
    assert _jobs(db) == []
    entry = db.execute(select(AuditLog).where(AuditLog.action == "inbound_rejected")).scalar_one()
    assert entry.details["raw"]["text"]["body"] == "hello"
    assert sent == []


def test_inactive_owner_is_rejected(client, db, owner):
    owner.is_active = False
    db.commit()
    assert post(client, text_payload("hi")).json()["rejected"] == 1


def test_status_callbacks_update_outbound(client, db, owner):
    row = Message(direction="outbound", user_id=owner.id, body="x", status="sent", provider_message_id="wamid.OUT")
    db.add(row)
    db.commit()
    post(client, status_payload("wamid.OUT", "read"))
    post(client, status_payload("wamid.OUT", "delivered"))  # arrives late: no downgrade
    db.refresh(row)
    assert row.status == "read"
    post(client, status_payload("wamid.UNKNOWN", "read"))  # ignored


def test_parse_interactive_and_media():
    payload = media_payload("MID", "application/pdf", caption="invoice", kind="document", filename="inv.pdf")
    [m] = parse_meta_payload(payload).messages
    assert (m.type, m.media_id, m.filename, m.text) == ("document", "MID", "inv.pdf", "invoice")
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": "1", "id": "x", "type": "interactive",
        "interactive": {"button_reply": {"id": "b", "title": "Yes"}}}]}}]}]}
    [m] = parse_meta_payload(payload).messages
    assert (m.type, m.text) == ("text", "Yes")


# ---------- media ----------
def _sim_media(name: str, data: bytes) -> str:
    inbox = Path(get_settings().file_storage_dir) / "_sim_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_bytes(data)
    return name


def test_media_downloaded_saved_and_then_processed(client, db, owner):
    media_id = _sim_media("shot.png", PNG)
    post(client, media_payload(media_id, "image/png", caption="attach this to T-5"))
    assert [j.type for j in _jobs(db, "download_media")] == ["download_media"]
    assert _jobs(db, "process_inbound_message") == []
    assert run_jobs(["download_media"]) == 1
    [msg] = _inbound(db)
    f = db.get(File, msg.media_file_id)
    assert f.mime_type == "image/png" and f.source == "whatsapp" and f.size_bytes == len(PNG)
    assert files.get_store().get(f.storage_key) == PNG
    assert f.storage_key.endswith(".png") and "shot" not in f.storage_key  # random key
    assert len(_jobs(db, "process_inbound_message")) == 1


def test_bad_media_rejected_but_message_still_processed(client, db):
    media_id = _sim_media("evil.png", b"MZ\x90\x00 not really a png")
    post(client, media_payload(media_id, "image/png"))
    run_jobs(["download_media"])
    [msg] = _inbound(db)
    assert msg.media_file_id is None
    assert "not accepted" in msg.raw_payload["media_error"]
    assert len(_jobs(db, "process_inbound_message")) == 1


# ---------- file validation ----------
@pytest.mark.parametrize("data,mime", [
    (PNG, "image/png"), (PDF, "application/pdf"), (b"a,b\n1,2\n", "text/csv"),
    (b"PK\x03\x04" + b"0" * 20, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    (b"\xff\xd8\xff\xe0" + b"0" * 20, "image/jpeg; charset=binary"),
])
def test_allowed_files(data, mime):
    assert files.verify_mime(data, mime) == mime.split(";")[0]


@pytest.mark.parametrize("data,mime", [
    (PNG, "application/pdf"),           # mismatch
    (b"#!/bin/sh\nrm -rf /", "text/plain"),
    (b"\x7fELF....", "application/pdf"),
    (PDF, "application/x-msdownload"),  # not allowed
    (b"\xff\xfe\x00bad", "text/plain"),  # not utf-8
])
def test_rejected_files(data, mime):
    with pytest.raises(files.FileRejected):
        files.verify_mime(data, mime)


def test_size_cap_and_empty(db, monkeypatch):
    monkeypatch.setattr(type(get_settings()), "max_upload_bytes", property(lambda self: 10))
    with pytest.raises(files.FileRejected, match="larger"):
        files.save_file(db, PNG, mime_type="image/png", source="upload", actor=SYSTEM)
    with pytest.raises(files.FileRejected, match="empty"):
        files.save_file(db, b"", mime_type="image/png", source="upload", actor=SYSTEM)


def test_store_blocks_path_traversal(tmp_path):
    store = files.LocalFileStore(tmp_path)
    with pytest.raises(files.FileRejected):
        store.put("../../etc/passwd", b"x")


def test_attach_file_to_task(db, owner):
    from app.services import tasks

    t = tasks.create_task(db, title="x", actor=Actor.user(owner.id))
    f = files.save_file(db, PNG, mime_type="image/png", source="whatsapp", actor=SYSTEM)
    files.attach_to_task(db, f, t, Actor.user(owner.id))
    assert f.related_task_id == t.id
    assert "file_attached" in [e.event_type for e in tasks.recent_events(db, t)]


# ---------- outbound ----------
def test_split_text():
    assert outbound.split_text("short") == ["short"]
    assert outbound.split_text("  ") == []
    long = "\n\n".join(f"Paragraph {i} " + "word " * 200 for i in range(10))
    parts = outbound.split_text(long)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    assert parts[0].endswith(f"(1/{len(parts)})")
    no_breaks = "x" * 9000
    assert all(len(p) <= 4096 for p in outbound.split_text(no_breaks))


@pytest.mark.parametrize("hour,expected", [(23, True), (2, True), (6, True), (7, False), (12, False), (21, False)])
def test_quiet_hours_cross_midnight(db, hour, expected):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    settings_store.set_value(db, "quiet_hours", DEFAULT_SETTINGS["quiet_hours"], SYSTEM)
    now = datetime(2026, 9, 16, hour, 30, tzinfo=ZoneInfo("America/New_York"))
    assert outbound.in_quiet_hours(db, now) is expected


def test_quiet_hours_same_day_window_and_disabled(db):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    settings_store.set_value(db, "quiet_hours", {"start": "12:00", "end": "13:00"}, SYSTEM)
    noon = datetime(2026, 9, 16, 12, 15, tzinfo=ZoneInfo("America/New_York"))
    assert outbound.in_quiet_hours(db, noon)
    settings_store.set_value(db, "quiet_hours", None, SYSTEM)
    assert not outbound.in_quiet_hours(db, noon)


def _open_window(db, owner):
    owner.last_inbound_at = utcnow()
    db.commit()


def test_send_within_window(db, owner, sent, no_quiet_hours):
    _open_window(db, owner)
    outbound.queue_text(db, owner, "Logged as T-1")
    db.commit()
    run_jobs(["send_messages"])
    assert [(s["kind"], s["text"]) for s in sent] == [("text", "Logged as T-1")]
    row = db.execute(select(Message).where(Message.direction == "outbound")).scalar_one()
    db.refresh(row)
    assert row.status == "sent" and row.provider_message_id.startswith("console-")
    assert db.execute(select(AuditLog).where(AuditLog.action == "message_sent")).first()


def test_notify_owner_chain(db, owner, sent, no_quiet_hours):
    _open_window(db, owner)
    assert notify_owner(db, "hello", key="k") is True
    assert notify_owner(db, "hello", key="k") is False
    db.commit()
    run_jobs(["send_owner_message", "send_messages"])
    assert [s["text"] for s in sent] == ["hello"]


def test_outside_window_holds_and_sends_one_template(client, db, owner, sent, no_quiet_hours):
    owner.last_inbound_at = utcnow() - timedelta(hours=30)
    db.commit()
    outbound.queue_text(db, owner, "update 1")
    outbound.queue_text(db, owner, "update 2")
    db.commit()
    run_jobs(["send_messages"])
    assert [s["kind"] for s in sent] == ["template"]
    assert sent[0]["params"] == ["1", "your tasks"]  # sent when the first update is held
    held = db.execute(select(Message).where(Message.status == "held")).scalars().all()
    assert len(held) == 2 and all(m.raw_payload["hold_reason"] == "window" for m in held)
    # the owner replies -> window opens -> held updates delivered in order
    post(client, text_payload("ok show me"))
    run_jobs(["release_held_messages", "send_messages"])
    assert [s.get("text") for s in sent[1:]] == ["update 1", "update 2"]


def test_channel_outside_window_error_falls_back_to_template(db, owner, sent, no_quiet_hours):
    _open_window(db, owner)
    ConsoleChannel.fail_next.append(ChannelError("re-engagement", retryable=False, outside_window=True))
    outbound.queue_text(db, owner, "update")
    db.commit()
    run_jobs(["send_messages"])
    assert [s["kind"] for s in sent] == ["template"]
    assert db.execute(select(Message.status).where(Message.body == "update")).scalar_one() == "held"


def test_quiet_hours_hold_non_urgent_but_send_urgent(db, owner, sent):
    _open_window(db, owner)
    settings_store.set_value(db, "quiet_hours", {"start": "00:00", "end": "23:59"}, SYSTEM)
    outbound.queue_text(db, owner, "fyi")
    outbound.queue_text(db, owner, "urgent!", urgent=True)
    db.commit()
    run_jobs(["send_messages"])
    assert [s["text"] for s in sent] == ["urgent!"]
    assert db.execute(select(Message.raw_payload).where(Message.body == "fyi")).scalar_one()["hold_reason"] == \
        "quiet_hours"
    assert outbound.release_held(db, owner, ("quiet_hours",)) == 1
    db.commit()
    run_jobs(["send_messages"])
    assert [s["text"] for s in sent] == ["urgent!", "fyi"]


def test_rate_limit_holds_and_alerts_once(db, owner, sent, no_quiet_hours, monkeypatch):
    monkeypatch.setattr(get_settings(), "outbound_max_per_10min", 3)
    _open_window(db, owner)
    for i in range(6):
        outbound.queue_text(db, owner, f"m{i}")
    db.commit()
    run_jobs(["send_messages"])
    texts = [s["text"] for s in sent]
    assert texts[:3] == ["m0", "m1", "m2"]
    assert sum("Safety limit" in t for t in texts) == 1
    assert len(sent) == 4
    assert db.execute(select(Message).where(Message.status == "held")).scalars().all().__len__() == 3


def test_retry_does_not_resend_sent_chunks(db, owner, sent, no_quiet_hours):
    _open_window(db, owner)
    text = "\n\n".join("para " * 700 for _ in range(3))
    rows = outbound.queue_text(db, owner, text)
    db.commit()
    assert len(rows) >= 2

    class FlakyOnSecond(ConsoleChannel):
        calls = 0

        def send_text(self, to, text):
            FlakyOnSecond.calls += 1
            if FlakyOnSecond.calls == 2:
                raise ChannelError("503", retryable=True)
            return super().send_text(to, text)

    import app.services.outbound as ob

    orig = ob.get_channel
    ob.get_channel = lambda: FlakyOnSecond()
    try:
        run_jobs(["send_messages"])
        job = _jobs(db, "send_messages")[0]
        assert job.status == "pending" and job.attempts == 1
        job.run_after = utcnow()
        db.commit()
        run_jobs(["send_messages"])
    finally:
        ob.get_channel = orig
    assert len(sent) == len(rows)
    assert [s["text"] for s in sent] == [r.body for r in rows]


def test_permanent_send_error_marks_failed(db, owner, sent, no_quiet_hours):
    _open_window(db, owner)
    ConsoleChannel.fail_next.append(ChannelError("bad number", retryable=False))
    outbound.queue_text(db, owner, "x")
    db.commit()
    run_jobs(["send_messages"])
    assert db.execute(select(Message.status).where(Message.body == "x")).scalar_one() == "failed"
    assert _jobs(db, "send_messages")[0].status == "failed"


# ---------- watchdog ----------
def test_worker_watchdog_alerts_once_per_hour(db, owner, sent):
    assert monitor.check_worker_once() is False  # never started: no alert
    health.beat(db)
    db.commit()
    assert monitor.check_worker_once() is False  # fresh
    from app.db.models import Heartbeat

    hb = db.get(Heartbeat, "worker")
    hb.last_seen_at = utcnow() - timedelta(minutes=20)
    db.commit()
    assert monitor.check_worker_once() is True
    assert monitor.check_worker_once() is False
    assert len(sent) == 1 and sent[0]["kind"] == "template"  # window closed -> template
    assert "worker" in db.execute(select(Message.body).where(Message.raw_payload["direct"].astext == "true"))\
        .scalar_one()


# ---------- Meta HTTP client ----------
def _meta(handler):
    return MetaWhatsAppChannel(http=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture
def meta_env(monkeypatch):
    s = get_settings()
    from pydantic import SecretStr

    monkeypatch.setattr(s, "whatsapp_access_token", SecretStr("TOKEN"))
    monkeypatch.setattr(s, "whatsapp_phone_number_id", "PHONE")


def test_meta_send_text_request(meta_env):
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], req.content
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT1"}]})

    assert _meta(handler).send_text("+1 804 555 0100", "hi") == "wamid.OUT1"
    assert seen["url"].endswith("/PHONE/messages") and seen["auth"] == "Bearer TOKEN"
    import json

    body = json.loads(seen["body"])
    assert body["to"] == "18045550100" and body["text"]["body"] == "hi" and body["type"] == "text"


def test_meta_template_request(meta_env):
    def handler(req):
        import json

        body = json.loads(req.content)
        assert body["template"] == {"name": "ai_manager_update", "language": {"code": "en_US"}}
        return httpx.Response(200, json={"messages": [{"id": "wamid.T"}]})

    assert _meta(handler).send_template("+18045550100", "ai_manager_update", "en_US") == "wamid.T"


@pytest.mark.parametrize("status,code,retryable,window", [
    (400, 131047, False, True), (500, 1, True, False), (429, 130429, True, False), (401, 190, False, False),
])
def test_meta_errors(meta_env, status, code, retryable, window):
    ch = _meta(lambda req: httpx.Response(status, json={"error": {"code": code, "message": "m"}}))
    with pytest.raises(ChannelError) as exc:
        ch.send_text("+1", "x")
    assert exc.value.retryable is retryable and exc.value.outside_window is window


def test_meta_network_error_is_retryable(meta_env):
    def handler(req):
        raise httpx.ConnectError("down")

    with pytest.raises(ChannelError) as exc:
        _meta(handler).send_text("+1", "x")
    assert exc.value.retryable


def test_meta_download_and_document(meta_env):
    def handler(req):
        if req.url.path.endswith("/MEDIA1"):
            return httpx.Response(200, json={"url": "https://lookaside.example/abc", "mime_type": "image/png"})
        if req.url.host == "lookaside.example":
            assert req.headers["authorization"] == "Bearer TOKEN"
            return httpx.Response(200, content=PNG)
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "UP1"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.DOC"}]})

    ch = _meta(handler)
    m = ch.download_media("MEDIA1")
    assert m.data == PNG and m.mime_type == "image/png"
    assert ch.send_document("+1", PDF, "r.pdf", "application/pdf", "report") == "wamid.DOC"


def test_meta_missing_credentials(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "whatsapp_access_token", SecretStr(""))
    with pytest.raises(ChannelError) as exc:
        MetaWhatsAppChannel(http=httpx.Client()).send_text("+1", "x")
    assert not exc.value.retryable


def test_signature_verification():
    from app.channels.meta_whatsapp import compute_signature

    ch = MetaWhatsAppChannel(http=httpx.Client())
    body = b'{"a":1}'
    assert ch.verify_signature(body, compute_signature("test-app-secret", body))
    assert not ch.verify_signature(body, compute_signature("other", body))
    assert not ch.verify_signature(body, None)


@pytest.mark.parametrize("body,expected", [
    ("☀️ Daily summary — Monday\nstuff", "your daily summary"),
    ("🔐 Approval needed: A-12 (for T-3)\nx", "approval A-12"),
    ("⏰ Task reminder\n• T-1", "your task reminders"),
    ("hello", "your tasks"),
])
def test_template_topic(db, owner, body, expected):
    db.add(Message(direction="outbound", user_id=owner.id, body=body, status="held",
                   raw_payload={"hold_reason": "window"}))
    db.flush()
    params = outbound.template_params(db, owner)
    assert params == ["1", expected]
    assert all("\n" not in p for p in params)


def test_template_topic_uses_task_ref(db, owner):
    from app.services import tasks

    t = tasks.create_task(db, title="Call   the\nsupplier about pricing for Q4 orders and more", actor=Actor.user(owner.id))
    db.add(Message(direction="outbound", user_id=owner.id, body="✅ done", status="held", related_task_id=t.id))
    db.flush()
    topic = outbound.template_params(db, owner)[1]
    assert topic.startswith("task T-1 (Call the supplier") and "\n" not in topic and len(topic) < 60
