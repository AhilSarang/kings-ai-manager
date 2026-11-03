"""Email alert rules (built-in categories A-D plus owner rules)."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EMAIL_ALERT_LEVELS, EMAIL_RULE_TYPES, EmailAlertRule
from app.services import audit
from app.services.common import Actor, NotFound, ServiceError

BUILTIN_CATEGORIES = ("A", "B", "C", "D")
DOMAIN_RE = re.compile(r"^(?=.{3,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidRule(ServiceError):
    pass


def active_rules(session: Session) -> list[EmailAlertRule]:
    return list(session.execute(
        select(EmailAlertRule).where(EmailAlertRule.is_active.is_(True)).order_by(EmailAlertRule.created_at)
    ).scalars())


def builtin(session: Session, letter: str) -> EmailAlertRule:
    rule = session.execute(
        select(EmailAlertRule).where(EmailAlertRule.rule_type == "builtin_category", EmailAlertRule.value == letter)
    ).scalar_one_or_none()
    if rule is None:
        raise NotFound(f"Built-in alert rule {letter} is missing")
    return rule


def normalize_value(rule_type: str, value: str) -> str:
    value = (value or "").strip()
    if rule_type == "sender":
        value = value.lower()
        if not EMAIL_RE.match(value):
            raise InvalidRule(f"'{value}' is not an email address")
    elif rule_type == "domain":
        value = value.lower().lstrip("@")
        if not DOMAIN_RE.match(value):
            raise InvalidRule(f"'{value}' is not a domain")
    elif rule_type == "keyword":
        if len(value) < 2:
            raise InvalidRule("Keyword is too short")
        value = value.lower()
    elif rule_type == "custom_description":
        if len(value) < 5:
            raise InvalidRule("Describe the emails in a few words")
    elif rule_type == "builtin_category":
        raise InvalidRule("Built-in rules can't be added")
    if len(value) > 500:
        raise InvalidRule("Rule text is too long")
    return value


def add_owner_rule(session: Session, *, rule_type: str, value: str, alert_level: str, actor: Actor,
                   bypass_quiet_hours: bool = False, name: str | None = None) -> EmailAlertRule:
    if actor.type != "user":
        raise InvalidRule("Only the owner can add alert rules")
    if rule_type not in EMAIL_RULE_TYPES:
        raise InvalidRule(f"Unknown rule type '{rule_type}'")
    if alert_level not in EMAIL_ALERT_LEVELS:
        raise InvalidRule(f"Unknown alert level '{alert_level}'")
    value = normalize_value(rule_type, value)
    existing = session.execute(
        select(EmailAlertRule).where(EmailAlertRule.rule_type == rule_type, EmailAlertRule.value == value,
                                     EmailAlertRule.is_active.is_(True))
    ).scalar_one_or_none()
    if existing:
        existing.is_active = False  # the newest instruction wins
    rule = EmailAlertRule(
        name=(name or f"{rule_type}: {value}")[:200], rule_type=rule_type, value=value,
        alert_level=alert_level, bypass_quiet_hours=bypass_quiet_hours, created_by="owner", is_active=True,
    )
    session.add(rule)
    session.flush()
    audit.record(session, actor, "email_alert_rule_added", target_type="email_alert_rule", target_id=rule.id,
                 details={"type": rule_type, "value": value, "level": alert_level,
                          "replaced": str(existing.id) if existing else None})
    return rule


def deactivate(session: Session, rule: EmailAlertRule, actor: Actor) -> None:
    if rule.created_by == "system":
        raise InvalidRule("Built-in rules can be muted with an 'ignore' rule, not deleted")
    rule.is_active = False
    audit.record(session, actor, "email_alert_rule_removed", target_type="email_alert_rule", target_id=rule.id)
    session.flush()


LEVEL_WORDS = {"instant": "Alert me right away", "daily_summary_only": "Daily summary only", "ignore": "Ignore"}
TYPE_WORDS = {"sender": "from", "domain": "from", "keyword": "mentioning", "custom_description": "about"}


def owner_rules(session: Session) -> list[EmailAlertRule]:
    """The owner's active rules in a stable order; RULES numbers them 1..n in this order."""
    return [r for r in active_rules(session) if r.created_by == "owner"]


def describe(rule: EmailAlertRule) -> str:
    what = f'"{rule.value}"' if rule.rule_type in ("keyword", "custom_description") else rule.value
    return f"{LEVEL_WORDS.get(rule.alert_level, rule.alert_level)}: emails {TYPE_WORDS.get(rule.rule_type, '')} {what}"


def remove_owner_rule(session: Session, rule: EmailAlertRule, actor: Actor) -> None:
    if actor.type != "user":
        raise InvalidRule("Only the owner can remove alert rules")
    deactivate(session, rule, actor)


def find_owner_rules(session: Session, value: str) -> list[EmailAlertRule]:
    """Owner rules whose value matches what the owner named (exact, or the domain part of an address)."""
    v = (value or "").strip().lower().lstrip("@")
    if not v:
        return []
    exact = [r for r in owner_rules(session) if r.value.lower() == v]
    if exact:
        return exact
    return [r for r in owner_rules(session) if v in r.value.lower() or r.value.lower() in v]
