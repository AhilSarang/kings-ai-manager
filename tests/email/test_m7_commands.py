from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import EmailAlertRule, EmailContact, EmailThread, Job
from app.email import followup
from app.email.fake_gmail import FakeGmailClient as G
from app.llm.fake import FakeLLMProvider
from app.services import settings_store
from app.services.common import SYSTEM, utcnow
from app.services.summaries import daily_summary
from tests.conftest import run_jobs
from tests.email.helpers import connect, receive, triage_reply
from tests.helpers import post, text_payload


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


def say(client, sent, text, *llm):
    FakeLLMProvider.script(*llm)
    before = len(sent)
    assert post(client, text_payload(text)).status_code == 200
    run_jobs(limit=100)
    return "\n---\n".join(s.get("text", f"[{s['kind']}]") for s in sent[before:])


def act(type_, **params):
    return {"actions": [{"type": type_, "params": params}], "reply_to_owner": ""}


def make_thread(db, account, *, subject, importance="high", needs_reply=True, days_old=3, **kw):
    m = receive(db, account, subject=subject, age_seconds=days_old * 86400, **kw)
    t = m.thread
    t.importance, t.needs_reply, t.category, t.summary = importance, needs_reply, "supplier", f"About {subject}"
    db.commit()
    return t


# ---------- commands ----------
def test_emails_and_thread_detail(client, db, sent, account):
    make_thread(db, account, subject="Damaged goods", from_="Buyer <buyer@shop.com>",
                attachments=[("photo.png", "image/png", b"\x89PNG\r\n\x1a\n")])
    make_thread(db, account, subject="FYI", importance="low", needs_reply=False)
    out = say(client, sent, "EMAILS")
    assert "Needs your reply* (1)" in out and "E-1 ❗ Damaged goods — buyer@shop.com" in out and "FYI" not in out
    out = say(client, sent, "e1")
    assert "*E-1* Damaged goods" in out and "Summary: About Damaged goods" in out
    assert "— Buyer," in out and "📎 photo.png" in out and "HANDLED E-1" in out
    assert FakeLLMProvider.requests == []


def test_emails_empty(client, db, sent, account):
    assert "No emails need your attention" in say(client, sent, "emails")


def test_handled_and_ignore(client, db, sent, account):
    t = make_thread(db, account, subject="x")
    assert "E-1 marked handled" in say(client, sent, "HANDLED E-1")
    db.refresh(t)
    assert t.status == "handled" and not t.needs_reply
    assert "E-1 marked ignored" in say(client, sent, "ignore e-1")
    assert "can't find email thread E-9" in say(client, sent, "HANDLED E-9")


def test_waiting_command(client, db, sent, account):
    t = make_thread(db, account, subject="Quote", needs_reply=False, days_old=6)
    t.status, t.last_message_from_owner = "waiting_on_them", True
    t.follow_up_due_at = utcnow() - timedelta(days=1)
    db.commit()
    out = say(client, sent, "WAITING")
    assert "Waiting on replies" in out and "E-1" in out and "OVERDUE" in out and "business day" in out


def test_drafts_and_draft_detail(client, db, sent, account):
    from tests.email.factories import make_draft

    t = make_thread(db, account, subject="x")
    make_draft(db, t)
    db.commit()
    out = say(client, sent, "DRAFTS")
    assert "D-1 → supplier@example.com" in out and "(draft, reply in E-1)" in out
    out = say(client, sent, "d-1")
    assert "✍️ Draft D-1, reply in E-1 (v1)" in out
    assert "No open drafts" not in out


def test_help_lists_email_commands(client, db, sent, account):
    out = say(client, sent, "HELP")
    for cmd in ("EMAILS", "E-12", "WAITING", "DRAFTS", "SEND D-3", "DRAFT E-12", "EMAIL PAUSE"):
        assert cmd in out


