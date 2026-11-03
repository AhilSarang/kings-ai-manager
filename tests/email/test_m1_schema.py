import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.actions.registry import check_permitted, get_spec
from app.actions.tiers import Tier
from app.db.models import EmailAlertRule, File
from app.email import accounts, contacts, drafts, rules, threads
from app.services import audit, settings_store
from app.services.common import SYSTEM, Actor, NotFound
from tests.email.factories import make_draft, make_message, make_thread


@pytest.fixture
def owner_actor(owner):
    return Actor.user(owner.id)


def test_email_tables_exist(db):
    names = set(db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars())
    assert {"email_accounts", "email_threads", "email_messages", "email_attachments", "email_drafts",
            "email_alert_rules", "email_contacts"} <= names


def test_builtin_rules_seeded(db):
    seeded = {(r.value, r.alert_level, r.bypass_quiet_hours) for r in db.execute(
        select(EmailAlertRule).where(EmailAlertRule.created_by == "system")).scalars()}
    assert seeded == {("A", "instant", True), ("B", "instant", True), ("C", "instant", False),
                      ("D", "instant", False)}


def test_email_settings_seeded(db):
    assert settings_store.get(db, "email_followup_business_days") == 3
    assert settings_store.get(db, "email_large_order_threshold_usd") is None
    assert settings_store.get(db, "email_triage_batch_size") == 10
    assert settings_store.get(db, "email_paused") is False


def test_files_accept_email_source(db):
    db.add(File(storage_key="x/y.pdf", mime_type="application/pdf", size_bytes=1, sha256="0" * 64, source="email"))
    db.flush()
    with pytest.raises(IntegrityError):
        db.add(File(storage_key="x/z.pdf", mime_type="application/pdf", size_bytes=1, sha256="0" * 64,
                    source="fax"))
        db.flush()
    db.rollback()


def test_error_refs_no_longer_clash_with_threads(db):
    assert audit.next_error_ref(db).startswith("ERR-")


# ---- accounts ----
def test_primary_account_created_paused(db):
    a = accounts.get_primary(db)
    assert a.address == "you@example.com" and a.status == "paused" and a.provider == "gmail"
    assert accounts.get_primary(db).id == a.id
    assert accounts.set_status(db, a, "active", SYSTEM) is True
    assert accounts.set_status(db, a, "active", SYSTEM) is False


# ---- threads ----
def test_thread_refs_sequential_and_lookup(db):
    t1, t2 = make_thread(db), make_thread(db)
    assert (t1.ref, t2.ref) == ("E-1", "E-2")
    assert threads.get_by_ref(db, "e2").id == t2.id
    assert threads.get_by_ref(db, " E-1 ").id == t1.id
    with pytest.raises(NotFound):
        threads.get_by_ref(db, "E-99")
    with pytest.raises(NotFound):
        threads.get_by_ref(db, "T-1")


def test_thread_unique_per_account(db):
    make_thread(db, provider_thread_id="abc")
    with pytest.raises(IntegrityError):
        make_thread(db, provider_thread_id="abc")
    db.rollback()


def test_thread_status_changes(db, owner_actor):
    from app.services.common import utcnow

    t = make_thread(db, needs_reply=True, follow_up_due_at=utcnow())
    threads.set_status(db, t, "waiting_on_them", owner_actor)
    assert t.follow_up_due_at is not None
    threads.set_status(db, t, "handled", owner_actor)
    assert t.follow_up_due_at is None and t.needs_reply is False
    threads.set_status(db, t, "open", owner_actor)
    with pytest.raises(threads.InvalidThreadStatus):
        threads.set_status(db, t, "archived", owner_actor)


def test_message_defaults(db):
    t = make_thread(db)
    m = make_message(db, t)
    assert (m.auth_dkim, m.triage_status, m.is_from_owner, m.possible_phishing) == ("none", "pending", False, False)


def test_invalid_category_rejected(db):
    with pytest.raises(IntegrityError):
        make_thread(db, category="spam")
    db.rollback()


