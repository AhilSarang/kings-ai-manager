"""ORM models for every table in Section 6 of the Phase 1 brief.

Enum-like columns are plain strings guarded by CHECK constraints (see
docs/DECISIONS.md D10). Human-readable refs (T-1, A-1) come from Postgres
sequences via server defaults.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# ---- allowed values (single source of truth) ----
USER_ROLES = ("owner", "manager", "viewer")
MESSAGE_DIRECTIONS = ("inbound", "outbound")
MESSAGE_STATUSES = ("received", "processed", "held", "queued", "sent", "delivered", "read", "failed", "ignored")
AGENT_STATUSES = ("active", "inactive", "paused")
TASK_STATUSES = ("new", "queued", "in_progress", "waiting_approval", "waiting_owner", "blocked", "done", "cancelled", "failed")
OPEN_TASK_STATUSES = ("new", "queued", "in_progress", "waiting_approval", "waiting_owner", "blocked")
TASK_PRIORITIES = ("urgent", "high", "normal", "low")
TASK_EVENT_TYPES = ("created", "status_changed", "note", "assigned", "approval_requested", "approval_decided", "file_attached", "result_added", "updated", "reminder_sent")
ACTOR_TYPES = ("user", "agent", "system")
RISK_LEVELS = ("low", "medium", "high", "critical")
APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "executed", "execution_failed")
JOB_STATUSES = ("pending", "running", "done", "failed", "waiting_llm", "cancelled")
FILE_SOURCES = ("whatsapp", "agent", "upload", "email")
MEMORY_CATEGORIES = ("sop", "company_rule", "owner_preference", "procedure", "contact_note")
MEMORY_CREATORS = ("user", "agent")

task_ref_seq = Sequence("task_ref_seq")
approval_ref_seq = Sequence("approval_ref_seq")
error_ref_seq = Sequence("error_ref_seq")
email_thread_ref_seq = Sequence("email_thread_ref_seq")
email_draft_ref_seq = Sequence("email_draft_ref_seq")

# ---- Phase 3: amazon ----
AMAZON_ACCOUNT_STATUSES = ("active", "auth_error", "paused")
AMAZON_WRITE_MODES = ("validation_only", "live")
AMAZON_PARENTAGE = ("parent", "child", "standalone")
AMAZON_FULFILLMENT = ("FBA", "FBM")
AMAZON_LISTING_OPERATIONS = ("create_new_asin", "create_offer_existing_asin", "patch_attributes", "full_replace",
                             "update_price", "create_variation_family", "add_variation_child",
                             "remove_variation_child", "delete_listing", "revert")
AMAZON_CHANGE_STATUSES = ("drafting", "needs_info", "validating", "ready_for_approval", "pending_approval",
                          "approved", "submitted", "accepted", "live_verified", "completed_with_issues",
                          "rejected_by_amazon", "cancelled", "failed")
AMAZON_VERSION_SOURCES = ("amazon_sync", "submitted_change", "revert")
AMAZON_IMAGE_STATUSES = ("pending", "checked", "rejected", "hosted", "live")
AMAZON_ALERT_TYPES = ("listing_suppressed", "listing_issue", "buy_box_lost", "low_stock", "stockout", "stranded",
                      "sales_drop", "price_anomaly", "submission_rejected", "shipment_problem",
                      "receiving_discrepancy")
AMAZON_ALERT_STATUSES = ("open", "acknowledged", "resolved", "ignored")
FBA_SHIPPING_MODES = ("small_parcel", "ltl_pallet")
FBA_LABEL_OWNERS = ("SELLER", "AMAZON", "NONE")
FBA_PREP_OWNERS = ("SELLER", "AMAZON", "NONE")
FBA_CARRIER_CHOICES = ("partnered", "own_carrier")
FBA_REQUEST_STATUSES = ("drafting", "needs_info", "prechecks_passed", "building_plan", "options_ready",
                        "pending_approval", "confirming", "confirmed", "in_transit", "receiving", "closed",
                        "closed_with_discrepancy", "expired", "cancelled", "failed")

amazon_listing_ref_seq = Sequence("amazon_listing_ref_seq")
amazon_change_ref_seq = Sequence("amazon_change_ref_seq")
amazon_alert_ref_seq = Sequence("amazon_alert_ref_seq")
fba_request_ref_seq = Sequence("fba_request_ref_seq")

# ---- Phase 2: email ----
EMAIL_ACCOUNT_STATUSES = ("active", "auth_error", "paused")
EMAIL_CATEGORIES = ("amazon", "supplier", "customer_b2b", "customer_b2c", "logistics", "finance_invoice",
                    "legal", "marketing_spam", "personal", "other")
EMAIL_IMPORTANCE = ("critical", "high", "normal", "low")
EMAIL_THREAD_STATUSES = ("open", "waiting_on_them", "handled", "ignored")
EMAIL_TRIAGE_STATUSES = ("pending", "done", "failed", "skipped", "backfill")
EMAIL_DRAFT_STATUSES = ("draft", "pending_approval", "sent", "discarded", "send_failed")
EMAIL_RULE_TYPES = ("builtin_category", "sender", "domain", "keyword", "custom_description")
EMAIL_ALERT_LEVELS = ("instant", "daily_summary_only", "ignore")
EMAIL_RULE_CREATORS = ("system", "owner")
EMAIL_CONTACT_TYPES = ("amazon", "supplier", "customer_b2b", "customer_b2c", "logistics", "legal", "finance",
                       "other")
AUTH_RESULTS = ("pass", "fail", "none")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


class TimestampMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"), default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", USER_ROLES), name="role"),)

    name: Mapped[str] = mapped_column(String(200))
    whatsapp_number: Mapped[str] = mapped_column(String(20), unique=True)
    email: Mapped[str | None] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    approval_pin_hash: Mapped[str | None] = mapped_column(String(255))
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Agent(TimestampMixin, Base):
    __tablename__ = "agents"
    __table_args__ = (CheckConstraint(_in("status", AGENT_STATUSES), name="status"),)

    key: Mapped[str] = mapped_column(String(40), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="inactive")
    description: Mapped[str] = mapped_column(Text, default="")
    permissions: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))


class File(TimestampMixin, Base):
    __tablename__ = "files"
    __table_args__ = (CheckConstraint(_in("source", FILE_SOURCES), name="source"),)

    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    original_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(20))
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    related_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), index=True)


class Message(TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(_in("direction", MESSAGE_DIRECTIONS), name="direction"),
        CheckConstraint(_in("status", MESSAGE_STATUSES), name="status"),
        Index("ix_messages_user_created", "user_id", "created_at"),
    )

    direction: Mapped[str] = mapped_column(String(10))
    channel: Mapped[str] = mapped_column(String(20), default="whatsapp")
    provider_message_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text, default="")
    media_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    status: Mapped[str] = mapped_column(String(20))
    related_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(_in("status", TASK_STATUSES), name="status"),
        CheckConstraint(_in("priority", TASK_PRIORITIES), name="priority"),
        Index("ix_tasks_status_priority", "status", "priority"),
    )

    ref: Mapped[str] = mapped_column(
        String(20), unique=True, server_default=text("'T-' || nextval('task_ref_seq')")
    )
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="new")
    priority: Mapped[str] = mapped_column(String(10), default="normal")
    assigned_agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", use_alter=True, name="fk_tasks_source_message_id_messages")
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    result_summary: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", default=dict, server_default=text("'{}'::jsonb")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    agent: Mapped[Agent | None] = relationship(lazy="joined")
    subtasks: Mapped[list[Task]] = relationship(back_populates="parent", lazy="selectin")
    parent: Mapped[Task | None] = relationship(back_populates="subtasks", remote_side="Task.id")


class TaskEvent(TimestampMixin, Base):
    __tablename__ = "task_events"
    __table_args__ = (
        CheckConstraint(_in("event_type", TASK_EVENT_TYPES), name="event_type"),
        CheckConstraint(_in("actor_type", ACTOR_TYPES), name="actor_type"),
        Index("ix_task_events_task_created", "task_id", "created_at"),
    )

    # clock_timestamp() (not now()) so events written in one transaction keep their order
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()"), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"))
    event_type: Mapped[str] = mapped_column(String(30))
    actor_type: Mapped[str] = mapped_column(String(10))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))


class Approval(TimestampMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(_in("risk_level", RISK_LEVELS), name="risk_level"),
        CheckConstraint(_in("status", APPROVAL_STATUSES), name="status"),
    )

    ref: Mapped[str] = mapped_column(
        String(20), unique=True, server_default=text("'A-' || nextval('approval_ref_seq')")
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    requested_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id"))
    action_type: Mapped[str] = mapped_column(String(80))
    action_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    risk_level: Mapped[str] = mapped_column(String(10))
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    confirmation_code: Mapped[str] = mapped_column(String(8))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        Index("ix_jobs_ready", "status", "run_after"),
    )

    type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default=text("100"))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    uses_llm: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class MemoryEntry(TimestampMixin, Base):
    __tablename__ = "memory_entries"
    __table_args__ = (
        CheckConstraint(_in("category", MEMORY_CATEGORIES), name="category"),
        CheckConstraint(_in("created_by", MEMORY_CREATORS), name="created_by"),
    )

    category: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    applies_to_agent_key: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_by: Mapped[str] = mapped_column(String(10))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(_in("actor_type", ACTOR_TYPES), name="actor_type"),
        Index("ix_audit_log_occurred", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_type: Mapped[str] = mapped_column(String(10))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(80), index=True)
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    trigger_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class LLMCall(TimestampMixin, Base):
    __tablename__ = "llm_calls"

    backend: Mapped[str] = mapped_column(String(30))
    model: Mapped[str | None] = mapped_column(String(80))
    purpose: Mapped[str] = mapped_column(String(60))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    error: Mapped[str | None] = mapped_column(Text)
    prompt_hash: Mapped[str | None] = mapped_column(String(64))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Heartbeat(Base):
    """Liveness records for long-running processes (M3)."""

    __tablename__ = "heartbeats"

    component: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    details: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    """Dedupe ledger for one-off notices (reminders, alerts, LLM pause notice)."""

    __tablename__ = "notifications"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# =====================================================================
# Phase 2: Email Agent (docs/PHASE_2_EMAIL_BRIEF.md, Section 5)
# =====================================================================
class EmailAccount(TimestampMixin, Base):
    __tablename__ = "email_accounts"
    __table_args__ = (CheckConstraint(_in("status", EMAIL_ACCOUNT_STATUSES), name="status"),
                      CheckConstraint("provider = 'gmail'", name="provider"))

    address: Mapped[str] = mapped_column(String(320), unique=True)
    provider: Mapped[str] = mapped_column(String(20), default="gmail", server_default=text("'gmail'"))
    status: Mapped[str] = mapped_column(String(20), default="paused", server_default=text("'paused'"))
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    last_history_id: Mapped[int | None] = mapped_column(BigInteger)
    watch_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_reason: Mapped[str | None] = mapped_column(Text)


class EmailThread(TimestampMixin, Base):
    __tablename__ = "email_threads"
    __table_args__ = (
        UniqueConstraint("account_id", "provider_thread_id", name="uq_email_threads_account_thread"),
        CheckConstraint(f"category IS NULL OR {_in('category', EMAIL_CATEGORIES)}", name="category"),
        CheckConstraint(f"importance IS NULL OR {_in('importance', EMAIL_IMPORTANCE)}", name="importance"),
        CheckConstraint(_in("status", EMAIL_THREAD_STATUSES), name="status"),
        Index("ix_email_threads_status_last", "status", "last_message_at"),
    )

    ref: Mapped[str] = mapped_column(
        String(20), unique=True, server_default=text("'E-' || nextval('email_thread_ref_seq')"))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email_accounts.id"))
    provider_thread_id: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    participants: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    first_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_from_owner: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    category: Mapped[str | None] = mapped_column(String(30))
    importance: Mapped[str | None] = mapped_column(String(10))
    needs_reply: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(20), default="open", server_default=text("'open'"))
    follow_up_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    follow_up_tracking: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    alert_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    alerted_rules: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    alerted_importance: Mapped[str | None] = mapped_column(String(10))
    linked_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    summary: Mapped[str | None] = mapped_column(Text)
    suggested_action: Mapped[str | None] = mapped_column(Text)
    risk_flags: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    triage_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Links to later phases (nullable, no FK yet)
    amazon_case_id: Mapped[str | None] = mapped_column(String(64))
    supplier_id: Mapped[str | None] = mapped_column(String(64))

    account: Mapped[EmailAccount] = relationship(lazy="joined")


class EmailMessage(TimestampMixin, Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        CheckConstraint(_in("auth_spf", AUTH_RESULTS), name="auth_spf"),
        CheckConstraint(_in("auth_dkim", AUTH_RESULTS), name="auth_dkim"),
        CheckConstraint(_in("auth_dmarc", AUTH_RESULTS), name="auth_dmarc"),
        CheckConstraint(_in("triage_status", EMAIL_TRIAGE_STATUSES), name="triage_status"),
        Index("ix_email_messages_thread_sent", "thread_id", "sent_at"),
        Index("ix_email_messages_triage", "triage_status", "created_at"),
    )

    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email_threads.id"))
    provider_message_id: Mapped[str] = mapped_column(String(64), unique=True)
    rfc_message_id: Mapped[str | None] = mapped_column(String(998))
    from_address: Mapped[str] = mapped_column(String(320), default="", server_default=text("''"), index=True)
    from_name: Mapped[str | None] = mapped_column(String(320))
    to: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    cc: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    reply_to: Mapped[str | None] = mapped_column(String(998))
    subject: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    body_text: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    snippet: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    gmail_labels: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    gmail_category: Mapped[str | None] = mapped_column(String(20))
    auth_spf: Mapped[str] = mapped_column(String(10), default="none", server_default=text("'none'"))
    auth_dkim: Mapped[str] = mapped_column(String(10), default="none", server_default=text("'none'"))
    auth_dmarc: Mapped[str] = mapped_column(String(10), default="none", server_default=text("'none'"))
    dkim_domains: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    is_from_owner: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_self_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    possible_phishing: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    headers: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    triage_status: Mapped[str] = mapped_column(String(10), default="pending", server_default=text("'pending'"))
    triage_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    thread: Mapped[EmailThread] = relationship(lazy="joined")


class EmailAttachment(TimestampMixin, Base):
    __tablename__ = "email_attachments"

    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email_messages.id"), index=True)
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    provider_attachment_id: Mapped[str | None] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(255), default="", server_default=text("''"))
    mime_type: Mapped[str] = mapped_column(String(255), default="", server_default=text("''"))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    skipped_reason: Mapped[str | None] = mapped_column(Text)


class EmailDraft(TimestampMixin, Base):
    __tablename__ = "email_drafts"
    __table_args__ = (CheckConstraint(_in("status", EMAIL_DRAFT_STATUSES), name="status"),)

    ref: Mapped[str] = mapped_column(
        String(20), unique=True, server_default=text("'D-' || nextval('email_draft_ref_seq')"))
    thread_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("email_threads.id"), index=True)  # None: new email
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    owner_instructions: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    to: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    cc: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    subject: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    body_text: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    notes: Mapped[str | None] = mapped_column(Text)
    gmail_draft_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default=text("'draft'"))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id"))
    sent_provider_message_id: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    attachment_file_ids: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))

    thread: Mapped[EmailThread | None] = relationship(lazy="joined")


class EmailAlertRule(TimestampMixin, Base):
    __tablename__ = "email_alert_rules"
    __table_args__ = (
        CheckConstraint(_in("rule_type", EMAIL_RULE_TYPES), name="rule_type"),
        CheckConstraint(_in("alert_level", EMAIL_ALERT_LEVELS), name="alert_level"),
        CheckConstraint(_in("created_by", EMAIL_RULE_CREATORS), name="created_by"),
    )

    name: Mapped[str] = mapped_column(String(200))
    rule_type: Mapped[str] = mapped_column(String(30))
    value: Mapped[str] = mapped_column(Text)
    alert_level: Mapped[str] = mapped_column(String(30))
    bypass_quiet_hours: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_by: Mapped[str] = mapped_column(String(10))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class EmailContact(TimestampMixin, Base):
    __tablename__ = "email_contacts"
    __table_args__ = (CheckConstraint(_in("type", EMAIL_CONTACT_TYPES), name="type"),)

    address: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str | None] = mapped_column(String(320))
    domain: Mapped[str] = mapped_column(String(255), index=True)
    type: Mapped[str] = mapped_column(String(20), default="other", server_default=text("'other'"))
    type_set_by_owner: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    notes: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


# ---------------- Phase 3: Amazon ----------------
class AmazonAccount(TimestampMixin, Base):
    __tablename__ = "amazon_accounts"
    __table_args__ = (CheckConstraint(_in("status", AMAZON_ACCOUNT_STATUSES), name="status"),
                      CheckConstraint(_in("write_mode", AMAZON_WRITE_MODES), name="write_mode"))

    seller_id: Mapped[str] = mapped_column(String(40), unique=True)
    region: Mapped[str] = mapped_column(String(4), default="na", server_default=text("'na'"))
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="paused", server_default=text("'paused'"))
    status_reason: Mapped[str | None] = mapped_column(Text)
    enabled_marketplaces: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    write_mode: Mapped[str] = mapped_column(String(20), default="validation_only",
                                            server_default=text("'validation_only'"))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Brand(TimestampMixin, Base):
    __tablename__ = "brands"

    name: Mapped[str] = mapped_column(String(120), unique=True)
    brand_registry: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    owned_by_company: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    notes: Mapped[str | None] = mapped_column(Text)


class Product(TimestampMixin, Base):
    """Our own product record: costs and case packs the agent must never invent."""

    __tablename__ = "products"

    internal_name: Mapped[str] = mapped_column(String(200))
    brand_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("brands.id"), index=True)
    unit_cost: Mapped[float | None] = mapped_column(Numeric(12, 2))
    cost_currency: Mapped[str] = mapped_column(String(3), default="USD", server_default=text("'USD'"))
    supplier_ref: Mapped[str | None] = mapped_column(String(200))
    prep_category: Mapped[str | None] = mapped_column(String(60))
    units_per_case: Mapped[int | None] = mapped_column(Integer)
    case_dimensions: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    case_weight_lb: Mapped[float | None] = mapped_column(Numeric(8, 2))
    notes: Mapped[str | None] = mapped_column(Text)

    brand: Mapped[Brand | None] = relationship(lazy="joined")


class AmazonListing(TimestampMixin, Base):
    __tablename__ = "amazon_listings"
    __table_args__ = (
        UniqueConstraint("account_id", "marketplace_id", "sku", name="account_marketplace_sku"),
        CheckConstraint(_in("parentage", AMAZON_PARENTAGE), name="parentage"),
        CheckConstraint(_in("fulfillment", AMAZON_FULFILLMENT), name="fulfillment"),
        Index("ix_amazon_listings_asin", "asin"),
    )

    ref: Mapped[str] = mapped_column(
        String(20), unique=True, server_default=text("'L-' || nextval('amazon_listing_ref_seq')"))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("amazon_accounts.id"), index=True)
    marketplace_id: Mapped[str] = mapped_column(String(20), index=True)
    sku: Mapped[str] = mapped_column(String(80))
    asin: Mapped[str | None] = mapped_column(String(20))
    fnsku: Mapped[str | None] = mapped_column(String(20))
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id"), index=True)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("brands.id"))
    product_type: Mapped[str | None] = mapped_column(String(80))
    parentage: Mapped[str] = mapped_column(String(20), default="standalone", server_default=text("'standalone'"))
    parent_listing_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("amazon_listings.id"))
    variation_theme: Mapped[str | None] = mapped_column(String(80))
    fulfillment: Mapped[str] = mapped_column(String(10), default="FBA", server_default=text("'FBA'"))
    title_cached: Mapped[str | None] = mapped_column(Text)
    price_cached: Mapped[float | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    status_cached: Mapped[str | None] = mapped_column(String(40))
    issues_cached: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    buyable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("amazon_listing_versions.id", use_alter=True, name="fk_listing_current_version"))

    brand: Mapped[Brand | None] = relationship(lazy="joined")
    product: Mapped[Product | None] = relationship(lazy="joined")


class AmazonListingVersion(TimestampMixin, Base):
    __tablename__ = "amazon_listing_versions"
    __table_args__ = (CheckConstraint(_in("source", AMAZON_VERSION_SOURCES), name="source"),
                      UniqueConstraint("listing_id", "version_number", name="listing_version"))

    listing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("amazon_listings.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(20))
    change_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("amazon_change_requests.id", use_alter=True, name="fk_listing_version_change_request"))


class AmazonChangeRequest(TimestampMixin, Base):
    __tablename__ = "amazon_change_requests"
    __table_args__ = (CheckConstraint(_in("operation", AMAZON_LISTING_OPERATIONS), name="operation"),
                      CheckConstraint(_in("status", AMAZON_CHANGE_STATUSES), name="status"),
                      CheckConstraint(_in("risk_level", RISK_LEVELS), name="risk_level"))

    ref: Mapped[str] = mapped_column(
        String(20), unique=True, server_default=text("'LC-' || nextval('amazon_change_ref_seq')"))
    listing_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("amazon_listings.id"), index=True)
    marketplace_id: Mapped[str] = mapped_column(String(20))
    operation: Mapped[str] = mapped_column(String(40))
    owner_instructions: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    before_snapshot_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("amazon_listing_versions.id"))
    diff: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    local_validation: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict,
                                                             server_default=text("'{}'::jsonb"))
    amazon_preview_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    risk_level: Mapped[str] = mapped_column(String(20), default="medium", server_default=text("'medium'"))
    requires_pin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    warnings: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(30), default="drafting", server_default=text("'drafting'"))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id"))
    submission_id: Mapped[str | None] = mapped_column(String(80))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_issues: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))

    listing: Mapped[AmazonListing | None] = relationship(lazy="joined")


class AmazonImage(TimestampMixin, Base):
    __tablename__ = "amazon_images"
    __table_args__ = (CheckConstraint(_in("status", AMAZON_IMAGE_STATUSES), name="status"),)

    listing_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("amazon_listings.id"), index=True)
    change_request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("amazon_change_requests.id"), index=True)
    drive_file_id: Mapped[str | None] = mapped_column(String(120))
    drive_url: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    public_url: Mapped[str | None] = mapped_column(Text)
    slot: Mapped[str | None] = mapped_column(String(40))
    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)
    format: Mapped[str | None] = mapped_column(String(20))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    checks: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"))


class AmazonProductTypeSchema(TimestampMixin, Base):
    __tablename__ = "amazon_product_type_schemas"
    __table_args__ = (UniqueConstraint("product_type", "marketplace_id", "locale", name="type_marketplace_locale"),)

    product_type: Mapped[str] = mapped_column(String(80))
    marketplace_id: Mapped[str] = mapped_column(String(20))
    locale: Mapped[str] = mapped_column(String(10))
    schema_version: Mapped[str | None] = mapped_column(String(40))
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AmazonInventorySnapshot(Base):
    __tablename__ = "amazon_inventory_snapshots"
    __table_args__ = (Index("ix_amazon_inventory_sku_at", "sku", "snapshot_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          server_default=text("gen_random_uuid()"))
    marketplace_id: Mapped[str] = mapped_column(String(20))
    sku: Mapped[str] = mapped_column(String(80))
    asin: Mapped[str | None] = mapped_column(String(20))
    fulfillable_qty: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    inbound_qty: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    reserved_qty: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    unfulfillable_qty: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AmazonSalesDaily(Base):
    __tablename__ = "amazon_sales_daily"
    __table_args__ = (UniqueConstraint("marketplace_id", "asin", "sku", "date", name="marketplace_asin_sku_date"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          server_default=text("gen_random_uuid()"))
    marketplace_id: Mapped[str] = mapped_column(String(20))
    asin: Mapped[str] = mapped_column(String(20))
    sku: Mapped[str] = mapped_column(String(80), default="", server_default=text("''"))
    date: Mapped[datetime] = mapped_column(Date)
    units: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    ordered_product_sales: Mapped[float | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    sessions: Mapped[int | None] = mapped_column(Integer)
    page_views: Mapped[int | None] = mapped_column(Integer)
    unit_session_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
    buy_box_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))


class AmazonAlert(TimestampMixin, Base):
    __tablename__ = "amazon_alerts"
    __table_args__ = (CheckConstraint(_in("type", AMAZON_ALERT_TYPES), name="type"),
                      CheckConstraint(_in("severity", RISK_LEVELS), name="severity"),
                      CheckConstraint(_in("status", AMAZON_ALERT_STATUSES), name="status"))

    ref: Mapped[str] = mapped_column(
        String(20), unique=True, server_default=text("'AZ-' || nextval('amazon_alert_ref_seq')"))
    type: Mapped[str] = mapped_column(String(30), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="medium", server_default=text("'medium'"))
    marketplace_id: Mapped[str | None] = mapped_column(String(20))
    sku: Mapped[str | None] = mapped_column(String(80), index=True)
    asin: Mapped[str | None] = mapped_column(String(20))
    shipment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("fba_shipments.id", use_alter=True, name="fk_amazon_alert_shipment"))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), default="open", server_default=text("'open'"))
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    linked_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    linked_email_thread_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("email_threads.id"))


class AmazonApiCall(Base):
    """One row per SP-API call: no tokens, no bodies."""

    __tablename__ = "amazon_api_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          server_default=text("gen_random_uuid()"))
    operation: Mapped[str] = mapped_column(String(80), index=True)
    marketplace_id: Mapped[str | None] = mapped_column(String(20))
    sku: Mapped[str | None] = mapped_column(String(80))
    http_status: Mapped[int | None] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String(80))
    operation_id: Mapped[str | None] = mapped_column(String(80))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(200))
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ShipFromAddress(TimestampMixin, Base):
    __tablename__ = "ship_from_addresses"

    label: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    company: Mapped[str | None] = mapped_column(String(120))
    address_line1: Mapped[str] = mapped_column(String(180))
    address_line2: Mapped[str | None] = mapped_column(String(180))
    city: Mapped[str] = mapped_column(String(80))
    state_or_province: Mapped[str] = mapped_column(String(40))
    postal_code: Mapped[str] = mapped_column(String(20))
    country_code: Mapped[str] = mapped_column(String(2), default="US", server_default=text("'US'"))
    phone: Mapped[str | None] = mapped_column(String(30))
    email: Mapped[str | None] = mapped_column(String(320))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class FbaInboundRequest(TimestampMixin, Base):
    __tablename__ = "fba_inbound_requests"
    __table_args__ = (CheckConstraint(_in("status", FBA_REQUEST_STATUSES), name="status"),
                      CheckConstraint(_in("shipping_mode", FBA_SHIPPING_MODES), name="shipping_mode"),
                      CheckConstraint(_in("label_owner", FBA_LABEL_OWNERS), name="label_owner"),
                      CheckConstraint(_in("prep_owner", FBA_PREP_OWNERS), name="prep_owner"))

    ref: Mapped[str] = mapped_column(
        String(20), unique=True, server_default=text("'FB-' || nextval('fba_request_ref_seq')"))
    marketplace_id: Mapped[str] = mapped_column(String(20))
    ship_from_address_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ship_from_addresses.id"))
    owner_instructions: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    shipping_mode: Mapped[str] = mapped_column(String(20), default="small_parcel",
                                               server_default=text("'small_parcel'"))
    ready_to_ship_date: Mapped[datetime | None] = mapped_column(Date)
    label_owner: Mapped[str] = mapped_column(String(10), default="SELLER", server_default=text("'SELLER'"))
    prep_owner: Mapped[str] = mapped_column(String(10), default="SELLER", server_default=text("'SELLER'"))
    status: Mapped[str] = mapped_column(String(30), default="drafting", server_default=text("'drafting'"))
    inbound_plan_id: Mapped[str | None] = mapped_column(String(80))
    packing_option_id: Mapped[str | None] = mapped_column(String(80))
    selected_placement_option_id: Mapped[str | None] = mapped_column(String(80))
    selected_transport_choice: Mapped[str | None] = mapped_column(String(20))
    delivery_window_pref: Mapped[str | None] = mapped_column(String(120))   # "earliest", "week of Oct 6"
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id"))
    estimated_fees: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confirmed_fees: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    error: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ship_from: Mapped[ShipFromAddress | None] = relationship(lazy="joined")


class FbaInboundItem(TimestampMixin, Base):
    __tablename__ = "fba_inbound_items"

    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fba_inbound_requests.id"), index=True)
    listing_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("amazon_listings.id"))
    asin: Mapped[str | None] = mapped_column(String(20))
    msku: Mapped[str] = mapped_column(String(80))
    fnsku: Mapped[str | None] = mapped_column(String(20))
    quantity: Mapped[int] = mapped_column(Integer)
    prep_category: Mapped[str | None] = mapped_column(String(60))
    # Amazon decides these per SKU (listPrepDetails); a stickerless SKU must be labelOwner NONE.
    label_owner: Mapped[str | None] = mapped_column(String(10))
    prep_owner: Mapped[str | None] = mapped_column(String(10))
    expiration_date: Mapped[datetime | None] = mapped_column(Date)
    manufacturing_lot_code: Mapped[str | None] = mapped_column(String(60))


class FbaBox(TimestampMixin, Base):
    __tablename__ = "fba_boxes"

    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fba_inbound_requests.id"), index=True)
    box_group: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    box_count: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    length_in: Mapped[float] = mapped_column(Numeric(8, 2))
    width_in: Mapped[float] = mapped_column(Numeric(8, 2))
    height_in: Mapped[float] = mapped_column(Numeric(8, 2))
    weight_lb: Mapped[float] = mapped_column(Numeric(8, 2))
    contents: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    amazon_box_ids: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))


class FbaPlacementOption(TimestampMixin, Base):
    __tablename__ = "fba_placement_options"

    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fba_inbound_requests.id"), index=True)
    amazon_placement_option_id: Mapped[str] = mapped_column(String(80))
    option_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(30))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    placement_fees: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    discounts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    shipment_count: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    shipments_preview: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    transport_quotes: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    total_estimated_cost: Mapped[float | None] = mapped_column(Numeric(12, 2))


class FbaShipment(TimestampMixin, Base):
    __tablename__ = "fba_shipments"

    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fba_inbound_requests.id"), index=True)
    amazon_shipment_id: Mapped[str | None] = mapped_column(String(80), index=True)
    shipment_confirmation_id: Mapped[str | None] = mapped_column(String(40), index=True)
    destination_fc: Mapped[str | None] = mapped_column(String(20))
    destination_address: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str | None] = mapped_column(String(40))
    carrier: Mapped[str | None] = mapped_column(String(80))
    transport_mode: Mapped[str | None] = mapped_column(String(30))
    tracking_numbers: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    box_label_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    carrier_label_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    bill_of_lading_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("files.id"))
    delivery_window: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    units_expected: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    units_received: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------- PPC agent (Phase 4) ----------------
# Naming rule, applied without exception below: a column holding *Amazon's* identifier is
# `amazon_*` and a String; a column holding *our* row id is `*_id` and a uuid foreign key. The
# schema review found these two conflated across the fact tables, which would have made the
# experiment evaluation join silently return nothing.
PPC_PROFILE_STATUSES = ("active", "auth_error", "paused")
PPC_WRITE_MODES = ("read_only", "live")
PPC_ACCOUNT_TYPES = ("seller", "vendor", "agency")
PPC_ENTITY_STATES = ("enabled", "paused", "archived")
# Which Amazon ad product a row belongs to. The two-letter forms match the prefixes Amazon's own
# report types use — spCampaigns, sbCampaigns, sdCampaigns — so a loader takes it from the report it
# just asked for rather than deciding for itself. See migration 0016 for why every uniqueness key
# below includes it.
PPC_AD_PRODUCTS = ("sp", "sb", "sd")
PPC_AD_PRODUCT_NAMES = {"sp": "Sponsored Products", "sb": "Sponsored Brands",
                        "sd": "Sponsored Display"}

PPC_TARGET_KINDS = ("keyword", "target")
PPC_TARGET_TYPES = ("keyword", "product", "category", "auto", "audience")
PPC_MATCH_TYPES = ("exact", "phrase", "broad", "auto_close", "auto_loose", "auto_substitutes",
                   "auto_complements", "asin", "category", "audience")
PPC_ENTITY_KINDS = ("campaign", "ad_group", "target", "product_ad", "placement", "search_term", "asin")
PPC_CHANGE_TYPES = ("bid_increase", "bid_decrease", "pause_target", "enable_target", "add_negative_exact",
                    "add_negative_phrase", "harvest_search_term", "budget_increase", "budget_decrease",
                    "placement_adjustment", "campaign_structure", "dayparting", "other")
PPC_RECOMMENDATION_STATUSES = ("proposed", "approved", "rejected", "executed", "expired", "superseded",
                               "failed", "rolled_back")
PPC_RISK_LEVELS = ("low", "medium", "high")
PPC_VERDICTS = ("positive", "neutral", "negative", "inconclusive")
PPC_EXPERIMENT_STATUSES = ("designed", "pending_approval", "running", "stopped_early", "completed", "abandoned")
PPC_EXPERIMENT_ORIGINS = ("agent_idea", "owner_idea", "research_note", "playbook_review")
PPC_EXPERIMENT_CONCLUSIONS = ("supported", "not_supported", "inconclusive")
PPC_ARMS = ("treatment", "control")
PPC_PLAYBOOK_STATUSES = ("proposed", "active", "on_probation", "retired")
PPC_PLAYBOOK_ACTION_KINDS = ("add", "modify", "suppress")
PPC_PLAYBOOK_ORIGINS = ("experiment", "outcome_analysis", "owner_rejection_pattern")
PPC_HARVEST_STATUSES = ("new", "harvested", "negated", "watching", "ignored")
PPC_RESEARCH_STATUSES = ("untested", "experiment_designed", "tested_supported", "tested_rejected",
                         "not_applicable")
PPC_RESEARCH_SOURCES = ("amazon_docs", "amazon_changelog", "industry_article", "owner_input")
PPC_RULE_SCOPES = ("account", "portfolio", "campaign", "asin", "brand")
PPC_ALERT_TYPES = ("spend_spike", "budget_exhausted_early", "cpc_jump", "zero_sales_with_spend",
                   "account_spend_cap", "experiment_aborted", "ad_serving_problem", "rule_breached")
PPC_CALENDAR_KINDS = ("prime_day", "black_friday", "cyber_monday", "q4_peak", "owner_note", "other")


class PpcProfile(TimestampMixin, Base):
    """One Amazon Ads marketplace profile. Everything else hangs off this."""

    __tablename__ = "ppc_profiles"
    __table_args__ = (
        CheckConstraint(_in("account_type", PPC_ACCOUNT_TYPES), name="account_type"),
        CheckConstraint(_in("status", PPC_PROFILE_STATUSES), name="status"),
        CheckConstraint(_in("write_mode", PPC_WRITE_MODES), name="write_mode"),
        UniqueConstraint("amazon_profile_id", "marketplace_id", name="profile_marketplace"),
    )

    amazon_profile_id: Mapped[str] = mapped_column(String(20), unique=True)
    ads_account_id: Mapped[str | None] = mapped_column(String(80))   # Amazon-Ads-AccountId (reporting v3)
    marketplace_id: Mapped[str] = mapped_column(String(20))
    country_code: Mapped[str] = mapped_column(String(2))
    region: Mapped[str] = mapped_column(String(4), server_default=text("'na'"))
    currency: Mapped[str] = mapped_column(String(3))
    timezone: Mapped[str] = mapped_column(String(60))
    account_type: Mapped[str] = mapped_column(String(20), default="seller", server_default=text("'seller'"))
    account_name: Mapped[str | None] = mapped_column(String(200))
    seller_id: Mapped[str | None] = mapped_column(String(40))
    amazon_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("amazon_accounts.id"), index=True)
    # A newly discovered profile is paused until the owner enables it, exactly as a new Amazon account is.
    status: Mapped[str] = mapped_column(String(20), default="paused", server_default=text("'paused'"))
    status_reason: Mapped[str | None] = mapped_column(Text)
    write_mode: Mapped[str] = mapped_column(String(20), default="read_only",
                                            server_default=text("'read_only'"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PpcPortfolio(TimestampMixin, Base):
    __tablename__ = "ppc_portfolios"
    __table_args__ = (
        CheckConstraint(_in("state", PPC_ENTITY_STATES), name="state"),
        UniqueConstraint("profile_id", "amazon_portfolio_id", name="portfolio"),
    )

    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_profiles.id"), index=True)
    amazon_portfolio_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(255), server_default=text("''"))
    state: Mapped[str] = mapped_column(String(20), default="enabled", server_default=text("'enabled'"))
    # Money Amazon validates at 2dp, so it is stored at 2dp and sent back byte-identical on rollback.
    budget_amount: Mapped[float | None] = mapped_column(Numeric(12, 2))
    budget_currency: Mapped[str | None] = mapped_column(String(3))
    budget_policy: Mapped[str | None] = mapped_column(String(20))
    budget_start_date: Mapped[datetime | None] = mapped_column(Date)
    budget_end_date: Mapped[datetime | None] = mapped_column(Date)
    in_budget: Mapped[bool | None] = mapped_column(Boolean)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PpcCampaign(TimestampMixin, Base):
    __tablename__ = "ppc_campaigns"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("state", PPC_ENTITY_STATES), name="state"),
        UniqueConstraint("profile_id", "ad_product", "amazon_campaign_id", name="campaign"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_profiles.id"), index=True)
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_portfolios.id"), index=True)
    amazon_campaign_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(255), server_default=text("''"))
    campaign_type: Mapped[str] = mapped_column(String(30), default="sponsoredProducts",
                                               server_default=text("'sponsoredProducts'"))
    targeting_type: Mapped[str | None] = mapped_column(String(20))       # manual | auto
    state: Mapped[str] = mapped_column(String(20), default="enabled", server_default=text("'enabled'"))
    daily_budget: Mapped[float | None] = mapped_column(Numeric(12, 2))
    budget_type: Mapped[str | None] = mapped_column(String(20))
    bidding_strategy: Mapped[str | None] = mapped_column(String(40))
    # Amazon's dynamicBidding.placementBidding, kept verbatim so a rollback can send it back unchanged.
    placement_adjustments: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    brand_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("brands.id"), index=True)
    start_date: Mapped[datetime | None] = mapped_column(Date)
    end_date: Mapped[datetime | None] = mapped_column(Date)
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # Amazon's own verdict on whether this is actually running: a campaign can be ENABLED and still
    # not serve (out of budget, payment failed, ineligible). Without it there is no way to raise the
    # `ad_serving_problem` alert, and "enabled" would be read as "advertising".
    serving_status: Mapped[str | None] = mapped_column(String(60))
    serving_status_details: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    created_at_amazon: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PpcAdGroup(TimestampMixin, Base):
    __tablename__ = "ppc_ad_groups"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("state", PPC_ENTITY_STATES), name="state"),
        UniqueConstraint("profile_id", "ad_product", "amazon_ad_group_id", name="ad_group"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_profiles.id"), index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_campaigns.id"), index=True)
    amazon_ad_group_id: Mapped[str] = mapped_column(String(40))
    amazon_campaign_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(255), server_default=text("''"))
    state: Mapped[str] = mapped_column(String(20), default="enabled", server_default=text("'enabled'"))
    default_bid: Mapped[float | None] = mapped_column(Numeric(12, 2))
    serving_status: Mapped[str | None] = mapped_column(String(60))
    serving_status_details: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PpcProductAd(TimestampMixin, Base):
    __tablename__ = "ppc_product_ads"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("state", PPC_ENTITY_STATES), name="state"),
        UniqueConstraint("profile_id", "ad_product", "amazon_ad_id", name="product_ad"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_profiles.id"), index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_campaigns.id"), index=True)
    ad_group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_ad_groups.id"), index=True)
    amazon_ad_id: Mapped[str] = mapped_column(String(40))
    asin: Mapped[str | None] = mapped_column(String(20), index=True)
    sku: Mapped[str | None] = mapped_column(String(80), index=True)
    # The link to Phase 3, which is how stock, suppression and Buy Box reach the analysers.
    listing_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("amazon_listings.id"), index=True)
    state: Mapped[str] = mapped_column(String(20), default="enabled", server_default=text("'enabled'"))
    serving_status: Mapped[str | None] = mapped_column(String(60))
    serving_status_details: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PpcTarget(TimestampMixin, Base):
    """Keywords and product/category/auto targets in one table.

    Amazon serves keywords and targets from different endpoints with independent id namespaces, so
    the unique key is (profile, kind, id) — keying on target_type would let one targetId land three
    times under three classifications.
    """

    __tablename__ = "ppc_targets"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("state", PPC_ENTITY_STATES), name="state"),
        CheckConstraint(_in("target_kind", PPC_TARGET_KINDS), name="target_kind"),
        CheckConstraint(_in("target_type", PPC_TARGET_TYPES), name="target_type"),
        CheckConstraint(_in("match_type", PPC_MATCH_TYPES), name="match_type"),
        CheckConstraint("(target_kind = 'keyword') = (target_type = 'keyword')", name="kind_matches_type"),
        UniqueConstraint("profile_id", "ad_product", "target_kind", "amazon_target_id",
                         name="target"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_profiles.id"), index=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_campaigns.id"), index=True)
    ad_group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_ad_groups.id"), index=True)
    amazon_target_id: Mapped[str] = mapped_column(String(40))
    amazon_campaign_id: Mapped[str] = mapped_column(String(40))
    amazon_ad_group_id: Mapped[str] = mapped_column(String(40))
    target_kind: Mapped[str] = mapped_column(String(10))
    target_type: Mapped[str] = mapped_column(String(20))
    expression: Mapped[str] = mapped_column(Text, server_default=text("''"))
    match_type: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20), default="enabled", server_default=text("'enabled'"))
    bid: Mapped[float | None] = mapped_column(Numeric(12, 2))
    bid_inherited: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    suggested_bid: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at_amazon: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    harvested_from_search_term: Mapped[str | None] = mapped_column(Text)
    serving_status: Mapped[str | None] = mapped_column(String(60))
    serving_status_details: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PpcNegative(TimestampMixin, Base):
    __tablename__ = "ppc_negatives"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("state", PPC_ENTITY_STATES), name="state"),
        CheckConstraint(_in("match_type", PPC_MATCH_TYPES), name="match_type"),
        CheckConstraint("(campaign_id IS NOT NULL) OR (ad_group_id IS NOT NULL)", name="has_a_level"),
        UniqueConstraint("profile_id", "ad_product", "target_kind", "amazon_negative_id",
                         name="negative"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_profiles.id"), index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_campaigns.id"), index=True)
    ad_group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_ad_groups.id"), index=True)
    amazon_negative_id: Mapped[str] = mapped_column(String(40))
    target_kind: Mapped[str] = mapped_column(String(10), default="keyword",
                                             server_default=text("'keyword'"))
    expression: Mapped[str] = mapped_column(Text, server_default=text("''"))
    match_type: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20), default="enabled", server_default=text("'enabled'"))
    added_by_recommendation_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PpcMetricsDaily(TimestampMixin, Base):
    """One row per entity per day. Derived metrics (ACOS, CPC, CVR) are computed, never stored.

    `amazon_entity_id` holds Amazon's own id so the experiment and outcome joins line up with the
    structure tables; `is_provisional` marks the trailing days that still restate, and nothing the
    agent concludes may be built on a provisional row (brief 4.4).
    """

    __tablename__ = "ppc_metrics_daily"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("entity_type", PPC_ENTITY_KINDS), name="entity_type"),
        UniqueConstraint("amazon_profile_id", "ad_product", "entity_type", "amazon_entity_id",
                         "date", name="entity_day"),
        Index("ix_ppc_metrics_entity_date", "entity_type", "amazon_entity_id", "date"),
        Index("ix_ppc_metrics_date_final", "date", "is_provisional"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    amazon_profile_id: Mapped[str] = mapped_column(String(20), index=True)
    marketplace_id: Mapped[str] = mapped_column(String(20))
    entity_type: Mapped[str] = mapped_column(String(20))
    amazon_entity_id: Mapped[str] = mapped_column(String(40))
    amazon_campaign_id: Mapped[str | None] = mapped_column(String(40))
    amazon_ad_group_id: Mapped[str | None] = mapped_column(String(40))
    date: Mapped[datetime] = mapped_column(Date)
    impressions: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    clicks: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    spend: Mapped[float] = mapped_column(Numeric(12, 2), default=0, server_default=text("0"))
    sales: Mapped[float] = mapped_column(Numeric(12, 2), default=0, server_default=text("0"))
    orders: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    units: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    same_sku_sales: Mapped[float | None] = mapped_column(Numeric(12, 2))
    other_sku_sales: Mapped[float | None] = mapped_column(Numeric(12, 2))
    # The "under-exposed" analyser needs headroom, not just efficiency.
    impression_share: Mapped[float | None] = mapped_column(Numeric(6, 2))
    top_of_search_impression_share: Mapped[float | None] = mapped_column(Numeric(6, 2))
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'USD'"))
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # Stored so a historical number can be reproduced exactly (brief 4.4).
    report_id: Mapped[str | None] = mapped_column(String(80))
    report_start_date: Mapped[datetime | None] = mapped_column(Date)
    report_end_date: Mapped[datetime | None] = mapped_column(Date)
    attribution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PpcMetricsRestatement(TimestampMixin, Base):
    """What changed when a re-pull overwrote a day. Acceptance 1 requires restatements to be logged."""

    __tablename__ = "ppc_metrics_restatements"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        Index("ix_ppc_restatement_entity_date", "entity_type", "amazon_entity_id", "date"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    amazon_profile_id: Mapped[str] = mapped_column(String(20), index=True)
    entity_type: Mapped[str] = mapped_column(String(20))
    # Wide enough for a search term, not just an id: a restated search-term row is identified by its
    # ad group and the words the shopper typed.
    amazon_entity_id: Mapped[str] = mapped_column(String(300))
    date: Mapped[datetime] = mapped_column(Date)
    old_spend: Mapped[float | None] = mapped_column(Numeric(12, 2))
    old_sales: Mapped[float | None] = mapped_column(Numeric(12, 2))
    old_orders: Mapped[int | None] = mapped_column(Integer)
    old_clicks: Mapped[int | None] = mapped_column(Integer)
    new_spend: Mapped[float | None] = mapped_column(Numeric(12, 2))
    new_sales: Mapped[float | None] = mapped_column(Numeric(12, 2))
    new_orders: Mapped[int | None] = mapped_column(Integer)
    new_clicks: Mapped[int | None] = mapped_column(Integer)
    old_report_id: Mapped[str | None] = mapped_column(String(80))
    new_report_id: Mapped[str | None] = mapped_column(String(80))
    restated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PpcSearchTerm(TimestampMixin, Base):
    """What shoppers actually typed, per day. The harvest decision lives in its own table."""

    __tablename__ = "ppc_search_terms"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        UniqueConstraint("amazon_profile_id", "ad_product", "amazon_ad_group_id", "amazon_target_id",
                         "search_term", "date", name="search_term_day"),
        Index("ix_ppc_search_terms_term_date", "search_term", "date"),
        Index("ix_ppc_search_terms_campaign_date", "amazon_campaign_id", "date"),
        # The daily analysis asks for "this profile, this date range, finalised only" and nothing
        # else, and no index above has date in a leading position for that shape.
        Index("ix_ppc_search_terms_profile_date", "amazon_profile_id", "date", "is_provisional"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    amazon_profile_id: Mapped[str] = mapped_column(String(20), index=True)
    marketplace_id: Mapped[str] = mapped_column(String(20))
    amazon_campaign_id: Mapped[str] = mapped_column(String(40))
    amazon_ad_group_id: Mapped[str] = mapped_column(String(40))
    amazon_target_id: Mapped[str | None] = mapped_column(String(40))
    search_term: Mapped[str] = mapped_column(Text)
    match_type: Mapped[str | None] = mapped_column(String(20))
    date: Mapped[datetime] = mapped_column(Date)
    impressions: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    clicks: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    spend: Mapped[float] = mapped_column(Numeric(12, 2), default=0, server_default=text("0"))
    sales: Mapped[float] = mapped_column(Numeric(12, 2), default=0, server_default=text("0"))
    orders: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    units: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    search_term_impression_share: Mapped[float | None] = mapped_column(Numeric(6, 2))
    search_term_impression_rank: Mapped[float | None] = mapped_column(Numeric(6, 2))
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'USD'"))
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    report_id: Mapped[str | None] = mapped_column(String(80))
    pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PpcSearchTermDecision(TimestampMixin, Base):
    """One row per term per ad group: harvested, negated, watching, ignored.

    The decision is a fact about the term, not about a day of it — keeping it on the daily rows
    meant a harvested term stayed "new" on its other thirteen rows and was re-proposed every morning.
    """

    __tablename__ = "ppc_search_term_decisions"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("harvest_status", PPC_HARVEST_STATUSES), name="harvest_status"),
        UniqueConstraint("amazon_profile_id", "ad_product", "amazon_ad_group_id", "search_term",
                         name="term_decision"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    amazon_profile_id: Mapped[str] = mapped_column(String(20), index=True)
    amazon_campaign_id: Mapped[str | None] = mapped_column(String(40))
    amazon_ad_group_id: Mapped[str] = mapped_column(String(40))
    search_term: Mapped[str] = mapped_column(Text)
    harvest_status: Mapped[str] = mapped_column(String(20), default="new", server_default=text("'new'"))
    harvest_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    harvested_target_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_targets.id"))
    negative_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_negatives.id"))
    decided_by_recommendation_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    note: Mapped[str | None] = mapped_column(Text)


class PpcPlacement(TimestampMixin, Base):
    __tablename__ = "ppc_placements"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        UniqueConstraint("amazon_profile_id", "ad_product", "amazon_campaign_id", "placement", "date",
                         name="placement_day"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    amazon_profile_id: Mapped[str] = mapped_column(String(20), index=True)
    marketplace_id: Mapped[str] = mapped_column(String(20))
    amazon_campaign_id: Mapped[str] = mapped_column(String(40), index=True)
    placement: Mapped[str] = mapped_column(String(40))      # top of search | rest of search | product pages
    date: Mapped[datetime] = mapped_column(Date)
    impressions: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    clicks: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    spend: Mapped[float] = mapped_column(Numeric(12, 2), default=0, server_default=text("0"))
    sales: Mapped[float] = mapped_column(Numeric(12, 2), default=0, server_default=text("0"))
    orders: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    units: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    bid_adjustment_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'USD'"))
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    report_id: Mapped[str | None] = mapped_column(String(80))
    pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PpcPurchasedProduct(TimestampMixin, Base):
    """spPurchasedProduct: what an ad actually sold, which is often not the advertised ASIN."""

    __tablename__ = "ppc_purchased_products"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        UniqueConstraint("amazon_profile_id", "ad_product", "amazon_campaign_id", "advertised_asin",
                         "purchased_asin", "date", name="purchased_day"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    amazon_profile_id: Mapped[str] = mapped_column(String(20), index=True)
    marketplace_id: Mapped[str] = mapped_column(String(20))
    amazon_campaign_id: Mapped[str] = mapped_column(String(40), index=True)
    amazon_ad_group_id: Mapped[str | None] = mapped_column(String(40))
    advertised_asin: Mapped[str | None] = mapped_column(String(20))
    advertised_sku: Mapped[str | None] = mapped_column(String(80))
    purchased_asin: Mapped[str] = mapped_column(String(20))
    date: Mapped[datetime] = mapped_column(Date)
    sales: Mapped[float] = mapped_column(Numeric(12, 2), default=0, server_default=text("0"))
    orders: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    units: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'USD'"))
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    report_id: Mapped[str | None] = mapped_column(String(80))
    pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PpcHourlyMetric(TimestampMixin, Base):
    """Marketing Stream hourly metrics (M11), for dayparting and same-day anomaly detection."""

    __tablename__ = "ppc_hourly_metrics"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        UniqueConstraint("amazon_profile_id", "ad_product", "amazon_campaign_id", "date", "hour",
                         name="campaign_hour"),
        Index("ix_ppc_hourly_date_hour", "date", "hour"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    amazon_profile_id: Mapped[str] = mapped_column(String(20), index=True)
    amazon_campaign_id: Mapped[str] = mapped_column(String(40), index=True)
    date: Mapped[datetime] = mapped_column(Date)
    hour: Mapped[int] = mapped_column(Integer)
    weekday: Mapped[int | None] = mapped_column(Integer)
    impressions: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    clicks: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    spend: Mapped[float] = mapped_column(Numeric(12, 2), default=0, server_default=text("0"))
    sales: Mapped[float] = mapped_column(Numeric(12, 2), default=0, server_default=text("0"))
    orders: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    budget_consumed_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'USD'"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PpcApiCall(TimestampMixin, Base):
    __tablename__ = "ppc_api_calls"

    operation: Mapped[str] = mapped_column(String(80), index=True)
    profile_id: Mapped[str | None] = mapped_column(String(20))
    http_status: Mapped[int | None] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String(80))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(200))
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                index=True)


class PpcProductEconomics(TimestampMixin, Base):
    """What an ASIN can afford to spend. Break-even and target ACOS, with history.

    Rows are never updated in place: a price, cost or fee change closes the current row with
    `valid_to` and opens a new one, so an analysis from six weeks ago still reproduces.
    """

    __tablename__ = "ppc_product_economics"
    __table_args__ = (
        CheckConstraint(_in("target_source", ("owner", "derived", "learned")), name="target_source"),
        Index("ix_ppc_economics_asin_valid", "asin", "marketplace_id", "valid_to"),
    )

    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_profiles.id"), index=True)
    marketplace_id: Mapped[str] = mapped_column(String(20))
    asin: Mapped[str] = mapped_column(String(20), index=True)
    sku: Mapped[str | None] = mapped_column(String(80))
    listing_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("amazon_listings.id"), index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id"), index=True)
    sale_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    unit_cost: Mapped[float | None] = mapped_column(Numeric(12, 2))
    referral_fee: Mapped[float | None] = mapped_column(Numeric(12, 2))
    fba_fee: Mapped[float | None] = mapped_column(Numeric(12, 2))
    other_costs: Mapped[float | None] = mapped_column(Numeric(12, 2))
    contribution_margin: Mapped[float | None] = mapped_column(Numeric(12, 2))
    break_even_acos: Mapped[float | None] = mapped_column(Numeric(6, 4))
    target_acos: Mapped[float | None] = mapped_column(Numeric(6, 4))
    target_source: Mapped[str] = mapped_column(String(20), default="derived",
                                               server_default=text("'derived'"))
    # Missing cost is never guessed: it is asked for once and flagged on every recommendation.
    cost_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    cost_asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'USD'"))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_note: Mapped[str | None] = mapped_column(Text)


class PpcRule(TimestampMixin, Base):
    """The owner's hard constraints. Learning can never override these (brief 12.2)."""

    __tablename__ = "ppc_rules"
    __table_args__ = (
        CheckConstraint("ad_product IS NULL OR " + _in("ad_product", PPC_AD_PRODUCTS),
                        name="ad_product"),
        CheckConstraint(_in("scope", PPC_RULE_SCOPES), name="scope"),
    )

    # Null means every ad product, which is a meaningful answer here.
    ad_product: Mapped[str | None] = mapped_column(String(2))
    scope: Mapped[str] = mapped_column(String(20), default="account", server_default=text("'account'"))
    scope_ref: Mapped[str | None] = mapped_column(String(80), index=True)
    target_acos: Mapped[float | None] = mapped_column(Numeric(6, 4))
    max_bid: Mapped[float | None] = mapped_column(Numeric(12, 2))
    min_bid: Mapped[float | None] = mapped_column(Numeric(12, 2))
    max_daily_budget: Mapped[float | None] = mapped_column(Numeric(12, 2))
    max_bid_change_pct_per_change: Mapped[float | None] = mapped_column(Numeric(6, 2))
    max_changes_per_entity_per_week: Mapped[int | None] = mapped_column(Integer)
    max_daily_account_spend: Mapped[float | None] = mapped_column(Numeric(12, 2))
    protected_entities: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    # "Never pause branded terms" is a change-type ban, not a number, and Section 9.4 uses it as the
    # worked example of the owner's judgement becoming permanent behaviour.
    blocked_change_types: Mapped[list[Any]] = mapped_column(default=list,
                                                            server_default=text("'[]'::jsonb"))
    notes: Mapped[str | None] = mapped_column(Text)
    set_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class PpcCalendarEvent(TimestampMixin, Base):
    """Prime Day, Black Friday, Q4 peak. Section 9.6 forbids conclusions drawn across these."""

    __tablename__ = "ppc_calendar_events"
    __table_args__ = (
        CheckConstraint(_in("kind", PPC_CALENDAR_KINDS), name="kind"),
    )

    kind: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(120))
    marketplace_id: Mapped[str | None] = mapped_column(String(20))
    start_date: Mapped[datetime] = mapped_column(Date, index=True)
    end_date: Mapped[datetime] = mapped_column(Date, index=True)
    note: Mapped[str | None] = mapped_column(Text)