# ---------- email pause & kill switch ----------
def test_email_pause_resume(client, db, sent, account):
    assert "Email paused" in say(client, sent, "EMAIL PAUSE")
    G.add_message(subject="while paused")
    from app.jobs import periodic

    periodic.gmail_poll()
    assert db.execute(select(func.count()).select_from(Job).where(Job.type == "gmail_sync")).scalar_one() == 0
    out = say(client, sent, "email resume")
    assert "Email resumed" in out
    db.expire_all()
    assert db.execute(select(EmailThread)).scalars().first().subject == "while paused"


def test_stop_all_keeps_email_work_and_resume_restarts(client, db, sent, account):
    from app.jobs import queue

    m = receive(db, account, subject="pending mail")
    queue.enqueue(db, "triage_emails", {}, uses_llm=True)
    queue.enqueue(db, "gmail_sync", {"account_id": str(account.id)})
    db.commit()
    say(client, sent, "STOP ALL")
    db.expire_all()
    statuses = {j.type: j.status for j in db.execute(select(Job)).scalars() if j.type in ("triage_emails", "gmail_sync")}
    assert statuses == {"triage_emails": "pending", "gmail_sync": "cancelled"}
    say(client, sent, "RESUME", triage_reply({}))
    db.expire_all()
    assert db.get(type(m), m.id).triage_status == "done"
    assert db.execute(select(Job).where(Job.type == "gmail_sync", Job.status == "done")).first()


# ---------- natural language ----------
def test_email_search(client, db, sent, account):
    make_thread(db, account, subject="HBI order", from_="Sales <sales@hbi.com>")
    make_thread(db, account, subject="Other", from_="x@else.com")
    out = say(client, sent, "any emails from HBI this week?",
              act("email_search", sender="hbi", subject_contains=None, since_days=7, category=None,
                  needs_reply_only=None))
    assert "1 matching thread" in out and "HBI order" in out and "Other" not in out
    out = say(client, sent, "emails from nobody", act("email_search", sender="nobody", subject_contains=None,
                                                     since_days=None, category=None, needs_reply_only=None))
    assert "No matching emails" in out


def test_email_summary_today(client, db, sent, account):
    make_thread(db, account, subject="Legal letter", days_old=0, importance="critical")
    make_thread(db, account, subject="Promo", days_old=0, importance="low")
    t = db.execute(select(EmailThread).where(EmailThread.subject == "Promo")).scalar_one()
    t.category = "marketing_spam"
    db.commit()
    out = say(client, sent, "what important emails came in today?", act("email_summary", period="today"))
    assert "1 business email thread today, 1 important" in out and "Legal letter" in out and "Promo" not in out


def test_follow_up_override_and_stop(client, db, sent, account):
    t = make_thread(db, account, subject="Quote")
    out = say(client, sent, "follow up on E-1 in 5 days", act("email_set_followup", thread_ref="E-1", days=5,
                                                                 stop=None))
    db.refresh(t)
    assert "I'll flag E-1" in out and t.follow_up_due_at.date() == followup.add_business_days(utcnow(), 5).date()
    say(client, sent, "stop tracking E-1", act("email_set_followup", thread_ref="E-1", days=None, stop=True))
    db.refresh(t)
    assert t.follow_up_due_at is None and t.follow_up_tracking is False


