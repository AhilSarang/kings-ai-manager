"""Executes the Supervisor's validated actions. Pure code, permission-checked.

Returns the lines the system appends to the Supervisor's reply, so facts
(task refs, statuses) always come from the database, never from the LLM.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.actions import schemas as S
from app.actions.registry import ActionNotPermitted, UnknownAction, check_permitted
from app.db.models import File, Message, Task, User
from app.jobs import queue
from app.services import agents as agents_svc
from app.services import audit, files, memory, summaries, tasks
from app.services.common import Actor, NotFound, ServiceError, utcnow
from app.services.formatting import STATUS_LABEL, fmt_dt

log = logging.getLogger(__name__)


@dataclass
class ExecutionContext:
    session: Session
    user: User
    message: Message
    llm_call_id: uuid.UUID | None = None
    agent_key: str = "supervisor"


# Actions whose own output is the whole answer: the agent has already said what it needs or did.
AUTHORITATIVE = ("amazon_", "fba_", "email_draft_request", "email_new_draft", "email_edit_draft")


@dataclass
class ExecutionResult:
    lines: list[str] = field(default_factory=list)
    created: list[Task] = field(default_factory=list)
    related_task_id: uuid.UUID | None = None
    errors: int = 0
    authoritative: bool = False


def execute(ctx: ExecutionContext, output: S.SupervisorOutput) -> ExecutionResult:
    result = ExecutionResult()
    agent = agents_svc.get_by_key(ctx.session, ctx.agent_key)
    owner_actor = Actor.user(ctx.user.id)
    for action in output.actions:
        try:
            check_permitted(action.type, agent.permissions)
            handler = _HANDLERS[action.type]
            handler(ctx, action.params, result, owner_actor)
            if action.type.startswith(AUTHORITATIVE):
                result.authoritative = True
            audit.record(ctx.session, Actor.agent(ctx.agent_key), "action_executed",
                         target_type="action", target_id=action.type,
                         details={"params": action.params.model_dump(mode="json")},
                         trigger_message_id=ctx.message.id, llm_call_id=ctx.llm_call_id)
        except (UnknownAction, ActionNotPermitted) as exc:
            result.errors += 1
            result.lines.append(f"⛔ {exc}")
            audit.record(ctx.session, Actor.agent(ctx.agent_key), "action_refused", target_type="action",
                         target_id=action.type, details={"reason": str(exc)},
                         trigger_message_id=ctx.message.id, llm_call_id=ctx.llm_call_id)
        except (NotFound, ServiceError) as exc:
            result.errors += 1
            result.lines.append(f"⚠️ {exc}")
    return result


def compose_reply(output: S.SupervisorOutput, result: ExecutionResult) -> str:
    lines = "\n".join(result.lines).strip()
    if result.errors:
        return lines or "⚠️ I couldn't do that."  # never let "doing it now" sit above a refusal
    if result.authoritative and lines:
        # The Amazon and email agents write their own answer, down to the questions they need
        # answering. A preamble from the model on top of it only contradicts it or invents status.
        return lines
    parts = [p for p in (output.reply_to_owner.strip(), lines) if p]
    return "\n\n".join(parts) or "👍"


# ---------------- handlers ----------------
def _current_file(ctx: ExecutionContext) -> File | None:
    return ctx.session.get(File, ctx.message.media_file_id) if ctx.message.media_file_id else None


def _attach(ctx: ExecutionContext, task: Task, result: ExecutionResult, actor: Actor) -> None:
    f = _current_file(ctx)
    if f is None:
        result.lines.append(f"⚠️ No file came with this message to attach to {task.ref}.")
        return
    files.attach_to_task(ctx.session, f, task, actor)
    result.lines.append(f"📎 File attached to {task.ref}.")


def _create_task(ctx, p: S.CreateTaskParams, result: ExecutionResult, actor: Actor) -> None:
    meta = {"supervisor_can_complete": bool(p.supervisor_can_complete)} if p.agent_key == "supervisor" else {}
    task = tasks.create_task(
        ctx.session, title=p.title, description=p.description or "", priority=p.priority,
        agent_key=p.agent_key, due_at=S.parse_due(p.due_at), actor=actor, created_by_user_id=ctx.user.id,
        source_message_id=ctx.message.id, metadata=meta,
    )
    result.created.append(task)
    result.related_task_id = result.related_task_id or task.id
    for sub in p.subtasks or []:
        tasks.create_task(
            ctx.session, title=sub.title, description=sub.description or "", priority=p.priority,
            agent_key=p.agent_key, actor=actor, created_by_user_id=ctx.user.id,
            source_message_id=ctx.message.id, parent_task_id=task.id, metadata=dict(meta),
        )
    if p.attach_current_file:
        f = _current_file(ctx)
        if f:
            files.attach_to_task(ctx.session, f, task, actor)

    line = f"🆕 {task.ref} logged: {task.title} ({task.agent.key}, {task.priority}"
    if task.due_at:
        line += f", due {fmt_dt(task.due_at)}"
    line += ")"
    if task.status == "blocked":
        line += f"\n   ⛔ Blocked: {tasks.status_reason(task)}. Nothing will happen on it until that agent is live."
    if p.subtasks:
        line += f"\n   + {len(p.subtasks)} subtask{'s' if len(p.subtasks) != 1 else ''}"
    if p.attach_current_file and task.id and _current_file(ctx):
        line += "\n   📎 file attached"
    if start_supervisor_work(ctx.session, task):
        line += "\n   ✍️ Working on it now; I'll send the result shortly."
    result.lines.append(line)


def start_supervisor_work(session: Session, task: Task) -> bool:
    """Queue Supervisor work for a task it can complete by itself."""
    if not (task.agent and task.agent.key == "supervisor"
            and (task.metadata_ or {}).get("supervisor_can_complete")
            and task.status in ("queued", "in_progress")):
        return False
    n_events = len(tasks.recent_events(session, task, limit=1000))
    return queue.enqueue(session, "supervisor_run_task", {"task_id": str(task.id)}, uses_llm=True,
                         idempotency_key=f"run:{task.id}:{n_events}", max_attempts=3) is not None


def _update_task(ctx, p: S.UpdateTaskParams, result: ExecutionResult, actor: Actor) -> None:
    task = tasks.get_by_ref(ctx.session, p.task_ref)
    result.related_task_id = result.related_task_id or task.id
    changes = []
    if p.priority or p.due_at:
        tasks.update_task(ctx.session, task, actor, priority=p.priority, due_at=S.parse_due(p.due_at))
        if p.priority:
            changes.append(f"priority {p.priority}")
        if p.due_at:
            changes.append(f"due {fmt_dt(task.due_at)}")
    if p.note:
        tasks.add_note(ctx.session, task, actor, p.note)
        changes.append("note added")
    if p.status:
        reason = p.note if p.status in ("blocked", "failed") else None
        if p.status in ("blocked", "failed") and not reason:
            reason = "Set by the owner"
        if task.status == "failed" and p.status == "in_progress":
            tasks.change_status(ctx.session, task, "queued", actor)  # failed tasks restart via queued
        tasks.change_status(ctx.session, task, p.status, actor, reason=reason)
        changes.append(STATUS_LABEL[p.status])
        if p.status in ("queued", "in_progress") and start_supervisor_work(ctx.session, task):
            changes.append("working on it now")
    if p.attach_current_file:
        _attach(ctx, task, result, actor)
    if changes:
        result.lines.append(f"✏️ {task.ref} updated: {', '.join(changes)}.")


def _cancel_task(ctx, p: S.CancelTaskParams, result: ExecutionResult, actor: Actor) -> None:
    task = tasks.get_by_ref(ctx.session, p.task_ref)
    tasks.change_status(ctx.session, task, "cancelled", actor, reason=p.reason)
    result.related_task_id = result.related_task_id or task.id
    result.lines.append(f"🗑️ {task.ref} cancelled: {task.title}")


def _get_status(ctx, p: S.GetStatusParams, result: ExecutionResult, actor: Actor) -> None:
    s = ctx.session
    if p.task_ref:
        task = tasks.get_by_ref(s, p.task_ref)
        result.related_task_id = result.related_task_id or task.id
        result.lines.append(summaries.task_detail(s, task.ref))
    elif p.scope == "today":
        result.lines.append(summaries.today_view(s))
    elif p.scope and p.scope.startswith("agent:"):
        result.lines.append(summaries.agent_scope(s, p.scope.split(":", 1)[1]))
    else:
        result.lines.append(summaries.status_overview(s))


def _list_tasks(ctx, p: S.ListTasksParams, result: ExecutionResult, actor: Actor) -> None:
    result.lines.append(summaries.filtered_list(
        ctx.session, statuses=p.statuses, agent_key=p.agent_key, priority=p.priority, due=p.due))


def _remember(ctx, p: S.RememberParams, result: ExecutionResult, actor: Actor) -> None:
    entry = memory.create(ctx.session, category=p.category, title=p.title, content=p.content, actor=actor,
                          applies_to_agent_key=p.applies_to_agent_key, trigger_message_id=ctx.message.id)
    scope = f" (for {entry.applies_to_agent_key})" if entry.applies_to_agent_key else ""
    result.lines.append(f"🧠 Saved to company memory{scope}: {entry.title}")
    from app.email import drafting

    if drafting.resume_pending_after_signature(ctx.session):
        result.lines.append("✍️ Signature saved — writing the email draft you asked for now.")


def _email_draft_request(ctx, p: S.EmailDraftRequestParams, result: ExecutionResult, actor: Actor) -> None:
    from app.email import drafting

    thread = drafting.resolve_thread(ctx.session, ref=p.thread_ref, sender=p.sender, subject=p.subject_contains)
    result.lines.append(drafting.request_draft(ctx.session, ctx.user, thread, p.instructions))


def _resolve_listing(ctx, p):
    from app.amazon import listings

    if p.listing_ref:
        return listings.get_by_ref(ctx.session, p.listing_ref)
    rows = listings.find(ctx.session, sku=p.sku) if p.sku else (listings.find(ctx.session, asin=p.asin)
                                                                if p.asin else [])
    if not rows:
        raise ServiceError("Which listing? Give me the ref (L-4), the SKU or the ASIN.")
    if len(rows) > 1:
        raise ServiceError("That SKU or ASIN exists in more than one marketplace; give me the ref (L-4).")
    return rows[0]


def _amazon_edit_listing(ctx, p: S.AmazonEditListingParams, result: ExecutionResult, actor: Actor) -> None:
    from app.amazon import changes, views

    listing = _resolve_listing(ctx, p)
    changed = {k: v for k, v in (p.changes or {}).items() if v is not None}
    request = changes.build(ctx.session, listing, changed, operation="patch_attributes",
                            instructions=p.instructions or ctx.message.body or "",
                            source_message_id=ctx.message.id)
    result.lines.append(views.change_detail(ctx.session, request))
    if request.status == "ready_for_approval":
        result.lines.append(changes.request_approval(ctx.session, request, ctx.user))


def _amazon_update_price(ctx, p: S.AmazonUpdatePriceParams, result: ExecutionResult, actor: Actor) -> None:
    from app.amazon import changes, views

    listing = _resolve_listing(ctx, p)
    request = changes.price_change(ctx.session, listing, p.price,
                                   instructions=p.instructions or ctx.message.body or "",
                                   source_message_id=ctx.message.id)
    result.lines.append(views.change_detail(ctx.session, request))
    if request.status == "ready_for_approval":
        result.lines.append(changes.request_approval(ctx.session, request, ctx.user))


def _amazon_create_listing(ctx, p: S.AmazonCreateListingParams, result: ExecutionResult, actor: Actor) -> None:
    from app.amazon import intake

    result.lines.append(intake.start(ctx.session, user=ctx.user, sku=p.sku, marketplace_code=p.marketplace,
                                     details=p.details, asin=p.asin, create_anyway=p.create_anyway,
                                     message_id=ctx.message.id))


def _amazon_create_variations(ctx, p: S.AmazonCreateVariationsParams, result: ExecutionResult,
                              actor: Actor) -> None:
    from app.amazon import changes, intake, variations, views

    children = variations.parse_grid(p.children, p.theme)
    if len(children) < 2:
        raise ServiceError("Send the children one per line as \"SKU | value | price\", at least two of them.")
    parent = _resolve_listing(ctx, S.AmazonEditListingParams(sku=p.parent_sku, changes={"x": "y"})) \
        if listings_exists(ctx, p.parent_sku) else None
    product_type = parent.product_type if parent else None
    base = changes.current_attributes(ctx.session, parent) if parent else {}
    if not product_type:
        raise ServiceError(f"I don't have {p.parent_sku} yet. Create the parent listing first, then I'll "
                           "build the family around it.")
    request = variations.build_family(
        ctx.session, parent_sku=p.parent_sku, theme=p.theme, children=children, product_type=product_type,
        marketplace_code=p.marketplace, base_attributes=base, instructions=p.details or ctx.message.body or "",
        source_message_id=ctx.message.id)
    result.lines.append(views.change_detail(ctx.session, request))
    if request.status == "ready_for_approval":
        result.lines.append(changes.request_approval(ctx.session, request, ctx.user))


def listings_exists(ctx, sku: str) -> bool:
    from app.amazon import listings

    return bool(listings.find(ctx.session, sku=sku))


def _amazon_modify_variations(ctx, p: S.AmazonModifyVariationsParams, result: ExecutionResult,
                              actor: Actor) -> None:
    from app.amazon import changes, listings, variations, views

    if p.action == "remove_child":
        child = listings.get_by_ref(ctx.session, p.child_ref or "")
        request = variations.remove_child(ctx.session, child, instructions=ctx.message.body or "")
    else:
        parent = listings.get_by_ref(ctx.session, p.parent_ref or "")
        parsed = variations.parse_grid(p.child or "", p.theme or parent.variation_theme or "SIZE")
        if not parsed:
            raise ServiceError("Send the child as \"SKU | value | price\" and I'll add it.")
        request = variations.add_child(ctx.session, parent, parsed[0],
                                       theme=p.theme or parent.variation_theme or "SIZE",
                                       base_attributes=changes.current_attributes(ctx.session, parent),
                                       instructions=ctx.message.body or "")
    result.lines.append(views.change_detail(ctx.session, request))
    result.lines.append(changes.request_approval(ctx.session, request, ctx.user))


def _amazon_copy_marketplace(ctx, p: S.AmazonCopyMarketplaceParams, result: ExecutionResult,
                             actor: Actor) -> None:
    from app.amazon import canada, changes, views

    if p.brand:
        requests = canada.copy_brand(ctx.session, p.brand, target_code=p.target,
                                     prices=canada.parse_prices(p.prices or ""))
        result.lines.append(f"🇨🇦 Drafted {len(requests)} {p.target} listing(s) for {p.brand}: "
                            + ", ".join(r.ref for r in requests))
        for request in requests:
            if request.status == "ready_for_approval":
                result.lines.append(changes.request_approval(ctx.session, request, ctx.user))
        return
    listing = _resolve_listing(ctx, p)
    request = canada.copy_listing(ctx.session, listing, target_code=p.target, price=p.price,
                                  source_message_id=ctx.message.id)
    result.lines.append(canada.copy_summary(ctx.session, request, listing, p.target))
    result.lines.append(views.change_detail(ctx.session, request))
    if request.status == "ready_for_approval":
        result.lines.append(changes.request_approval(ctx.session, request, ctx.user))


def _amazon_delete_listing(ctx, p: S.AmazonDeleteListingParams, result: ExecutionResult, actor: Actor) -> None:
    from app.amazon import changes, variations, views

    listing = _resolve_listing(ctx, S.AmazonEditListingParams(listing_ref=p.listing_ref, sku=p.sku,
                                                              changes={"x": "y"}))
    request = variations.build_delete(ctx.session, listing, instructions=ctx.message.body or "")
    result.lines.append(views.change_detail(ctx.session, request))
    result.lines.append(changes.request_approval(ctx.session, request, ctx.user))


def _fba_create_shipment(ctx, p: S.FbaCreateShipmentParams, result: ExecutionResult, actor: Actor) -> None:
    from app.amazon import fba_plan

    result.lines.append(fba_plan.start(ctx.session, user=ctx.user, details=p.details,
                                       marketplace_code=p.marketplace, message_id=ctx.message.id))


def _fba_provide_info(ctx, p: S.FbaProvideInfoParams, result: ExecutionResult, actor: Actor) -> None:
    from app.amazon import fba_plan

    result.lines.append(fba_plan.answer(ctx.session, user=ctx.user, answers=p.answers,
                                        message_id=ctx.message.id))


def _fba_set_case_pack(ctx, p: S.FbaSetCasePackParams, result: ExecutionResult, actor: Actor) -> None:
    from app.db.models import Product

    listing = _resolve_listing(ctx, S.AmazonEditListingParams(listing_ref=p.listing_ref, sku=p.sku,
                                                              changes={"x": "y"}))
    product = ctx.session.get(Product, listing.product_id) if listing.product_id else None
    if product is None:
        product = Product(internal_name=listing.title_cached or listing.sku, brand_id=listing.brand_id)
        ctx.session.add(product)
        ctx.session.flush()
        listing.product_id = product.id
    product.units_per_case = p.units_per_case
    product.case_dimensions = {"length_in": p.length_in, "width_in": p.width_in, "height_in": p.height_in}
    product.case_weight_lb = p.weight_lb
    result.lines.append(f"📦 Saved the case pack for {listing.sku}: {p.units_per_case} units, "
                        f"{p.length_in:g}×{p.width_in:g}×{p.height_in:g} in, {p.weight_lb:g} lb. "
                        "Next time just tell me how many cases.")


def _fba_set_ship_from(ctx, p: S.FbaSetShipFromParams, result: ExecutionResult, actor: Actor) -> None:
    from sqlalchemy import select as _select

    from app.db.models import ShipFromAddress

    address = ctx.session.execute(_select(ShipFromAddress)
                                  .where(ShipFromAddress.is_default.is_(True))).scalar_one_or_none()
    if address is None:
        raise ServiceError("There's no default ship-from address to update.")
    for field_ in ("phone", "email", "address_line1", "city", "state_or_province", "postal_code"):
        value = getattr(p, field_, None)
        if value:
            setattr(address, field_, value)
    result.lines.append(f"📍 Ship-from address updated: {address.address_line1}, {address.city} "
                        f"{address.state_or_province} {address.postal_code}"
                        + (f" · {address.phone}" if address.phone else "")
                        + (f" · {address.email}" if address.email else ""))


def _fba_set_ready_date(ctx, p: S.FbaSetReadyDateParams, result: ExecutionResult, actor: Actor) -> None:
    from app.amazon import fba_plan

    request = fba_plan.get_by_ref(ctx.session, p.request_ref)
    result.lines.append(fba_plan.set_ready_date(ctx.session, request, p.date))


def _amazon_provide_info(ctx, p: S.AmazonProvideInfoParams, result: ExecutionResult, actor: Actor) -> None:
    from app.amazon import intake

    result.lines.append(intake.answer(ctx.session, user=ctx.user, answers=p.answers, message_id=ctx.message.id))


def _amazon_revert(ctx, p: S.AmazonRevertParams, result: ExecutionResult, actor: Actor) -> None:
    from app.amazon import changes, listings, views

    listing = listings.get_by_ref(ctx.session, p.listing_ref)
    request = changes.build_revert(ctx.session, listing, p.version, ctx.user)
    result.lines.append(views.change_detail(ctx.session, request))
    if request.status == "ready_for_approval":
        result.lines.append(changes.request_approval(ctx.session, request, ctx.user))


def _amazon_query(ctx, p: S.AmazonQueryParams, result: ExecutionResult, actor: Actor) -> None:
    from app.amazon import views

    result.lines.append({"listings": views.listings_command, "stock": views.stock_command,
                         "changes": views.changes_command}[p.question](ctx.session))


def _email_new_draft(ctx, p: S.EmailNewDraftParams, result: ExecutionResult, actor: Actor) -> None:
    from app.email import drafting

    result.lines.append(drafting.request_new_email(ctx.session, ctx.user, to=p.to, cc=p.cc, subject=p.subject,
                                                   instructions=p.instructions, owner_text=ctx.message.body or ""))


def _email_attach_file(ctx, p: S.EmailAttachFileParams, result: ExecutionResult, actor: Actor) -> None:
    from app.email import drafting

    if p.remove:
        result.lines.append(drafting.detach_file(ctx.session, ctx.user, p.draft_ref, p.remove))
        return
    file = drafting.recent_whatsapp_file(ctx.session, ctx.user, ctx.message)
    result.lines.append(drafting.attach_file(ctx.session, ctx.user, p.draft_ref, file))


def _email_edit_draft(ctx, p: S.EmailEditDraftParams, result: ExecutionResult, actor: Actor) -> None:
    from app.email import drafting

    result.lines.append(drafting.request_edit(ctx.session, ctx.user, p.draft_ref, p.changes))


def _email_search(ctx, p: S.EmailSearchParams, result: ExecutionResult, actor: Actor) -> None:
    from app.email import views

    result.lines.append(views.search(ctx.session, sender=p.sender, subject=p.subject_contains,
                                     since_days=p.since_days, category=p.category,
                                     needs_reply_only=bool(p.needs_reply_only)))


def _email_summary(ctx, p: S.EmailSummaryParams, result: ExecutionResult, actor: Actor) -> None:
    from app.email import views

    result.lines.append(views.period_summary(ctx.session, p.period))


def _email_set_followup(ctx, p: S.EmailSetFollowupParams, result: ExecutionResult, actor: Actor) -> None:
    from app.email import followup, threads

    thread = threads.get_by_ref(ctx.session, p.thread_ref)
    if p.stop:
        thread.follow_up_tracking = False
        thread.follow_up_due_at = None
        result.lines.append(f"👍 Stopped tracking follow-ups for {thread.ref}.")
    else:
        if not p.days:
            raise ServiceError("In how many days should I remind you?")
        thread.follow_up_tracking = True
        thread.follow_up_due_at = followup.add_business_days(utcnow(), p.days)
        if thread.status == "open" and thread.last_message_from_owner:
            thread.status = "waiting_on_them"
        result.lines.append(f"⏰ I'll flag {thread.ref} on {fmt_dt(thread.follow_up_due_at, with_time=False)} "
                            "if there's no reply.")
    audit.record(ctx.session, actor, "email_followup_changed", target_type="email_thread", target_id=thread.ref,
                 details={"days": p.days, "stop": bool(p.stop)})


def _owner_text(ctx) -> str:
    from app.email.drafting import normalize_typed

    return normalize_typed(ctx.message.body)


def _owner_typed(ctx, value: str) -> bool:
    """Guard for settings-like email actions: the value must come from the owner's own message."""
    text = _owner_text(ctx)
    value = (value or "").strip().lower().lstrip("@")
    if not value:
        return False
    if value in text:
        return True
    words = [w for w in re.findall(r"\w{4,}", value)]
    return bool(words) and sum(w in text for w in words) / len(words) >= 0.6