class PpcAnalysisRun(TimestampMixin, Base):
    """One daily analysis cycle, and the batch of recommendations it produced."""

    __tablename__ = "ppc_analysis_runs"
    __table_args__ = (
        CheckConstraint("ad_product IS NULL OR " + _in("ad_product", PPC_AD_PRODUCTS),
                        name="ad_product"),
    )

    # Null where a run covered everything rather than one ad product.
    ad_product: Mapped[str | None] = mapped_column(String(2))
    ref: Mapped[str] = mapped_column(
        String(20), unique=True, server_default=text("'B-' || nextval('ppc_batch_ref_seq')"))
    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_profiles.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_window_start: Mapped[datetime | None] = mapped_column(Date)
    data_window_end: Mapped[datetime | None] = mapped_column(Date)
    analysers_run: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    playbook_rules_applied: Mapped[list[Any]] = mapped_column(default=list,
                                                              server_default=text("'[]'::jsonb"))
    suppressed_counts: Mapped[dict[str, Any]] = mapped_column(default=dict,
                                                              server_default=text("'{}'::jsonb"))
    suppressed_by_rule: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    candidates_considered: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    recommendations_made: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    llm_call_ids: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'::jsonb"))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    digest_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id"))
    spend_share_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
    requires_pin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    error: Mapped[str | None] = mapped_column(Text)


