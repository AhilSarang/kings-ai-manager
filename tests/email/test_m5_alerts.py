from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import AuditLog, EmailThread, Job, Message
from app.email import rules
from app.llm.fake import FakeLLMProvider
from app.services import outbound, settings_store
from app.services.common import SYSTEM, Actor, utcnow
from tests.conftest import run_jobs
from tests.email.helpers import GENUINE_AMAZON_AUTH, connect, receive, triage_reply


@pytest.fixture
def account(db, owner):
    owner.last_inbound_at = utcnow()  # 24h window open
    settings_store.set_value(db, "quiet_hours", None, SYSTEM)
    db.commit()
    return connect(db)


def triage_and_alert(*replies):
    from app.db.session import session_scope
    from app.email.sync import enqueue_triage

    FakeLLMProvider.script(*replies)
    with session_scope() as s:
        enqueue_triage(s)
    run_jobs(["triage_emails"])
    with session_scope() as s:  # alerts wait ~20s to allow combining; make them due now
        for j in s.execute(select(Job).where(Job.type == "send_email_alert", Job.status == "pending")).scalars():
            j.run_after = utcnow()
    run_jobs(["send_email_alert", "send_owner_message", "send_messages"])


def texts(sent):
    return [s.get("text", f"[{s['kind']}]") for s in sent]


def test_alert_format(db, account, sent):
    receive(db, account, from_="Sam Rep <sam@pellon.com>", subject="Price increase from Oct 1")
    triage_and_alert(triage_reply({"category": "supplier", "importance": "high", "matches": ["D"],
                                   "summary": "Pellon raises prices 8% from Oct 1.",
                                   "action": "Decide whether to order before Oct 1."}))
    [t] = texts(sent)
    assert t.splitlines() == [
        "📧 EMAIL ALERT — Supplier problem (E-1)",
        "From: Sam Rep <sam@pellon.com>",
        "Subject: Price increase from Oct 1",
        "Summary: Pellon raises prices 8% from Oct 1.",
        "Suggested: Decide whether to order before Oct 1.",
        'Reply "E-1" for details or "draft E-1 …" to write a reply.',
    ]
    thread = db.execute(select(EmailThread)).scalar_one()
    db.refresh(thread)
    assert thread.alerted_rules == ["D"] and thread.alert_sent_at is not None
    assert db.execute(select(AuditLog).where(AuditLog.action == "email_alert_sent")).scalar_one()
    assert db.execute(select(Message).where(Message.direction == "outbound")).scalar_one().status == "sent"


def test_one_alert_per_thread_per_rule(db, account, sent):
    m = receive(db, account, subject="Delay")
    triage_and_alert(triage_reply({"matches": ["D"], "importance": "high"}))
    receive(db, account, subject="Re: Delay", thread_id=m.thread.provider_thread_id)
    triage_and_alert(triage_reply({"matches": ["D"], "importance": "high"}))
    assert len(sent) == 1
    # importance rises -> alert again
    receive(db, account, subject="Re: Delay", thread_id=m.thread.provider_thread_id)
    triage_and_alert(triage_reply({"matches": ["D"], "importance": "critical"}))
    assert len(sent) == 2
    # a different rule on the same thread -> alert
    receive(db, account, subject="Re: Delay", thread_id=m.thread.provider_thread_id)
    triage_and_alert(triage_reply({"matches": ["B"], "importance": "critical"}))
    assert len(sent) == 3 and "Legal notice" in sent[-1]["text"]


def test_three_alerts_combined(db, account, sent):
    for i in range(3):
        receive(db, account, subject=f"Problem {i}", from_=f"v{i}@x.com")
    triage_and_alert(triage_reply({"matches": ["D"], "summary": "one"}, {"matches": ["C"], "summary": "two"},
                                  {"matches": ["B"], "summary": "three"}))
    [t] = texts(sent)
    assert t.startswith("📧 3 EMAIL ALERTS")
    assert "1) Supplier problem — E-1" in t and "3) Legal notice — E-3" in t
    assert db.execute(select(Job).where(Job.type == "send_email_alert", Job.status == "done")).scalars().all()


def test_two_alerts_sent_separately(db, account, sent):
    receive(db, account, subject="a", from_="a@x.com")
    receive(db, account, subject="b", from_="b@x.com")
    triage_and_alert(triage_reply({"matches": ["D"]}, {"matches": ["C"]}))
    assert len(sent) == 2 and all(t.startswith("📧 EMAIL ALERT") for t in texts(sent))