def _email_add_alert_rule(ctx, p: S.EmailAddAlertRuleParams, result: ExecutionResult, actor: Actor) -> None:
    from app.email import rules

    if not re.search(r"alert|notif|ignore|summary|tell me|let me know|stop|mute|warn", _owner_text(ctx)) \
            or not _owner_typed(ctx, p.value):
        raise ServiceError("I can only change email alert rules when you ask directly, naming the sender, "
                           "domain or topic in your message.")
    rule = rules.add_owner_rule(ctx.session, rule_type=p.rule_type, value=p.value, alert_level=p.alert_level,
                                bypass_quiet_hours=bool(p.bypass_quiet_hours), actor=actor)
    what = {"instant": "alert you right away about", "daily_summary_only": "only put in the daily summary",
            "ignore": "ignore"}[rule.alert_level]
    result.lines.append(f"🔔 Rule saved: I'll {what} emails matching {rule.rule_type.replace('_', ' ')} "
                        f"\"{rule.value}\".")


def _email_remove_alert_rule(ctx, p: S.EmailRemoveAlertRuleParams, result: ExecutionResult,
                             actor: Actor) -> None:
    from app.email import rules

    if not re.search(r"remove|delete|undo|cancel|stop ignoring|unignore|un-ignore|no longer|don't ignore|"
                     r"do not ignore|get rid", _owner_text(ctx)) or not _owner_typed(ctx, p.value):
        raise ServiceError("I can only remove an email rule when you ask directly and name it. "
                           "Send RULES to see them, then REMOVE RULE <number>.")
    found = rules.find_owner_rules(ctx.session, p.value)
    if not found:
        raise ServiceError(f'I don\'t have a rule for "{p.value}". Send RULES to see your rules.')
    if len(found) > 1:
        raise ServiceError(f'Several rules match "{p.value}". Send RULES, then REMOVE RULE <number>.')
    rules.remove_owner_rule(ctx.session, found[0], actor)
    result.lines.append(f"🗑️ Rule removed: {rules.describe(found[0])}.")