class PpcRecommendation(TimestampMixin, Base):
    """A proposed change, with the evidence behind it. Nothing reaches Amazon without approval."""

    __tablename__ = "ppc_recommendations"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("change_type", PPC_CHANGE_TYPES), name="change_type"),
        CheckConstraint(_in("entity_type", PPC_ENTITY_KINDS), name="entity_type"),
        CheckConstraint(_in("status", PPC_RECOMMENDATION_STATUSES), name="status"),
        CheckConstraint(_in("risk", PPC_RISK_LEVELS), name="risk"),
        Index("ix_ppc_recs_status_created", "status", "created_at"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    ref: Mapped[str] = mapped_column(
        String(20), unique=True, server_default=text("'P-' || nextval('ppc_recommendation_ref_seq')"))
    # The analysis run is the batch: one name, one owner, one sequence draw.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_analysis_runs.id"), index=True)
    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_profiles.id"), index=True)
    change_type: Mapped[str] = mapped_column(String(30))
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(index=True)     # our row
    amazon_entity_id: Mapped[str] = mapped_column(String(40), server_default=text("''"))   # Amazon's
    entity_label: Mapped[str | None] = mapped_column(Text)
    asin: Mapped[str | None] = mapped_column(String(20), index=True)
    current_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    proposed_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text, server_default=text("''"))
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    analyser: Mapped[str | None] = mapped_column(String(40))
    playbook_rule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_playbook.id"), index=True)
    predicted_effect: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    risk: Mapped[str] = mapped_column(String(10), default="low", server_default=text("'low'"))
    spend_at_risk: Mapped[float | None] = mapped_column(Numeric(12, 2))
    target_acos_unverified: Mapped[bool] = mapped_column(Boolean, default=False,
                                                         server_default=text("false"))
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_experiments.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="proposed", server_default=text("'proposed'"))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id"))
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PpcAction(TimestampMixin, Base):
    """Every executed change, with what it was before and how to put it back."""

    __tablename__ = "ppc_actions"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("change_type", PPC_CHANGE_TYPES), name="change_type"),
        CheckConstraint(_in("entity_type", PPC_ENTITY_KINDS), name="entity_type"),
        Index("ix_ppc_actions_executed", "executed_at"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_recommendations.id"),
                                                                index=True)
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_experiments.id"), index=True)
    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_profiles.id"), index=True)
    change_type: Mapped[str] = mapped_column(String(30))
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    amazon_entity_id: Mapped[str] = mapped_column(String(40), server_default=text("''"), index=True)
    asin: Mapped[str | None] = mapped_column(String(20), index=True)
    before_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Stored before the write, so an undo never depends on reading Amazon afterwards.
    rollback_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    verified_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    amazon_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    executed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_reason: Mapped[str | None] = mapped_column(Text)
    failed_reason: Mapped[str | None] = mapped_column(Text)


