"""The Supervisor's output contract (Section 8.1).

The LLM must return exactly this shape. Every field is validated here
before any code acts on it.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Annotated, Literal, Union
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import get_settings

AgentKey = Literal["supervisor", "email", "amazon", "ppc", "sourcing", "reporting"]
Priority = Literal["urgent", "high", "normal", "low"]
OwnerSettableStatus = Literal["queued", "in_progress", "waiting_owner", "blocked", "done", "failed"]
TaskStatus = Literal["new", "queued", "in_progress", "waiting_approval", "waiting_owner", "blocked",
                     "done", "cancelled", "failed"]
MemoryCategory = Literal["sop", "company_rule", "owner_preference", "procedure", "contact_note"]
DEFAULT_DUE_TIME = time(17, 0)


def parse_due(value: str | None) -> datetime | None:
    """ISO date or datetime -> aware datetime. Dates mean 5 PM Eastern; naive times are Eastern."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    tz = ZoneInfo(get_settings().timezone)
    text = value.strip()
    if len(text) == 10:
        d = datetime.fromisoformat(text).date()
        return datetime.combine(d, DEFAULT_DUE_TIME, tzinfo=tz)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _DueMixin(_Strict):
    due_at: str | None = Field(None, description="ISO 8601 date or datetime in Eastern Time, or null")

    @field_validator("due_at")
    @classmethod
    def _valid_due(cls, v):
        parse_due(v)  # raises ValueError if malformed
        return v


class SubtaskSpec(_Strict):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None


class CreateTaskParams(_DueMixin):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    priority: Priority = "normal"
    agent_key: AgentKey = "supervisor"
    subtasks: list[SubtaskSpec] | None = None
    attach_current_file: bool | None = Field(
        None, description="true to link the file sent with this message to the new task")
    supervisor_can_complete: bool | None = Field(
        None, description="true only if the Supervisor can finish this now by reasoning/writing alone")


class UpdateTaskParams(_DueMixin):
    task_ref: str
    status: OwnerSettableStatus | None = None
    priority: Priority | None = None
    note: str | None = None
    attach_current_file: bool | None = None


class CancelTaskParams(_Strict):
    task_ref: str
    reason: str = Field(min_length=1)


class GetStatusParams(_Strict):
    task_ref: str | None = None
    scope: str | None = Field(None, description="all_open | today | agent:<key>")

    @field_validator("scope")
    @classmethod
    def _scope(cls, v):
        if v is None or v in ("all_open", "today"):
            return v
        if v.startswith("agent:") and v.split(":", 1)[1] in AgentKey.__args__:
            return v
        raise ValueError("scope must be all_open, today, or agent:<key>")


class ListTasksParams(_Strict):
    statuses: list[TaskStatus] | None = None
    agent_key: AgentKey | None = None
    priority: Priority | None = None
    due: Literal["today", "overdue", "this_week"] | None = None


class RememberParams(_Strict):
    category: MemoryCategory
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    applies_to_agent_key: AgentKey | None = None


class RequestReportParams(_Strict):
    report_type: Literal["daily_summary"] = "daily_summary"


class EmailDraftRequestParams(_Strict):
    thread_ref: str | None = Field(None, description="E-n if known")
    sender: str | None = Field(None, description="name, address or company to find the thread")
    subject_contains: str | None = None
    instructions: str = Field(min_length=1, description="what the owner wants the reply to say, in his words")


class EmailEditDraftParams(_Strict):
    draft_ref: str
    changes: str = Field(min_length=1)


EmailCategory = Literal["amazon", "supplier", "customer_b2b", "customer_b2c", "logistics", "finance_invoice",
                        "legal", "marketing_spam", "personal", "other"]


class EmailSearchParams(_Strict):
    sender: str | None = None
    subject_contains: str | None = None
    since_days: int | None = Field(None, ge=1, le=90)
    category: EmailCategory | None = None
    needs_reply_only: bool | None = None


class EmailSetFollowupParams(_Strict):
    thread_ref: str
    days: int | None = Field(None, ge=1, le=60, description="business days from now; null with stop=true")
    stop: bool | None = Field(None, description="true to stop tracking follow-ups for this thread")


class EmailAddAlertRuleParams(_Strict):
    rule_type: Literal["sender", "domain", "keyword", "custom_description"]
    value: str = Field(min_length=1, max_length=500)
    alert_level: Literal["instant", "daily_summary_only", "ignore"]
    bypass_quiet_hours: bool | None = None


class AmazonEditListingParams(_Strict):
    listing_ref: str | None = Field(None, description="L-n if the owner gave one")
    sku: str | None = None
    asin: str | None = None
    changes: dict[str, str] = Field(description="attribute name -> new value, in the owner's words "
                                                "(e.g. {'item_name': 'New title'})", min_length=1)
    instructions: str = Field(default="", description="what the owner asked for, in his words")


class AmazonUpdatePriceParams(_Strict):
    listing_ref: str | None = None
    sku: str | None = None
    asin: str | None = None
    price: float = Field(gt=0, description="the new price the owner gave, never one you worked out")
    instructions: str = ""