def _email_list_alert_rules(ctx, p: S.EmptyParams, result: ExecutionResult, actor: Actor) -> None:
    from app.email import views

    result.lines.append(views.rules_command(ctx.session))


def _email_set_contact_type(ctx, p: S.EmailSetContactTypeParams, result: ExecutionResult, actor: Actor) -> None:
    from app.email import contacts

    if not _owner_typed(ctx, p.address):
        raise ServiceError("Tell me the email address in your message and I'll update that contact.")
    c = contacts.set_type(ctx.session, p.address, type=p.contact_type, is_vip=p.is_vip, actor=actor)
    bits = [c.type] + (["VIP"] if c.is_vip else [])
    result.lines.append(f"👤 {c.address} saved as {' / '.join(bits)}.")


def _email_update_thread_status(ctx, p: S.EmailUpdateThreadStatusParams, result: ExecutionResult,
                                actor: Actor) -> None:
    from app.email import threads

    thread = threads.get_by_ref(ctx.session, p.thread_ref)
    threads.set_status(ctx.session, thread, p.status, actor)
    result.lines.append(f"👍 {thread.ref} marked {p.status}.")


def _request_report(ctx, p: S.RequestReportParams, result: ExecutionResult, actor: Actor) -> None:
    result.lines.append(summaries.daily_summary(ctx.session))