def test_add_alert_rule_by_owner(client, db, sent, account):
    """Acceptance 12."""
    out = say(client, sent, "Always alert me about emails from walmart.com",
              act("email_add_alert_rule", rule_type="domain", value="walmart.com", alert_level="instant",
                  bypass_quiet_hours=None))
    assert "Rule saved" in out and "walmart.com" in out
    rule = db.execute(select(EmailAlertRule).where(EmailAlertRule.created_by == "owner")).scalar_one()
    assert (rule.rule_type, rule.value, rule.alert_level) == ("domain", "walmart.com", "instant")
    # the rule works: a normal-looking Walmart email alerts
    from app.services.common import utcnow as now

    owner_row = db.execute(select(__import__("app.db.models", fromlist=["User"]).User)).scalars().first()
    owner_row.last_inbound_at = now()
    db.commit()
    receive(db, account, subject="Hello", from_="buyer@walmart.com")
    FakeLLMProvider.script(triage_reply({"importance": "normal", "confidence": 0.2}))
    from app.email.sync import enqueue_triage

    enqueue_triage(db)
    db.commit()
    run_jobs(["triage_emails"])
    for j in db.execute(select(Job).where(Job.type == "send_email_alert")).scalars():
        j.run_after = now()
    db.commit()
    run_jobs(["send_email_alert", "send_messages"])
    assert any("EMAIL ALERT" in s.get("text", "") and "buyer@walmart.com" in s.get("text", "") for s in sent)


def test_invalid_rule_reported(client, db, sent, account):
    out = say(client, sent, "alert me about bob", act("email_add_alert_rule", rule_type="sender", value="bob",
                                                     alert_level="instant", bypass_quiet_hours=None))
    assert "not an email address" in out
    assert db.execute(select(EmailAlertRule).where(EmailAlertRule.created_by == "owner")).first() is None


def test_set_contact_type(client, db, sent, account):
    out = say(client, sent, "rep@pellon.com is a supplier and a VIP",
              act("email_set_contact_type", address="rep@pellon.com", contact_type="supplier", is_vip=True))
    assert "rep@pellon.com saved as supplier / VIP" in out
    c = db.execute(select(EmailContact)).scalar_one()
    assert c.type == "supplier" and c.is_vip and c.type_set_by_owner


def test_update_thread_status_nl(client, db, sent, account):
    t = make_thread(db, account, subject="x")
    say(client, sent, "mark E-1 handled", act("email_update_thread_status", thread_ref="E-1", status="handled"))
    db.refresh(t)
    assert t.status == "handled"


# ---------- daily summary ----------
def test_daily_summary_email_sections(db, account):
    reply = make_thread(db, account, subject="Needs answer", days_old=5)
    make_thread(db, account, subject="Fresh one", days_old=0)
    wait = make_thread(db, account, subject="Our quote", needs_reply=False, days_old=6)
    wait.status, wait.last_message_from_owner = "waiting_on_them", True
    wait.follow_up_due_at = utcnow() - timedelta(hours=2)
    phish = make_thread(db, account, subject="Fake Amazon", needs_reply=False)
    phish.risk_flags = ["possible_phishing"]
    reply.alert_sent_at = utcnow() - timedelta(hours=1)
    bad = receive(db, account, subject="Unsortable")
    bad.triage_status = "failed"
    db.commit()
    text = daily_summary(db)
    assert "*Email alerts (24h)*: 1 — E-1" in text
    assert "*Emails needing your reply* (1)" in text and "Needs answer" in text and "Fresh one" not in text
    assert "*Waiting on replies (follow up)* (1)" in text and "Our quote" in text
    assert "*Suspicious / high-risk emails* (1)" in text and "possible phishing" in text
    assert "*Emails I couldn't sort — please check* (1)" in text and "Unsortable" in text


def test_daily_summary_warns_when_gmail_disconnected(db, account):
    account.status = "auth_error"
    db.commit()
    assert "Gmail needs re-connecting" in daily_summary(db)


def test_no_email_section_before_connecting(db, owner):
    assert "Email" not in daily_summary(db)


# ---------- follow-up timing (acceptance 11) ----------
def test_waiting_appears_after_three_business_days(db, account):
    from app.email.sync import ingest_message

    raw = G.add_message(subject="Re: PO", from_="you@example.com", to="sam@pellon.com",
                        labels=["SENT"], age_seconds=6 * 86400)
    ingest_message(db, account, raw)
    db.commit()
    t = db.execute(select(EmailThread)).scalar_one()
    sent_at = t.last_message_at
    assert t.follow_up_due_at == followup.add_business_days(sent_at, 3)
    expected = followup.business_days_between(sent_at, utcnow()) >= 3
    assert ("Re: PO" in daily_summary(db)) == expected


