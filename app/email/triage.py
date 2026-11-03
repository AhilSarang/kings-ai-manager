"""Email triage: rules -> authentication -> batched LLM -> actions (brief Section 6)."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import EmailAlertRule, EmailAttachment, EmailMessage
from app.email import contacts as contacts_svc
from app.email import rules as rules_svc
from app.email.parse import strip_quoted
from app.jobs import queue
from app.llm import service as llm
from app.llm.base import LLMError
from app.prompts import load as load_prompt
from app.services import memory, settings_store, tasks
from app.services.common import Actor, utcnow
from app.services.formatting import truncate

log = logging.getLogger(__name__)

BUSINESS_CONTACT_TYPES = {"amazon", "supplier", "customer_b2b", "customer_b2c", "logistics", "legal", "finance"}
CATEGORY_TO_CONTACT = {"amazon": "amazon", "supplier": "supplier", "customer_b2b": "customer_b2b",
                       "customer_b2c": "customer_b2c", "logistics": "logistics", "legal": "legal",
                       "finance_invoice": "finance"}
IMPORTANCE_ORDER = {"low": 0, "normal": 1, "high": 2, "critical": 3}
ALERT_CONFIDENCE = 0.6
BODY_CHARS = 2000
FALLBACK_SUMMARY = "Could not auto-triage — needs review"
AGENT = Actor.agent("email")

_AMAZON_NOTICE = re.compile(r"seller ?central|account health|seller performance|suspend|deactivat|"
                            r"policy (violation|warning)|listing (removed|suppressed)|verify your", re.I)
_CREDENTIAL_ASK = re.compile(
    r"verify (your )?(account|identity)|confirm (your )?(identity|account|password|details)|\blog ?in\b|"
    r"sign ?in|password|update (your )?(payment|billing|bank)|within \d+ (hours|days)|click (here|the link)|"
    r"account (will be|has been) (suspended|deactivated|closed)|gift ?card", re.I)
_PAYMENT_FRAUD = re.compile(
    r"(bank|banking|account|wire|payment|remittance|ach) (details|information|info|instructions)\s+"
    r"(have |has )?(changed|been changed|updated|been updated)|new bank(ing)? (account|details)|"
    r"change (of|in|to) (our )?bank|(send|remit|wire|pay) .{0,40}to (our|the) new (account|bank)|"
    r"updated? (our )?(bank|banking|remittance|wire) (details|information|instructions)", re.I)


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    category: Literal["amazon", "supplier", "customer_b2b", "customer_b2c", "logistics", "finance_invoice",
                      "legal", "marketing_spam", "personal", "other"]
    importance: Literal["critical", "high", "normal", "low"]
    needs_reply: bool
    alert_rule_matches: list[str] = Field(max_length=10)
    summary: str = Field(max_length=600)
    suggested_action: str = Field(max_length=300)
    confidence: float = Field(ge=0, le=1)


class TriageBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[TriageResult] = Field(max_length=50)


@dataclass
class Prepared:
    msg: EmailMessage
    key: str
    forced_rules: list[str] = field(default_factory=list)
    summary_only: bool = False
    phishing: bool = False
    phishing_warning: bool = False
    payment_fraud: bool = False
    backfill: bool = False


# ---------------- deterministic steps ----------------
def amazon_domains() -> list[str]:
    return [d.strip().lower() for d in get_settings().amazon_dkim_domains.split(",") if d.strip()]


def _domain_ok(domain: str, allowed: list[str]) -> bool:
    return any(domain == a or domain.endswith("." + a) for a in allowed)


def claims_amazon(msg: EmailMessage) -> bool:
    domain = msg.from_address.split("@")[-1]
    if "amazon" in domain or "amazon" in (msg.from_name or "").lower():
        return True
    text = f"{msg.subject}\n{msg.body_text[:4000]}"
    return "amazon" in text.lower() and bool(_AMAZON_NOTICE.search(text))


def genuine_amazon(msg: EmailMessage) -> bool:
    allowed = amazon_domains()
    return msg.auth_dkim == "pass" and any(_domain_ok(d, allowed) for d in (msg.dkim_domains or [])) \
        and _domain_ok(msg.from_address.split("@")[-1], allowed + ["amazon.com"])


def owner_rule_hits(msg: EmailMessage, rules: list[EmailAlertRule]) -> list[EmailAlertRule]:
    sender = msg.from_address.lower()
    domain = sender.split("@")[-1]
    text = f"{msg.subject}\n{msg.body_text}".lower()
    hits = []
    for r in rules:
        if r.created_by != "owner":
            continue
        if (r.rule_type == "sender" and sender == r.value) or \
                (r.rule_type == "domain" and _domain_ok(domain, [r.value])) or \
                (r.rule_type == "keyword" and r.value in text):
            hits.append(r)
    return hits


def custom_rule_key(rule: EmailAlertRule) -> str:
    return f"custom:{str(rule.id)[:8]}"


def prepare(session: Session, msg: EmailMessage, rules: list[EmailAlertRule], key: str) -> Prepared | None:
    """Apply rules and auth checks. Returns None if the message was fully handled without the LLM."""
    p = Prepared(msg=msg, key=key, backfill=bool((msg.triage_result or {}).get("backfill")))
    hits = owner_rule_hits(msg, rules)
    if any(r.alert_level == "ignore" for r in hits):
        _finish(session, msg, category=msg.thread.category or "other", importance="low", needs_reply=False,
                summary="Ignored by your rule", action="", matches=[], confidence=1.0, status="done",
                extra={"rule": "ignore"})
        msg.thread.status = "ignored"
        return None
    p.forced_rules = [str(r.id) for r in hits if r.alert_level == "instant"]
    p.summary_only = any(r.alert_level == "daily_summary_only" for r in hits)

    if claims_amazon(msg) and not genuine_amazon(msg):
        p.phishing = True
        msg.possible_phishing = True
        text = f"{msg.subject}\n{msg.body_text[:6000]}"
        p.phishing_warning = bool(_CREDENTIAL_ASK.search(text))
    p.payment_fraud = bool(_PAYMENT_FRAUD.search(f"{msg.subject}\n{msg.body_text[:8000]}"))

    contact = contacts_svc.get(session, msg.from_address)
    known_business = contact is not None and contact.type in BUSINESS_CONTACT_TYPES
    if msg.gmail_category in ("Promotions", "Social") and not known_business and not hits \
            and not p.phishing and not p.payment_fraud:
        _finish(session, msg, category="marketing_spam", importance="low", needs_reply=False,
                summary=truncate(msg.snippet or msg.subject, 200), action="", matches=[], confidence=1.0,
                status="done", extra={"rule": "promotions"})
        return None
    return p


# ---------------- LLM step ----------------
def _describe_auth(p: Prepared) -> str:
    m = p.msg
    if m.auth_dkim == "pass" and m.dkim_domains:
        auth = f"DKIM passed for {', '.join(m.dkim_domains)}"
    elif m.auth_dkim == "fail":
        auth = "DKIM failed"
    else:
        auth = "not verified"
    return f"{auth}; possible phishing: {'yes' if p.phishing else 'no'}"


def build_prompt(session: Session, batch: list[Prepared], rules: list[EmailAlertRule]) -> str:
    esc = lambda s: html.escape(s or "", quote=False)  # noqa: E731
    threshold = settings_store.get(session, "email_large_order_threshold_usd")
    custom = [r for r in rules if r.rule_type == "custom_description" and r.alert_level == "instant"]
    parts = ["<alert_rules>",
             "A, B, C, D as described in your instructions."]
    parts += [f"{custom_rule_key(r)}: {esc(r.value)}" for r in custom]
    if threshold:
        parts.append(f"Order-value threshold for C: ${threshold}")
    parts.append("</alert_rules>")
    mem = memory.active_for(session, "email")
    parts.append("<company_memory>\n" + ("\n".join(
        f"- {esc(m.title)}: {esc(truncate(m.content, 300))}" for m in mem) or "(none)") + "\n</company_memory>")
    for p in batch:
        m = p.msg
        contact = contacts_svc.get(session, m.from_address)
        ctype = f"{contact.type}{', VIP' if contact.is_vip else ''}" if contact else "unknown"
        atts = list(session.execute(
            select(EmailAttachment.filename).where(EmailAttachment.message_id == m.id)).scalars())
        body = truncate(strip_quoted(m.body_text), BODY_CHARS)
        parts.append(
            f'<email id="{p.key}">\n'
            f"From: {esc(m.from_name or '')} <{esc(m.from_address)}> (known contact type: {esc(ctype)})\n"
            f"Sender authentication: {_describe_auth(p)}\n"
            f"Subject: {esc(m.subject)}\n"
            f"Attachments: {esc(', '.join(atts)) or 'none'}\n"
            f"Thread's previous category: {m.thread.category or 'none'}\n"
            f"Body:\n{esc(body)}\n</email>"
        )
    return "\n\n".join(parts)


def _shingles(text: str, n: int = 6) -> set[str]:
    words = re.findall(r"\w+", (text or "").lower())
    return {" ".join(words[i:i + n]) for i in range(max(len(words) - n + 1, 0))}


def leaks_other_email(summary: str, own: EmailMessage, others: list[EmailMessage]) -> bool:
    """True if a summary repeats 6+ consecutive words from a *different* email in the batch."""
    mine = _shingles(summary)
    if not mine:
        return False
    own_sh = _shingles(f"{own.subject} {own.body_text}")
    for other in others:
        if other.id == own.id:
            continue
        overlap = (mine & _shingles(f"{other.subject} {other.body_text}")) - own_sh
        if overlap:
            return True
    return False


def run_llm(session: Session, batch: list[Prepared], rules: list[EmailAlertRule]) -> dict[str, TriageResult]:
    """Returns results keyed by message key. Raises jobs.errors.LLMUnavailable if the LLM is paused."""
    try:
        output, _ = llm.complete_json(TriageBatch, system=load_prompt("email_triage"),
                                      prompt=build_prompt(session, batch, rules), purpose="email_triage")
    except LLMError as exc:  # invalid after retry, refusal, etc.
        log.warning("email triage LLM failed; using fallback", extra={"error": str(exc)[:200]})
        return {}
    by_key = {p.key: p for p in batch}
    msgs = [p.msg for p in batch]
    results: dict[str, TriageResult] = {}
    for r in output.results:
        if r.message_id not in by_key or r.message_id in results:
            continue
        own = by_key[r.message_id].msg
        if leaks_other_email(f"{r.summary} {r.suggested_action}", own, msgs):
            log.warning("triage summary repeated another email; withheld", extra={"email_message_id": str(own.id)})
            r = r.model_copy(update={"summary": f"Email about: {truncate(own.subject, 150)} (summary withheld)",
                                     "suggested_action": ""})
        results[r.message_id] = r
    return results


# ---------------- actions ----------------
def _finish(session, msg, *, category, importance, needs_reply, summary, action, matches, confidence, status,
            extra=None) -> None:
    thread = msg.thread
    result = {"category": category, "importance": importance, "needs_reply": needs_reply,
              "alert_rule_matches": matches, "confidence": confidence, "summary": summary,
              "suggested_action": action, **(extra or {})}
    if (msg.triage_result or {}).get("backfill"):
        result["backfill"] = True
    msg.triage_result = result
    msg.triage_status = status
    msg.triaged_at = utcnow()
    is_latest = thread.last_message_at is None or msg.sent_at is None or msg.sent_at >= thread.last_message_at
    if is_latest or thread.category is None:
        thread.category = category
        thread.importance = importance
        thread.summary = summary
        thread.suggested_action = action or None
    thread.needs_reply = bool(needs_reply) and not thread.last_message_from_owner and thread.status != "ignored"
    thread.triage_version += 1


FRAUD_PREFIX = "⚠️ HIGH RISK — possible payment fraud (bank details change); verify by phone. "


def apply_result(session: Session, p: Prepared, r: TriageResult | None, rules: list[EmailAlertRule]) -> None:
    msg, thread = p.msg, p.msg.thread
    if r is None:
        _finish(session, msg, category="other", importance="high", needs_reply=True, summary=FALLBACK_SUMMARY,
                action="Open the email and decide", matches=[], confidence=0.0, status="failed")
        category, importance, confidence, matches = "other", "high", 0.0, []
    else:
        allowed = set(rules_svc.BUILTIN_CATEGORIES) | {custom_rule_key(x) for x in rules
                                                       if x.rule_type == "custom_description"}
        matches = [m for m in dict.fromkeys(r.alert_rule_matches) if m in allowed]
        category, importance, confidence = r.category, r.importance, r.confidence
        if p.phishing or not genuine_amazon(msg):
            matches = [m for m in matches if m != "A"]  # A needs verified Amazon mail, whatever the model says
        summary = r.summary
        if p.payment_fraud:
            if IMPORTANCE_ORDER[importance] < IMPORTANCE_ORDER["high"]:
                importance = "high"
            if category == "supplier" and "D" not in matches:
                matches.append("D")
            summary = truncate(f"{FRAUD_PREFIX}{summary}", 600)
        _finish(session, msg, category=category, importance=importance, needs_reply=r.needs_reply,
                summary=summary, action=r.suggested_action, matches=matches, confidence=confidence,
                status="done")

    flags = set(thread.risk_flags or [])
    if p.phishing:
        flags.add("possible_phishing")
    if p.payment_fraud:
        flags.add("payment_fraud_risk")
    thread.risk_flags = sorted(flags)

    # Learn the sender's type (never overriding the owner, never from suspicious mail)
    contact = contacts_svc.get(session, msg.from_address)
    if contact and not contact.type_set_by_owner and contact.type == "other" and not p.phishing \
            and category in CATEGORY_TO_CONTACT and confidence >= ALERT_CONFIDENCE:
        contact.type = CATEGORY_TO_CONTACT[category]

    if p.backfill:
        return

    alert_rules = list(matches) + p.forced_rules
    if alert_rules and not p.summary_only and (confidence >= ALERT_CONFIDENCE or importance == "critical"
                                              or p.forced_rules):
        queue.enqueue(session, "send_email_alert", {
            "thread_id": str(thread.id), "message_id": str(msg.id), "rules": alert_rules,
            "importance": importance, "kind": "alert"}, run_after=utcnow() + timedelta(seconds=20), priority=15)
    if p.phishing_warning and not p.summary_only:
        queue.enqueue(session, "send_email_alert", {
            "thread_id": str(thread.id), "message_id": str(msg.id), "rules": ["phishing"],
            "importance": "high", "kind": "phishing"}, idempotency_key=f"phish:{thread.id}", priority=15)

    if importance in ("critical", "high") and thread.needs_reply and thread.linked_task_id is None \
            and not p.phishing and r is not None:
        task = tasks.create_task(
            session, title=truncate(f"Reply to {thread.ref}: {r.summary}", 300),
            description=f"Email thread {thread.ref} from {msg.from_address}: {msg.subject}",
            priority="urgent" if importance == "critical" else "high", agent_key="email", actor=AGENT,
            metadata={"email_thread": thread.ref},
        )
        if task.status == "queued":
            tasks.change_status(session, task, "waiting_owner", AGENT)
        thread.linked_task_id = task.id


def triage_pending(session: Session, *, max_batches: int = 5) -> int:
    """Triage waiting messages in batches. Returns how many were processed."""
    batch_size = int(settings_store.get(session, "email_triage_batch_size", 10) or 10)
    done = 0
    for _ in range(max_batches):
        pending = list(session.execute(
            select(EmailMessage).where(EmailMessage.triage_status == "pending")
            .order_by(EmailMessage.sent_at.asc().nulls_last(), EmailMessage.created_at)
            .limit(batch_size).with_for_update(skip_locked=True, of=EmailMessage)
        ).scalars())
        if not pending:
            break
        rules = rules_svc.active_rules(session)
        batch: list[Prepared] = []
        for i, msg in enumerate(pending, start=1):
            p = prepare(session, msg, rules, key=f"m{i}")
            if p is not None:
                batch.append(p)
            else:
                done += 1
        if batch:
            results = run_llm(session, batch, rules)  # LLMUnavailable propagates: batch stays pending
            for p in batch:
                apply_result(session, p, results.get(p.key), rules)
                done += 1
        session.commit()
    return done

