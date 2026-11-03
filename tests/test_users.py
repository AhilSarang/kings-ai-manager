from app.services import users


def test_ensure_owner_is_idempotent(db):
    a = users.ensure_owner(db)
    b = users.ensure_owner(db)
    assert a.id == b.id
    assert a.role == "owner" and a.whatsapp_number == "+18045550100"


def test_lookup_accepts_meta_format(db, owner):
    assert users.get_active_by_number(db, "18045550100").id == owner.id
    assert users.get_active_by_number(db, "+1 (804) 555-0100").id == owner.id
    assert users.get_active_by_number(db, "15550000000") is None


def test_inactive_user_not_allowed(db, owner):
    owner.is_active = False
    db.flush()
    assert users.get_active_by_number(db, "18045550100") is None


def test_pin_hashing():
    h = users.hash_pin("4821")
    assert "4821" not in h
    assert users.verify_pin("4821", h)
    assert not users.verify_pin("4822", h)
    assert not users.verify_pin("4821", None)
    assert not users.verify_pin("4821", "garbage")


def test_every_owner_gets_the_alerts(db, owner, sent):
    """A second owner runs the business too: alerts, approvals and summaries go to both."""
    from app.db.models import User
    from app.services import users
    from app.services.notify import notify_owner
    from tests.conftest import run_jobs

    from app.services.common import utcnow

    partner = User(name="Partner", whatsapp_number="+13469011109", role="owner", is_active=True,
                   last_inbound_at=utcnow())           # both inside WhatsApp's 24-hour window
    owner.last_inbound_at = utcnow()
    db.add(partner)
    db.commit()
    assert [u.name for u in users.owners(db)] == [owner.name, "Partner"]

    notify_owner(db, "stock is low on SKZ-001", urgent=True)
    db.commit()
    run_jobs(["send_owner_message", "send_messages"])
    went_to = {m["to"] for m in sent if "stock is low" in m.get("text", "")}
    assert went_to == {owner.whatsapp_number, "+13469011109"}


def test_a_viewer_can_be_switched_off(db, owner):
    from app.db.models import User
    from app.services import users

    guest = User(name="Temp", whatsapp_number="+15551234567", role="viewer", is_active=False)
    db.add(guest)
    db.commit()
    assert users.get_active_by_number(db, "+15551234567") is None, "an inactive number is refused"
    assert users.get_active_by_number(db, owner.whatsapp_number) is not None