class AmazonCreateListingParams(_Strict):
    sku: str = Field(min_length=1, max_length=80, description="the SKU the owner gave, or one he approved")
    marketplace: Literal["US", "CA"] = "US"
    details: str = Field(min_length=10, description="everything the owner said about the product, in his words")
    asin: str | None = Field(None, description="only when adding our offer to an ASIN that already exists")
    create_anyway: bool = Field(False, description="true only when the owner said to create it despite a duplicate")


class AmazonCreateVariationsParams(_Strict):
    parent_sku: str = Field(min_length=1, max_length=80)
    theme: str = Field(description="the variation theme the owner named, e.g. SIZE or COLOR")
    children: str = Field(description="the owner's list of children, verbatim: one per line, "
                                      "\"SKU | value | price\"")
    details: str = Field(default="", description="anything else the owner said about the family")
    marketplace: Literal["US", "CA"] = "US"


class AmazonModifyVariationsParams(_Strict):
    action: Literal["add_child", "remove_child"]
    parent_ref: str | None = Field(None, description="L-n of the parent, for add_child")
    child_ref: str | None = Field(None, description="L-n of the child, for remove_child")
    child: str | None = Field(None, description="for add_child: \"SKU | value | price\" as the owner wrote it")
    theme: str | None = None


class FbaCreateShipmentParams(_Strict):
    details: str = Field(min_length=5, description="the owner's shipment message verbatim: ASINs, quantities, "
                                                   "boxes, sizes, weights, dates")
    marketplace: Literal["US", "CA"] = "US"


class FbaProvideInfoParams(_Strict):
    answers: str = Field(min_length=1, description="the owner's answers to my shipment questions, verbatim")


class FbaSetCasePackParams(_Strict):
    sku: str | None = None
    listing_ref: str | None = None
    units_per_case: int = Field(gt=0)
    length_in: float = Field(gt=0)
    width_in: float = Field(gt=0)
    height_in: float = Field(gt=0)
    weight_lb: float = Field(gt=0)


class FbaSetShipFromParams(_Strict):
    phone: str | None = None
    email: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state_or_province: str | None = None
    postal_code: str | None = None


class FbaSetReadyDateParams(_Strict):
    request_ref: str = Field(description="the shipment ref, e.g. FB-5")
    date: str = Field(description="the day the owner named, in his words (\"Monday\", \"Sep 25\", \"2026-09-25\") — "
                                  "never a date he didn't give")


class AmazonCopyMarketplaceParams(_Strict):
    listing_ref: str | None = None
    sku: str | None = None
    asin: str | None = None
    brand: str | None = Field(None, description="for a bulk copy of one brand")
    target: Literal["CA", "US"] = "CA"
    price: float | None = Field(None, gt=0, description="the price the owner gave in the target currency; "
                                                        "never convert one yourself")
    prices: str | None = Field(None, description="for a bulk copy: the owner's \"SKU price\" lines, verbatim")


class AmazonDeleteListingParams(_Strict):
    listing_ref: str | None = None
    sku: str | None = None


class AmazonProvideInfoParams(_Strict):
    answers: str = Field(min_length=1, description="the owner's answers to the questions I asked, in his words")


class AmazonRevertParams(_Strict):
    listing_ref: str
    version: int = Field(ge=1)


class AmazonQueryParams(_Strict):
    question: Literal["listings", "stock", "changes"] = "listings"


class EmailAttachFileParams(_Strict):
    draft_ref: str
    remove: str | None = Field(None, description="name or number to remove; null attaches the file the owner sent")


class EmailNewDraftParams(_Strict):
    to: list[str] = Field(min_length=1, max_length=10, description="email addresses exactly as the owner gave them")
    cc: list[str] | None = Field(None, max_length=10)
    subject: str | None = Field(None, max_length=300, description="only if the owner gave one")
    instructions: str = Field(min_length=1, description="what the owner wants the email to say, in his words")


class EmailRemoveAlertRuleParams(_Strict):
    value: str = Field(min_length=1, max_length=500)


class EmailSetContactTypeParams(_Strict):
    address: str
    contact_type: Literal["amazon", "supplier", "customer_b2b", "customer_b2c", "logistics", "legal", "finance",
                          "other"] | None = None
    is_vip: bool | None = None


class EmailSummaryParams(_Strict):
    period: Literal["today", "24h", "week"] = "today"


class EmailUpdateThreadStatusParams(_Strict):
    thread_ref: str
    status: Literal["open", "handled", "ignored"]


class EmptyParams(_Strict):
    pass


class CreateTask(_Strict):
    type: Literal["create_task"]
    params: CreateTaskParams


class UpdateTask(_Strict):
    type: Literal["update_task"]
    params: UpdateTaskParams


class CancelTask(_Strict):
    type: Literal["cancel_task"]
    params: CancelTaskParams


class GetStatus(_Strict):
    type: Literal["get_status"]
    params: GetStatusParams


class ListTasks(_Strict):
    type: Literal["list_tasks"]
    params: ListTasksParams


class AnswerQuestion(_Strict):
    type: Literal["answer_question"]
    params: EmptyParams