class PpcActionOutcome(TimestampMixin, Base):
    """Did the change help? Measured at 7, 14 and 28 days, against a baseline and the context."""

    __tablename__ = "ppc_action_outcomes"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("verdict", PPC_VERDICTS), name="verdict"),
        UniqueConstraint("action_id", "window_days", name="action_window"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    action_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_actions.id"), index=True)
    window_days: Mapped[int] = mapped_column(Integer)
    pre_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    post_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    baseline_adjusted_effect: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Stockouts, price changes, suppression, Buy Box loss, calendar events — why a number moved.
    external_factors: Mapped[dict[str, Any]] = mapped_column(default=dict,
                                                             server_default=text("'{}'::jsonb"))
    contaminated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    verdict: Mapped[str] = mapped_column(String(20), default="inconclusive",
                                         server_default=text("'inconclusive'"))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PpcAlert(TimestampMixin, Base):
    """The eight anomaly types from Section 11. One open alert per entity and type."""

    __tablename__ = "ppc_alerts"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("type", PPC_ALERT_TYPES), name="type"),
        CheckConstraint(_in("severity", RISK_LEVELS), name="severity"),
        CheckConstraint(_in("status", ("open", "resolved", "muted")), name="status"),
        Index("ix_ppc_alerts_open", "type", "amazon_entity_id", "status"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_profiles.id"), index=True)
    type: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10), default="medium", server_default=text("'medium'"))
    entity_type: Mapped[str | None] = mapped_column(String(20))
    amazon_entity_id: Mapped[str | None] = mapped_column(String(40))
    entity_label: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), default="open", server_default=text("'open'"))
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PpcExperiment(TimestampMixin, Base):
    """A controlled test with a hypothesis, a control group, guardrails and an honest verdict."""

    __tablename__ = "ppc_experiments"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("origin", PPC_EXPERIMENT_ORIGINS), name="origin"),
        CheckConstraint(_in("status", PPC_EXPERIMENT_STATUSES), name="status"),
        CheckConstraint(_in("change_type", PPC_CHANGE_TYPES), name="change_type"),
        CheckConstraint("conclusion IS NULL OR " + _in("conclusion", PPC_EXPERIMENT_CONCLUSIONS),
                        name="conclusion"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    ref: Mapped[str] = mapped_column(
        String(20), unique=True, server_default=text("'X-' || nextval('ppc_experiment_ref_seq')"))
    profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_profiles.id"), index=True)
    hypothesis: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(30), default="agent_idea",
                                        server_default=text("'agent_idea'"))
    research_note_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_research_notes.id"))
    change_type: Mapped[str] = mapped_column(String(30))
    unit_of_assignment: Mapped[str] = mapped_column(String(20), default="target",
                                                    server_default=text("'target'"))
    design: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    guardrails: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    primary_metric: Mapped[str | None] = mapped_column(String(60))
    minimum_clicks_per_arm: Mapped[int | None] = mapped_column(Integer)
    planned_days: Mapped[int | None] = mapped_column(Integer)
    spend_at_risk: Mapped[float | None] = mapped_column(Numeric(12, 2))
    has_true_control: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(20), default="designed", server_default=text("'designed'"))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    achieved_power: Mapped[float | None] = mapped_column(Numeric(4, 3))
    conclusion: Mapped[str | None] = mapped_column(String(20))
    learning_note: Mapped[str | None] = mapped_column(Text)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PpcExperimentUnit(TimestampMixin, Base):
    """One entity in one arm. `amazon_entity_id` is the join key to the metrics tables."""

    __tablename__ = "ppc_experiment_units"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("arm", PPC_ARMS), name="arm"),
        CheckConstraint(_in("entity_type", PPC_ENTITY_KINDS), name="entity_type"),
        UniqueConstraint("experiment_id", "entity_type", "amazon_entity_id", name="unit"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ppc_experiments.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    amazon_entity_id: Mapped[str] = mapped_column(String(40), index=True)
    arm: Mapped[str] = mapped_column(String(20))
    stratum: Mapped[str | None] = mapped_column(String(40))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    pre_period_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    post_period_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    excluded_reason: Mapped[str | None] = mapped_column(Text)
    excluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PpcPlaybook(TimestampMixin, Base):
    """A learned rule: human-readable statement, machine-readable conditions, and its evidence.

    A rule may only be proposed from a completed experiment, from at least thirty measured actions
    of one change type, or from a pattern in the owner's rejections — never from intuition or an article.
    The CHECK below is written NULL-safe so the thirty-action floor actually holds.
    """

    __tablename__ = "ppc_playbook"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("status", PPC_PLAYBOOK_STATUSES), name="status"),
        CheckConstraint(_in("created_from", PPC_PLAYBOOK_ORIGINS), name="created_from"),
        CheckConstraint(_in("action_kind", PPC_PLAYBOOK_ACTION_KINDS), name="action_kind"),
        CheckConstraint(
            "(created_from = 'experiment' AND created_from_experiment_id IS NOT NULL) "
            "OR (created_from = 'outcome_analysis' AND COALESCE(evidence_action_count, 0) >= 30) "
            "OR (created_from = 'owner_rejection_pattern' AND COALESCE(evidence_rejection_count, 0) > 0)",
            name="evidence_required"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    ref: Mapped[str] = mapped_column(
        String(20), unique=True, server_default=text("'R-' || nextval('ppc_playbook_ref_seq')"))
    title: Mapped[str] = mapped_column(String(200))
    statement: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(20), default="account", server_default=text("'account'"))
    scope_ref: Mapped[str | None] = mapped_column(String(80))
    conditions: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    # A rule may add a candidate, modify one, or suppress one — Section 7.1 step 4 needs all three.
    action_kind: Mapped[str] = mapped_column(String(10), default="add", server_default=text("'add'"))
    action_template: Mapped[dict[str, Any]] = mapped_column(default=dict,
                                                            server_default=text("'{}'::jsonb"))
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    created_from: Mapped[str] = mapped_column(String(30))
    created_from_experiment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ppc_experiments.id"))
    evidence_action_count: Mapped[int | None] = mapped_column(Integer)
    evidence_rejection_count: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    status: Mapped[str] = mapped_column(String(20), default="proposed", server_default=text("'proposed'"))
    approved_by_owner_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id"))
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    times_applied: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    success_rate: Mapped[float | None] = mapped_column(Numeric(4, 3))
    probation_reason: Mapped[str | None] = mapped_column(Text)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(index=True)