def _nothing(ctx, p, result, actor) -> None:
    return None


_HANDLERS = {
    "create_task": _create_task,
    "update_task": _update_task,
    "cancel_task": _cancel_task,
    "get_status": _get_status,
    "list_tasks": _list_tasks,
    "answer_question": _nothing,
    "remember": _remember,
    "request_clarification": _nothing,
    "request_report": _request_report,
    "no_op": _nothing,
    "email_draft_request": _email_draft_request,
    "email_new_draft": _email_new_draft,
    "amazon_edit_listing": _amazon_edit_listing,
    "amazon_update_price": _amazon_update_price,
    "amazon_create_listing": _amazon_create_listing,
    "amazon_provide_info": _amazon_provide_info,
    "amazon_create_variations": _amazon_create_variations,
    "amazon_modify_variations": _amazon_modify_variations,
    "amazon_delete_listing": _amazon_delete_listing,
    "amazon_copy_marketplace": _amazon_copy_marketplace,
    "fba_create_shipment": _fba_create_shipment,
    "fba_provide_info": _fba_provide_info,
    "fba_set_case_pack": _fba_set_case_pack,
    "fba_set_ship_from": _fba_set_ship_from,
    "fba_set_ready_date": _fba_set_ready_date,
    "amazon_revert": _amazon_revert,
    "amazon_query": _amazon_query,
    "email_attach_file": _email_attach_file,
    "email_edit_draft": _email_edit_draft,
    "email_search": _email_search,
    "email_summary": _email_summary,
    "email_set_followup": _email_set_followup,
    "email_add_alert_rule": _email_add_alert_rule,
    "email_remove_alert_rule": _email_remove_alert_rule,
    "email_list_alert_rules": _email_list_alert_rules,
    "email_set_contact_type": _email_set_contact_type,
    "email_update_thread_status": _email_update_thread_status,
}