class Remember(_Strict):
    type: Literal["remember"]
    params: RememberParams


class RequestClarification(_Strict):
    type: Literal["request_clarification"]
    params: EmptyParams


class RequestReport(_Strict):
    type: Literal["request_report"]
    params: RequestReportParams


class EmailDraftRequest(_Strict):
    type: Literal["email_draft_request"]
    params: EmailDraftRequestParams


class EmailEditDraft(_Strict):
    type: Literal["email_edit_draft"]
    params: EmailEditDraftParams


class EmailSearch(_Strict):
    type: Literal["email_search"]
    params: EmailSearchParams


class EmailSetFollowup(_Strict):
    type: Literal["email_set_followup"]
    params: EmailSetFollowupParams


class EmailAddAlertRule(_Strict):
    type: Literal["email_add_alert_rule"]
    params: EmailAddAlertRuleParams


class AmazonEditListing(_Strict):
    type: Literal["amazon_edit_listing"]
    params: AmazonEditListingParams


class AmazonUpdatePrice(_Strict):
    type: Literal["amazon_update_price"]
    params: AmazonUpdatePriceParams


class AmazonCreateListing(_Strict):
    type: Literal["amazon_create_listing"]
    params: AmazonCreateListingParams


class AmazonCreateVariations(_Strict):
    type: Literal["amazon_create_variations"]
    params: AmazonCreateVariationsParams


class AmazonModifyVariations(_Strict):
    type: Literal["amazon_modify_variations"]
    params: AmazonModifyVariationsParams


class FbaCreateShipment(_Strict):
    type: Literal["fba_create_shipment"]
    params: FbaCreateShipmentParams


class FbaProvideInfo(_Strict):
    type: Literal["fba_provide_info"]
    params: FbaProvideInfoParams


class FbaSetCasePack(_Strict):
    type: Literal["fba_set_case_pack"]
    params: FbaSetCasePackParams


class FbaSetShipFrom(_Strict):
    type: Literal["fba_set_ship_from"]
    params: FbaSetShipFromParams


class FbaSetReadyDate(_Strict):
    type: Literal["fba_set_ready_date"]
    params: FbaSetReadyDateParams


class AmazonCopyMarketplace(_Strict):
    type: Literal["amazon_copy_marketplace"]
    params: AmazonCopyMarketplaceParams


class AmazonDeleteListing(_Strict):
    type: Literal["amazon_delete_listing"]
    params: AmazonDeleteListingParams


class AmazonProvideInfo(_Strict):
    type: Literal["amazon_provide_info"]
    params: AmazonProvideInfoParams


class AmazonRevert(_Strict):
    type: Literal["amazon_revert"]
    params: AmazonRevertParams


class AmazonQuery(_Strict):
    type: Literal["amazon_query"]
    params: AmazonQueryParams


class EmailAttachFile(_Strict):
    type: Literal["email_attach_file"]
    params: EmailAttachFileParams


class EmailNewDraft(_Strict):
    type: Literal["email_new_draft"]
    params: EmailNewDraftParams


class EmailRemoveAlertRule(_Strict):
    type: Literal["email_remove_alert_rule"]
    params: EmailRemoveAlertRuleParams


class EmailListAlertRules(_Strict):
    type: Literal["email_list_alert_rules"]
    params: EmptyParams


class EmailSetContactType(_Strict):
    type: Literal["email_set_contact_type"]
    params: EmailSetContactTypeParams


class EmailSummary(_Strict):
    type: Literal["email_summary"]
    params: EmailSummaryParams


class EmailUpdateThreadStatus(_Strict):
    type: Literal["email_update_thread_status"]
    params: EmailUpdateThreadStatusParams


class NoOp(_Strict):
    type: Literal["no_op"]
    params: EmptyParams


Action = Annotated[
    Union[CreateTask, UpdateTask, CancelTask, GetStatus, ListTasks, AnswerQuestion, Remember,
          RequestClarification, RequestReport, EmailDraftRequest, EmailNewDraft, EmailAttachFile,
          AmazonEditListing, AmazonUpdatePrice, AmazonRevert, AmazonQuery,
          AmazonCreateListing, AmazonProvideInfo, AmazonCreateVariations, AmazonModifyVariations,
          AmazonDeleteListing, AmazonCopyMarketplace, FbaCreateShipment, FbaProvideInfo, FbaSetCasePack, FbaSetShipFrom, FbaSetReadyDate, EmailEditDraft, EmailSearch,
          EmailSetFollowup, EmailAddAlertRule, EmailRemoveAlertRule, EmailListAlertRules,
          EmailSetContactType, EmailSummary, EmailUpdateThreadStatus, NoOp],
    Field(discriminator="type"),
]


class SupervisorOutput(_Strict):
    actions: list[Action] = Field(min_length=1, max_length=20)
    reply_to_owner: str = Field(max_length=6000)


class TaskWorkOutput(_Strict):
    """What the Supervisor returns when it works on a task itself."""

    outcome: Literal["done", "needs_owner_input"]
    result_text: str = Field(description="The finished work (e.g. the draft), or the question for the owner")
    summary: str = Field(max_length=500, description="One-line summary of what was produced")