# ---------- listing and removing rules ----------
def _active_owner_values(db):
    db.expire_all()
    return [r.value for r in db.execute(select(EmailAlertRule).where(
        EmailAlertRule.created_by == "owner", EmailAlertRule.is_active.is_(True))
        .order_by(EmailAlertRule.created_at)).scalars()]


def test_rules_list_and_remove_by_number(client, db, sent, account):
    say(client, sent, "ignore all emails from alibaba.com",
        act("email_add_alert_rule", rule_type="domain", value="alibaba.com", alert_level="ignore"))
    say(client, sent, "alert me right away about emails from pellon.com",
        act("email_add_alert_rule", rule_type="domain", value="pellon.com", alert_level="instant"))
    FakeLLMProvider.requests.clear()
    out = say(client, sent, "rules")
    assert "1) Ignore: emails from alibaba.com" in out and "2) Alert me right away: emails from pellon.com" in out
    assert "Always on" in out and FakeLLMProvider.requests == []
    assert "no rule 5" in say(client, sent, "REMOVE RULE 5")
    out = say(client, sent, "delete rule 1")
    assert "Removed rule 1: Ignore: emails from alibaba.com" in out
    assert _active_owner_values(db) == ["pellon.com"]
    assert "1) Alert me right away: emails from pellon.com" in say(client, sent, "EMAIL RULES")


def test_remove_rule_in_plain_words(client, db, sent, account):
    say(client, sent, "ignore emails from alibaba.com",
        act("email_add_alert_rule", rule_type="domain", value="alibaba.com", alert_level="ignore"))
    out = say(client, sent, "stop ignoring alibaba please",
              act("email_remove_alert_rule", value="alibaba.com"))
    assert "Rule removed: Ignore: emails from alibaba.com" in out
    assert _active_owner_values(db) == []
    out = say(client, sent, "what email rules do I have?", act("email_list_alert_rules"))
    assert "(none yet)" in out


def test_remove_rule_needs_owner_request(client, db, sent, account):
    say(client, sent, "ignore emails from alibaba.com",
        act("email_add_alert_rule", rule_type="domain", value="alibaba.com", alert_level="ignore"))
    # A confused model tries to remove the rule when the owner didn't ask.
    out = say(client, sent, "what's new with E-1?", act("email_remove_alert_rule", value="alibaba.com"))
    assert "only remove an email rule when you ask" in out
    assert _active_owner_values(db) == ["alibaba.com"]
    assert "don't have a rule" in say(client, sent, "remove my rule for uline.com",
                                      act("email_remove_alert_rule", value="uline.com"))


def test_builtin_rules_cannot_be_removed(db, owner):
    from app.email import rules
    from app.services.common import Actor

    with pytest.raises(rules.InvalidRule):
        rules.remove_owner_rule(db, rules.builtin(db, "A"), Actor.user(owner.id))
    with pytest.raises(rules.InvalidRule):
        rules.remove_owner_rule(db, rules.builtin(db, "B"), SYSTEM)


def test_self_sent_notes_are_not_waiting_on_anyone(client, db, sent, account):
    from app.email.sync import ingest_message
    from tests.email.helpers import G

    raw = G.add_message(from_="you@example.com", to="you@example.com", subject="",
                        labels=["SENT", "INBOX"], age_seconds=9 * 86400)
    note = ingest_message(db, account, raw)
    db.commit()
    assert note.thread.status == "waiting_on_them" and note.thread.participants == []
    assert "Nothing is waiting" in say(client, sent, "WAITING")
    # A real email he sent to someone else still counts.
    other = ingest_message(db, account, G.add_message(from_="you@example.com", to="sam@pellon.com",
                                                      subject="Order", labels=["SENT"], age_seconds=9 * 86400))
    db.commit()
    assert other.thread.ref in say(client, sent, "WAITING")