def _quiet_all_day(db):
    settings_store.set_value(db, "quiet_hours", {"start": "00:00", "end": "23:59"}, SYSTEM)
    db.commit()


def test_quiet_hours_hold_supplier_but_not_amazon_or_legal(db, account, sent):
    _quiet_all_day(db)
    receive(db, account, subject="Supplier delay", from_="s@x.com")
    receive(db, account, subject="Attorney letter", from_="law@firm.com")
    receive(db, account, subject="Listing removed", from_="Amazon <seller-performance@amazon.com>",
            headers={"Authentication-Results": GENUINE_AMAZON_AUTH})
    triage_and_alert(triage_reply({"matches": ["D"]}, {"matches": ["B"]}, {"matches": ["A"], "category": "amazon"}))
    # combined message is urgent because it contains A/B
    assert len(sent) == 1 and "3 EMAIL ALERTS" in sent[0]["text"]


def test_quiet_hours_hold_single_supplier_alert(db, account, sent):
    _quiet_all_day(db)
    receive(db, account, subject="Supplier delay")
    triage_and_alert(triage_reply({"matches": ["D"]}))
    assert sent == []
    held = db.execute(select(Message).where(Message.status == "held")).scalar_one()
    assert held.raw_payload["hold_reason"] == "quiet_hours" and held.body.startswith("📧 EMAIL ALERT")


def test_custom_rule_can_bypass_quiet_hours(db, account, owner, sent):
    _quiet_all_day(db)
    rules.add_owner_rule(db, rule_type="domain", value="walmart.com", alert_level="instant",
                         bypass_quiet_hours=True, actor=Actor.user(owner.id), name="Walmart")
    receive(db, account, subject="PO", from_="buyer@walmart.com")
    triage_and_alert(triage_reply({}))
    assert len(sent) == 1 and "EMAIL ALERT — Walmart" in sent[0]["text"]


def test_phishing_warning_format(db, account, sent):
    receive(db, account, from_="Amazon <help@amzn-secure.net>", subject="Verify your account",
            body="Your account will be suspended. Click here to verify your account.")
    triage_and_alert(triage_reply({"category": "amazon", "matches": ["A"], "importance": "critical"}))
    [t] = texts(sent)
    assert t.startswith("⚠️ SUSPICIOUS EMAIL (E-1)") and "FAKE" in t and "Don't click" in t


def test_payment_fraud_line_in_alert(db, account, sent):
    receive(db, account, subject="Invoice", body="Our bank details have changed, please pay the new account.")
    triage_and_alert(triage_reply({"matches": ["D"]}))
    assert "verify by phone" in sent[0]["text"]


def test_ignored_thread_not_alerted(db, account, sent):
    m = receive(db, account, subject="x")
    m.thread.status = "ignored"
    db.commit()
    triage_and_alert(triage_reply({"matches": ["D"]}))
    assert sent == []


def test_window_closed_uses_template_with_email_topic(db, owner, sent):
    settings_store.set_value(db, "quiet_hours", None, SYSTEM)
    owner.last_inbound_at = utcnow() - timedelta(days=2)
    db.commit()
    account = connect(db)
    receive(db, account, subject="Legal")
    triage_and_alert(triage_reply({"matches": ["B"], "importance": "critical"}))
    assert [s["kind"] for s in sent] == ["template"]
    assert sent[0]["params"] == ["1", "email E-1"]


def test_email_pause_defers_alert(db, account, sent):
    receive(db, account, subject="x")
    FakeLLMProvider.script(triage_reply({"matches": ["D"]}))
    from app.db.session import session_scope
    from app.email.sync import enqueue_triage

    with session_scope() as s:
        enqueue_triage(s)
    run_jobs(["triage_emails"])
    settings_store.set_value(db, "email_paused", True, SYSTEM)
    for j in db.execute(select(Job).where(Job.type == "send_email_alert")).scalars():
        j.run_after = utcnow()
    db.commit()
    run_jobs(["send_email_alert"])
    assert sent == []
    later = db.execute(select(Job).where(Job.type == "send_email_alert", Job.status == "pending")).scalar_one()
    assert later.run_after > utcnow() + timedelta(minutes=10)


def test_template_topic_for_alert(db, owner):
    db.add(Message(direction="outbound", user_id=owner.id, status="held",
                   body="📧 EMAIL ALERT — Legal notice (E-7)\nFrom: x"))
    db.flush()
    assert outbound.template_params(db, owner) == ["1", "email E-7"]