# ---- drafts ----
def test_draft_refs_and_transitions(db, owner_actor):
    t = make_thread(db)
    d1, d2 = make_draft(db, t), make_draft(db, t)
    assert (d1.ref, d2.ref) == ("D-1", "D-2")
    assert drafts.get_by_ref(db, "d1").id == d1.id
    assert drafts.get_by_ref(db, "D-1", lock=True).id == d1.id
    drafts.change_status(db, d1, "pending_approval", owner_actor)
    drafts.change_status(db, d1, "draft", owner_actor, reason="edited")
    drafts.change_status(db, d1, "pending_approval", owner_actor)
    drafts.change_status(db, d1, "sent", owner_actor)
    for target in ("draft", "discarded", "pending_approval"):
        with pytest.raises(drafts.InvalidDraftTransition):
            drafts.change_status(db, d1, target, owner_actor)
    drafts.change_status(db, d2, "discarded", owner_actor)
    with pytest.raises(drafts.InvalidDraftTransition):
        drafts.change_status(db, d2, "pending_approval", owner_actor)


@pytest.mark.parametrize("start,target", [("draft", "sent"), ("draft", "send_failed")])
def test_draft_cannot_skip_approval(db, owner_actor, start, target):
    d = make_draft(db, make_thread(db))
    with pytest.raises(drafts.InvalidDraftTransition):
        drafts.change_status(db, d, target, owner_actor)


def test_send_failed_can_be_retried(db, owner_actor):
    d = make_draft(db, make_thread(db))
    drafts.change_status(db, d, "pending_approval", owner_actor)
    drafts.change_status(db, d, "send_failed", owner_actor)
    drafts.change_status(db, d, "pending_approval", owner_actor)


# ---- alert rules ----
@pytest.mark.parametrize("rule_type,value,stored", [
    ("sender", " Buyer@HBI.com ", "buyer@hbi.com"),
    ("domain", "@Pellon.COM", "pellon.com"),
    ("keyword", "Chargeback", "chargeback"),
    ("custom_description", "Emails about warehouse lease renewal", "Emails about warehouse lease renewal"),
])
def test_owner_rules(db, owner_actor, rule_type, value, stored):
    r = rules.add_owner_rule(db, rule_type=rule_type, value=value, alert_level="instant", actor=owner_actor)
    assert r.value == stored and r.created_by == "owner" and r.is_active
    assert r in rules.active_rules(db)


@pytest.mark.parametrize("rule_type,value", [
    ("sender", "not-an-email"), ("domain", "no spaces.com"), ("keyword", "x"),
    ("custom_description", "hi"), ("builtin_category", "A"), ("regex", "x"),
])
def test_invalid_owner_rules(db, owner_actor, rule_type, value):
    with pytest.raises(rules.InvalidRule):
        rules.add_owner_rule(db, rule_type=rule_type, value=value, alert_level="instant", actor=owner_actor)


def test_newest_rule_replaces_same_value(db, owner_actor):
    r1 = rules.add_owner_rule(db, rule_type="domain", value="hbi.com", alert_level="instant", actor=owner_actor)
    r2 = rules.add_owner_rule(db, rule_type="domain", value="hbi.com", alert_level="ignore", actor=owner_actor)
    assert not r1.is_active and r2.is_active and r2.alert_level == "ignore"


def test_only_owner_adds_rules_and_builtins_stay(db, owner_actor):
    with pytest.raises(rules.InvalidRule):
        rules.add_owner_rule(db, rule_type="domain", value="x.com", alert_level="instant", actor=Actor.agent("email"))
    with pytest.raises(rules.InvalidRule):
        rules.deactivate(db, rules.builtin(db, "A"), owner_actor)


# ---- contacts ----
def test_contacts_seen_and_typed(db, owner_actor):
    c = contacts.record_seen(db, "Rep@Supplier.com", "Sam Rep", None)
    contacts.record_seen(db, "rep@supplier.com", None, None)
    assert (c.address, c.domain, c.message_count, c.name, c.type) == \
        ("rep@supplier.com", "supplier.com", 2, "Sam Rep", "other")
    contacts.set_type(db, "rep@supplier.com", type="supplier", is_vip=True, actor=owner_actor)
    assert c.type == "supplier" and c.is_vip and c.type_set_by_owner
    assert contacts.record_seen(db, "undisclosed-recipients:;", None, None) is None
    with pytest.raises(contacts.InvalidContact):
        contacts.set_type(db, "rep@supplier.com", type="enemy", actor=owner_actor)


# ---- action registry ----
def test_email_action_tiers():
    assert get_spec("email_send").tier == Tier.EXTERNAL
    for t in ("email_create_draft", "email_update_thread_status", "email_set_contact_type", "email_add_alert_rule"):
        assert get_spec(t).tier == Tier.INTERNAL
    assert check_permitted("email_send", {"max_tier": 2})