class PpcPrediction(TimestampMixin, Base):
    """What the agent said would happen, and what did. Calibration, scored by change type."""

    __tablename__ = "ppc_predictions"
    __table_args__ = (
        CheckConstraint(_in("ad_product", PPC_AD_PRODUCTS), name="ad_product"),
        CheckConstraint(_in("subject_type", ("recommendation", "experiment")), name="subject_type"),
        Index("ix_ppc_predictions_change_type", "change_type", "evaluated_at"),
    )

    ad_product: Mapped[str] = mapped_column(String(2), index=True)
    subject_type: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[uuid.UUID] = mapped_column(index=True)
    change_type: Mapped[str | None] = mapped_column(String(30))
    metric: Mapped[str | None] = mapped_column(String(60))
    predicted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    actual: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[float | None] = mapped_column(Numeric(12, 4))
    absolute_error: Mapped[float | None] = mapped_column(Numeric(12, 4))
    within_interval: Mapped[bool | None] = mapped_column(Boolean)
    stated_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    model_note: Mapped[str | None] = mapped_column(Text)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PpcResearchNote(TimestampMixin, Base):
    """Something read elsewhere. It is a claim until this account's own data says otherwise."""

    __tablename__ = "ppc_research_notes"
    __table_args__ = (
        CheckConstraint("ad_product IS NULL OR " + _in("ad_product", PPC_AD_PRODUCTS),
                        name="ad_product"),
        CheckConstraint(_in("source_type", PPC_RESEARCH_SOURCES), name="source_type"),
        CheckConstraint(_in("status", PPC_RESEARCH_STATUSES), name="status"),
    )

    # Null means every ad product, which is a meaningful answer here.
    ad_product: Mapped[str | None] = mapped_column(String(2))
    source_url: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(30))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The agent's own one-sentence summary. Long verbatim extracts are deliberately not stored.
    claim: Mapped[str] = mapped_column(Text)
    applicability_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="untested",
                                        server_default=text("'untested'"))
    linked_experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ppc_experiments.id", use_alter=True, name="fk_ppc_research_note_experiment"))
    reported_to_owner_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
