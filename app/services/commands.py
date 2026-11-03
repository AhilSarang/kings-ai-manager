"""Deterministic WhatsApp commands (Section 8.3). No LLM involved.

Matching is case-insensitive and whole-message. Anything that doesn't match
goes to the Supervisor LLM.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db.models import Message, User
from app.jobs import queue
from app.jobs.handlers import CRITICAL_TYPES
from app.services import settings_store, summaries, tasks
from app.services.common import Actor, NotFound, ServiceError


@dataclass
class CommandContext:
    session: Session
    user: User
    message: Message
    match: re.Match


@dataclass
class CommandResult:
    text: str
    related_task_id: object = None
    urgent: bool = True  # direct replies to the owner are never held for quiet hours


@dataclass
class Command:
    name: str
    pattern: re.Pattern
    handler: Callable[[CommandContext], CommandResult]
    allowed_when_paused: bool
    help: str


COMMANDS: list[Command] = []


def command(name: str, pattern: str, *, paused_ok: bool, help: str):
    def register(fn):
        COMMANDS.append(Command(name, re.compile(rf"^\s*{pattern}\s*[.!]?\s*$", re.I | re.S), fn, paused_ok, help))
        return fn

    return register


def match(text: str) -> tuple[Command, re.Match] | None:
    for cmd in COMMANDS:
        m = cmd.pattern.match(text or "")
        if m:
            return cmd, m
    return None


# Jobs that STOP ALL leaves in place so nothing the owner sent is lost.
PRESERVED_ON_STOP = {"supervisor_interpret", "download_media", "execute_approval",
                     "triage_emails", "send_email_alert", "draft_email_reply", "email_fetch_attachment"}


@command("HELP", r"(help|commands|\?)", paused_ok=True, help="HELP — this list")
def _help(ctx: CommandContext) -> CommandResult:
    lines = ["*Commands*"] + [f"• {c.help}" for c in COMMANDS if c.help]
    lines.append("\nOr just write normally, e.g. \"Log a task to call the landlord Friday\".")
    return CommandResult("\n".join(lines))


@command("STATUS", r"status", paused_ok=True, help="STATUS — open tasks by status + approvals")
def _status(ctx: CommandContext) -> CommandResult:
    text = summaries.status_overview(ctx.session)
    if settings_store.is_paused(ctx.session):
        text = "⏸️ System is PAUSED (send RESUME to continue)\n\n" + text
    return CommandResult(text)


@command("TASKS", r"tasks", paused_ok=True, help="TASKS — open tasks, most urgent first")
def _tasks(ctx: CommandContext) -> CommandResult:
    return CommandResult(summaries.open_tasks_list(ctx.session))


@command("TASK", r"(T-?\d+)", paused_ok=True, help="T-12 — details of a task")
def _task(ctx: CommandContext) -> CommandResult:
    task = tasks.get_by_ref(ctx.session, ctx.match.group(1))
    return CommandResult(summaries.task_detail(ctx.session, task.ref), related_task_id=task.id)


@command("DONE", r"done\s+(T-?\d+)", paused_ok=False, help="DONE T-12 — mark a task done")
def _done(ctx: CommandContext) -> CommandResult:
    task = tasks.get_by_ref(ctx.session, ctx.match.group(1))
    tasks.change_status(ctx.session, task, "done", Actor.user(ctx.user.id))
    return CommandResult(f"✅ {task.ref} marked done: {task.title}", related_task_id=task.id)


@command("PAUSE", r"(pause|stop all|stop)", paused_ok=True, help="PAUSE / STOP ALL — halt all agent work")
def _pause(ctx: CommandContext) -> CommandResult:
    actor = Actor.user(ctx.user.id)
    already = settings_store.is_paused(ctx.session)
    settings_store.set_value(ctx.session, "system_paused", True, actor)
    cancelled = queue.cancel_pending(ctx.session, keep_types=CRITICAL_TYPES | PRESERVED_ON_STOP)
    head = "⏸️ Already paused." if already else "⏸️ STOPPED. All agent work is paused."
    return CommandResult(
        f"{head} Cancelled {cancelled} queued background job{'s' if cancelled != 1 else ''}. "
        "Your messages are still saved. Status commands still work. Send RESUME to continue."
    )


@command("RESUME", r"(resume|start|unpause)", paused_ok=True, help="RESUME — restart after a pause")
def _resume(ctx: CommandContext) -> CommandResult:
    if not settings_store.is_paused(ctx.session):
        return CommandResult("▶️ The system is already running.")
    settings_store.set_value(ctx.session, "system_paused", False, Actor.user(ctx.user.id))
    requeued = requeue_supervisor_work(ctx.session)
    _restart_email(ctx.session)
    extra = f" Restarted {requeued} task{'s' if requeued != 1 else ''}." if requeued else ""
    waiting = _approved_waiting(ctx.session)
    if waiting:
        extra += f" Approved action{'s' if len(waiting) > 1 else ''} {', '.join(waiting)} will now run."
    return CommandResult(f"▶️ Resumed. Saved messages will be handled now.{extra}")


@command("SUMMARY", r"(summary|daily summary|report)", paused_ok=True, help="SUMMARY — daily summary now")
def _summary(ctx: CommandContext) -> CommandResult:
    return CommandResult(summaries.daily_summary(ctx.session))


def _approved_waiting(session: Session) -> list[str]:
    from sqlalchemy import select

    from app.db.models import Approval

    return list(session.execute(select(Approval.ref).where(Approval.status == "approved")).scalars())


def requeue_supervisor_work(session: Session) -> int:
    """After RESUME: restart Supervisor task work that STOP ALL cancelled."""
    from sqlalchemy import select

    from app.db.models import Task
    from app.services import agents

    sup = agents.get_by_key(session, "supervisor")
    pending = session.execute(
        select(Task).where(Task.assigned_agent_id == sup.id, Task.status == "queued",
                           Task.result_summary.is_(None))
    ).scalars().all()
    n = 0
    for t in pending:
        if (t.metadata_ or {}).get("supervisor_can_complete"):
            if queue.enqueue(session, "supervisor_run_task", {"task_id": str(t.id)}, uses_llm=True,
                             idempotency_key=f"run:{t.id}:{t.updated_at.timestamp()}"):
                n += 1
    return n


def run(ctx_session: Session, user: User, message: Message) -> CommandResult | None:
    found = match(message.body)
    if not found:
        return _affirmation_guard(ctx_session, message.body)
    cmd, m = found
    if settings_store.is_paused(ctx_session) and not cmd.allowed_when_paused:
        return CommandResult("⏸️ The system is paused, so I can't do that now. Send RESUME first.")
    try:
        return cmd.handler(CommandContext(ctx_session, user, message, m))
    except (NotFound, ServiceError) as exc:
        return CommandResult(f"⚠️ {exc}")


# ---------------- approvals (M7) ----------------
AFFIRMATIONS = re.compile(
    r"^\s*(ok(ay)?|yes|yeah|yep|sure|approve[d]?|go ahead|do it|confirm(ed)?|proceed|fine|"
    r"send( it| now| the email)?|👍|✅)\s*[.!]*\s*$", re.I)


@command("APPROVE",
         r"approve\s+(A\s*-?\s*\d+)(?:\s+([A-Za-z0-9]{3,8}))?(?:\s+(\d{4,12}))?(?:\s+([\w.\-/]+))?",
         paused_ok=False,
         help="APPROVE A-7 CODE — approve an action")
def _approve(ctx: CommandContext) -> CommandResult:
    from app.services import approvals

    if ctx.match.group(3):
        _redact_pin(ctx.message)
    a = approvals.approve(ctx.session, ctx.user, ctx.match.group(1), ctx.match.group(2), ctx.match.group(3),
                          ctx.match.group(4))
    return CommandResult(f"👍 {a.ref} approved. Executing now; I'll confirm when it's done.",
                         related_task_id=a.task_id)


@command("REJECT", r"reject\s+(A\s*-?\s*\d+)(?:\s+(.+))?", paused_ok=True, help="REJECT A-7 — reject an action")
def _reject(ctx: CommandContext) -> CommandResult:
    from app.services import approvals

    a = approvals.reject(ctx.session, ctx.user, ctx.match.group(1), ctx.match.group(2))
    return CommandResult(f"🚫 {a.ref} rejected. Nothing will be done.", related_task_id=a.task_id)


@command("APPROVALS", r"approvals", paused_ok=True, help="APPROVALS — list pending approvals")
def _approvals(ctx: CommandContext) -> CommandResult:
    from app.services import approvals

    return CommandResult(approvals.list_pending_text(ctx.session))


@command("DEMO", r"demo approval", paused_ok=False, help="DEMO APPROVAL — test the approval flow safely")
def _demo(ctx: CommandContext) -> CommandResult:
    from app.services import approvals

    a = approvals.create_demo(ctx.session, ctx.user)
    from app.db.models import Task

    task = ctx.session.get(Task, a.task_id)
    return CommandResult("🧪 Demo created — nothing real will happen.\n\n" + approvals.approval_message(a, task),
                         related_task_id=a.task_id)


def _redact_pin(message: Message) -> None:
    """Never keep the approval PIN in stored messages."""
    redacted = PIN_RE.sub(lambda m: f"{m.group(1)} ****", message.body)
    message.body = redacted
    raw = dict(message.raw_payload or {})
    if isinstance(raw.get("text"), dict):
        raw["text"] = {**raw["text"], "body": redacted}
    message.raw_payload = raw


PIN_RE = re.compile(r"(approve\s+A-?\d+\s+[A-Za-z0-9]{3,8})\s+\d{4,12}", re.I)
CODE_RE = re.compile(r"(approve\s+A-?\d+)\s+[A-Za-z0-9]{3,8}(\s+\S+)?", re.I)


def mask_codes(text: str) -> str:
    """Hide approval codes/PINs (used before text is shown to an LLM)."""
    return CODE_RE.sub(lambda m: f"{m.group(1)} ****", text or "")


def _affirmation_guard(ctx_session: Session, text: str) -> CommandResult | None:
    """'ok', 'yes'… never approve anything. If approvals are pending, say how to approve."""
    if not AFFIRMATIONS.match(text or ""):
        return None
    from app.services.summaries import pending_approvals

    pending = pending_approvals(ctx_session)
    if not pending:
        from sqlalchemy import select

        from app.db.models import EmailDraft

        open_drafts = list(ctx_session.execute(
            select(EmailDraft.ref).where(EmailDraft.status == "draft").order_by(EmailDraft.created_at.desc())
        ).scalars())
        if open_drafts:
            return CommandResult(f"Nothing was sent. To send a draft, reply SEND {open_drafts[0]} "
                                 f"(open drafts: {', '.join(open_drafts[:5])}).")
        return None
    refs = ", ".join(a.ref for a in pending[:5])
    return CommandResult(
        f"Nothing was approved. To approve, reply with the exact command, e.g. "
        f"APPROVE {pending[0].ref} <code> (pending: {refs}). Reply APPROVALS to see the codes."
    )


# ---------- PPC approvals (Phase 4 M6) ----------
# These sit above APPROVE_MALFORMED on purpose: that pattern catches anything starting with
# "approve" or "reject", so a PPC approval registered after it would never be reached.
@command("PPC_APPROVE_BATCH",
         r"approve\s+batch\s+(B\s*-?\s*\d+)\s+([A-Z0-9]{4})(?:\s+(\d{4,8}))?",
         paused_ok=False, help="APPROVE BATCH B-7 CODE — approve a whole advertising digest")
def _ppc_approve_batch(ctx: CommandContext) -> CommandResult:
    from app.ppc import recommendations

    if ctx.match.group(3):
        _redact_pin(ctx.message)
    got = recommendations.decide(ctx.session, ctx.user, batch_ref=ctx.match.group(1),
                                 code=ctx.match.group(2), pin=ctx.match.group(3))
    return CommandResult(f"👍 {got['batch']} approved — {len(got['approved'])} changes. "
                         "Applying them now; I'll tell you what landed.")


@command("PPC_APPROVE_SOME",
         r"approve\s+((?:P\s*-?\s*\d+\s*[, ]\s*)*P\s*-?\s*\d+)\s+([A-Z0-9]{4})(?:\s+(\d{4,8}))?",
         paused_ok=False, help="APPROVE P-14,P-15 CODE — approve some of a digest")
def _ppc_approve_some(ctx: CommandContext) -> CommandResult:
    from app.ppc import recommendations

    if ctx.match.group(3):
        _redact_pin(ctx.message)
    refs = [r for r in re.split(r"[,\s]+", ctx.match.group(1)) if r.strip()]
    got = recommendations.decide(ctx.session, ctx.user, refs=refs, code=ctx.match.group(2),
                                 pin=ctx.match.group(3))
    extra = ""
    if got["skipped"]:
        extra = (f" The other {len(got['skipped'])} in {got['batch']} are closed — ask for a fresh "
                 "digest if you want them.")
    if got["unknown"]:
        extra += f" I don't have {', '.join(got['unknown'])}."
    return CommandResult(f"👍 Approved {', '.join(got['approved'])}. Applying now.{extra}")


@command("PPC_REJECT", r"reject\s+(P\s*-?\s*\d+)(?:\s+(.+))?", paused_ok=True,
         help="REJECT P-16 why — turn one down; the reason teaches me something")
def _ppc_reject(ctx: CommandContext) -> CommandResult:
    from app.ppc import recommendations

    row = recommendations.reject(ctx.session, ctx.user, ctx.match.group(1), ctx.match.group(2))
    if row.rejected_reason:
        return CommandResult(f"🚫 {row.ref} turned down: \u201c{row.rejected_reason}\u201d. "
                             "I'll remember that when I see the same kind of thing.")
    return CommandResult(f"🚫 {row.ref} turned down. If you tell me why, I can avoid proposing it "
                         "again — `REJECT {row.ref} too aggressive`.")


@command("PPC_ROLLBACK_BATCH", r"roll\s*back\s+batch\s+(B\s*-?\s*\d+)", paused_ok=True,
         help="ROLLBACK BATCH B-7 — put a whole batch back")
def _ppc_rollback_batch(ctx: CommandContext) -> CommandResult:
    from app.ppc import apply as ppc_apply
    from app.ppc import recommendations

    run = recommendations.batch_by_ref(ctx.session, ctx.match.group(1))
    got = ppc_apply.rollback_batch(ctx.session, run, actor=Actor.user(ctx.user.id))
    text = f"↩️ {got['batch']}: put back {len(got['rolled_back'])} changes."
    if got["problems"]:
        text += (" Couldn't undo " + ", ".join(f"{p['ref']} ({p['why'][:60]})"
                                               for p in got["problems"][:3]))
    return CommandResult(text)


@command("PPC_ROLLBACK", r"roll\s*back\s+(P\s*-?\s*\d+)", paused_ok=True,
         help="ROLLBACK P-14 — put one change back (no approval needed)")
def _ppc_rollback(ctx: CommandContext) -> CommandResult:
    from app.ppc import apply as ppc_apply
    from app.ppc import recommendations

    row = recommendations.get_by_ref(ctx.session, ctx.match.group(1))
    ppc_apply.rollback(ctx.session, row, actor=Actor.user(ctx.user.id))
    return CommandResult(f"↩️ {row.ref} put back to what it was.")


@command("PPC_BATCH_DETAILS", r"(B\s*-?\s*\d+)(?:\s+details?)?", paused_ok=True,
         help="B-7 DETAILS — every recommendation in a digest")
def _ppc_batch_details(ctx: CommandContext) -> CommandResult:
    from app.ppc import recommendations

    return CommandResult(recommendations.batch_details(ctx.session, ctx.match.group(1)))


@command("PPC_DETAILS", r"(P\s*-?\s*\d+)(?:\s+details?)?", paused_ok=True,
         help="P-14 DETAILS — the numbers behind one recommendation")
def _ppc_details(ctx: CommandContext) -> CommandResult:
    from app.ppc import recommendations

    return CommandResult(recommendations.details(ctx.session, ctx.match.group(1)))


# ---------- PPC experiments (Phase 4 M8) ----------
@command("PPC_EXPERIMENT_STOP", r"stop\s+(X\s*-?\s*\d+)(?:\s+(.+))?", paused_ok=True,
         help="STOP X-1 — end an experiment and put the bids back (no approval needed)")
def _ppc_experiment_stop(ctx: CommandContext) -> CommandResult:
    from app.ppc import experiments

    row = experiments.get_by_ref(ctx.session, ctx.match.group(1), lock=True)
    reason = (ctx.match.group(2) or "").strip() or "you asked me to stop it"
    experiments.stop(ctx.session, row, reason=reason, actor=Actor.user(ctx.user.id))
    return CommandResult(f"🛑 {row.ref} stopped and the treated bids put back to what they were. "
                         "Nothing is left half-changed.")


@command("PPC_EXPERIMENTS", r"(?:experiments?|ppc\s+(?:experiments?|tests?))", paused_ok=True,
         help="EXPERIMENTS — what's being tested and what the finished ones showed")
def _ppc_experiments(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.experiments_summary(ctx.session))


@command("PPC_EXPERIMENT", r"(X\s*-?\s*\d+)(?:\s+details?)?", paused_ok=True,
         help="X-1 — one experiment in full, including what it could and couldn't detect")
def _ppc_experiment(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.experiment_detail(ctx.session, ctx.match.group(1)))


# ---------- the learned playbook (Phase 4 M9) ----------
@command("PPC_PLAYBOOK", r"(?:(?:ppc\s+)?playbook|what\s+have\s+you\s+learn(?:ed|t))",
         paused_ok=True, help="PLAYBOOK — the rules I've learned and how each is doing")
def _ppc_playbook(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.playbook_summary(ctx.session))


@command("PPC_PLAYBOOK_RULE", r"(R\s*-?\s*\d+)(?:\s+details?)?", paused_ok=True,
         help="R-1 — one learned rule, with the evidence behind it")
def _ppc_playbook_rule(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.playbook_detail(ctx.session, ctx.match.group(1)))


@command("PPC_PLAYBOOK_RETIRE", r"retire\s+(R\s*-?\s*\d+)", paused_ok=True,
         help="RETIRE R-1 — ask to retire a learned rule")
def _ppc_playbook_retire(ctx: CommandContext) -> CommandResult:
    from app.ppc import playbook

    rule = playbook.get_by_ref(ctx.session, ctx.match.group(1))
    approval = playbook.request_approval(ctx.session, rule, retire=True)
    return CommandResult(f"📘 Retiring {rule.ref} needs one confirmation: "
                         f"`APPROVE {approval.ref} {approval.confirmation_code}`. "
                         "It keeps shaping recommendations until then.")


# ---------- alerts, budgets and dayparting (Phase 4 M11) ----------
# Every alternation is grouped. An ungrouped one binds the anchors to each branch, which is how the
# original `PPC` command came to match "ppc please cut the bids on skyzz" and swallow an instruction.
@command("PPC_ALERTS",
         r"(?:(?:ppc\s+|ads?\s+|advertising\s+)?alerts?"
         r"|what(?:'s|\s+is|s)\s+wrong\s+with\s+(?:ppc|ads?|advertising))",
         paused_ok=True, help="PPC ALERTS — what's wrong with advertising right now")
def _ppc_alerts(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.alerts_summary(ctx.session))


@command("PPC_BUDGETS",
         r"(?:(?:ppc\s+|ads?\s+|advertising\s+)?budgets?(?:\s+now|\s+today)?|budget\s+pace)",
         paused_ok=True, help="PPC BUDGETS — how much of today's budget each campaign has spent")
def _ppc_budgets(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.budgets_now(ctx.session))


@command("PPC_HOURS",
         # "hours" on its own is too ordinary a word to claim; it needs a subject in front of it.
         r"(?:(?:ppc|ads?|advertising)\s+(?:hours?|dayparting|by\s+hour)|dayparting|hourly"
         r"|(?:the\s+)?shape\s+of\s+the\s+day)",
         paused_ok=True, help="PPC HOURS — which hours of the day sell and which only spend")
def _ppc_hours(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.day_shape(ctx.session))


@command("PPC_MUTE_ALERT",
         r"mute\s+(?:the\s+)?(?:alerts?\s+(?:for|about)\s+)?"
         r"(spend\s*spike|budget\s*exhausted\s*early|cpc\s*jump"
         r"|zero\s*sales(?:\s*with\s*spend)?|ad\s*serving\s*problems?)s?",
         paused_ok=True, help="MUTE SPEND SPIKE — stop being told about one kind of alert")
def _ppc_mute_alert(ctx: CommandContext) -> CommandResult:
    from app.ppc import alerts as alerts_module
    from app.ppc import profiles as ppc_profiles

    # The spoken name and the stored type are not the same string: "zero sales" is
    # `zero_sales_with_spend`, and the plural "problems" is one `ad_serving_problem`.
    spoken = " ".join(ctx.match.group(1).lower().split())
    kinds = {"spend spike": "spend_spike", "spendspike": "spend_spike",
             "budget exhausted early": "budget_exhausted_early",
             "budgetexhaustedearly": "budget_exhausted_early",
             "cpc jump": "cpc_jump", "cpcjump": "cpc_jump",
             "zero sales": "zero_sales_with_spend",
             "zero sales with spend": "zero_sales_with_spend",
             "zerosales": "zero_sales_with_spend",
             "ad serving problem": "ad_serving_problem",
             "ad serving problems": "ad_serving_problem",
             "adservingproblem": "ad_serving_problem"}
    kind = kinds.get(spoken)
    if kind is None:
        return CommandResult(f"I don't have an alert called \"{spoken}\". The ones I raise are: "
                             "spend spike, cpc jump, zero sales, budget exhausted early, "
                             "ad serving problem.")
    profile = ppc_profiles.require(ctx.session)
    rows = [row for row in alerts_module.open_alerts(ctx.session, profile) if row.type == kind]
    for row in rows:
        row.status = "muted"
    # No commit here. Every other handler in this file leaves that to the dispatcher's session
    # scope, and committing mid-handler splits the change from its audit record and its reply: if
    # the reply then failed, the mute would be in the database, the job would retry, and the retry
    # would find nothing open and answer "nothing of that kind to mute".
    if not rows:
        return CommandResult(f"Nothing open of that kind, so there was nothing to mute. If a "
                             f"{kind.replace('_', ' ')} comes up later I'll still tell you about it.")
    return CommandResult(f"Muted {len(rows)} {kind.replace('_', ' ')} alert(s). They stay open in "
                         "`PPC ALERTS` and I'll stop messaging about them.")


@command("APPROVE_MALFORMED", r"(?:approve|reject|aprove|approove)\b.*", paused_ok=True, help="")
def _approve_malformed(ctx: CommandContext) -> CommandResult:
    """Anything starting with "approve" that the strict pattern didn't take.

    It must never reach the model, which has no way to approve anything and would only narrate it.
    """
    from app.services.summaries import pending_approvals

    pending = pending_approvals(ctx.session)
    if not pending:
        return CommandResult("Nothing is waiting for approval right now.")
    a = pending[0]
    needs = " <PIN>" if a.risk_level == "critical" else ""
    if (a.action_payload or {}).get("confirm_text"):
        needs += f" {a.action_payload['confirm_text']}"
    refs = ", ".join(x.ref for x in pending[:5])
    return CommandResult(f"⚠️ Nothing was approved — I couldn't read that as an approval.\n"
                         f"Send it exactly like this:  APPROVE {a.ref} {a.confirmation_code}{needs}\n"
                         f"(waiting: {refs})")


# ---------------- email drafting and sending (Phase 2) ----------------
@command("DRAFT", r"(?:draft|reply)(?: to)?\s+(E-?\d+)\s*[:\-,]?\s+(.+)", paused_ok=False,
         help="DRAFT E-12 <what to say> — write a reply draft (never sent)")
def _draft(ctx: CommandContext) -> CommandResult:
    from app.email import drafting, threads

    thread = threads.get_by_ref(ctx.session, ctx.match.group(1))
    return CommandResult(drafting.request_draft(ctx.session, ctx.user, thread, ctx.match.group(2)))


_ADDR = r"[^\s,;:]+@[^\s,;:]+\.[^\s,;:]+"


@command("NEW_EMAIL", rf"(?:new\s+email|email|write)\s+(?:to\s+)?({_ADDR}(?:\s*(?:,|;|and)\s*{_ADDR})*)\s*[:\-,]?\s+(.+)",
         paused_ok=False, help="NEW EMAIL sam@x.com <what to say> — write a new email draft (never sent)")
def _new_email(ctx: CommandContext) -> CommandResult:
    from app.email import drafting

    to = re.findall(_ADDR, ctx.match.group(1))
    return CommandResult(drafting.request_new_email(ctx.session, ctx.user, to=to, cc=None, subject=None,
                                                    instructions=ctx.match.group(2), owner_text=ctx.message.body))


@command("ATTACH", r"attach(?:\s+(?:this|that|it|the\s+file))?(?:\s+to)?\s+(D-?\d+)", paused_ok=False,
         help="ATTACH D-3 — attach the file you just sent me")
def _attach(ctx: CommandContext) -> CommandResult:
    from app.email import drafting

    file = drafting.recent_whatsapp_file(ctx.session, ctx.user, ctx.message)
    return CommandResult(drafting.attach_file(ctx.session, ctx.user, ctx.match.group(1), file))


@command("DETACH", r"(?:detach|remove\s+attachment|remove\s+file)(?:\s+from)?\s+(D-?\d+)(?:\s+(.+))?",
         paused_ok=False, help="DETACH D-3 [name or number] — remove attachments from a draft")
def _detach(ctx: CommandContext) -> CommandResult:
    from app.email import drafting

    return CommandResult(drafting.detach_file(ctx.session, ctx.user, ctx.match.group(1), ctx.match.group(2)))


@command("EDIT", r"edit\s+(D-?\d+)\s*[:\-,]?\s+(.+)", paused_ok=False, help="EDIT D-3 <changes> — revise a draft")
def _edit(ctx: CommandContext) -> CommandResult:
    from app.email import drafting

    return CommandResult(drafting.request_edit(ctx.session, ctx.user, ctx.match.group(1), ctx.match.group(2)))


@command("SEND", r"send\s+(D-?\d+)", paused_ok=False, help="SEND D-3 — ask to send a draft (needs APPROVE)")
def _send(ctx: CommandContext) -> CommandResult:
    from app.email import drafting

    return CommandResult(drafting.request_send(ctx.session, ctx.user, ctx.match.group(1)))


@command("DISCARD", r"discard\s+(D-?\d+)", paused_ok=True, help="DISCARD D-3 — throw a draft away")
def _discard(ctx: CommandContext) -> CommandResult:
    from app.email import drafting

    return CommandResult(drafting.discard(ctx.session, ctx.user, ctx.match.group(1)))


# ---------------- email views and thread actions (Phase 2) ----------------
def _restart_email(session: Session) -> None:
    from sqlalchemy import select

    from app.db.models import EmailAccount
    from app.email.sync import enqueue_sync, enqueue_triage

    for account in session.execute(select(EmailAccount).where(EmailAccount.status == "active")).scalars():
        enqueue_sync(session, account.id)
    enqueue_triage(session)


@command("EMAILS", r"(?:emails|inbox)", paused_ok=True, help="EMAILS — emails that need you")
def _emails(ctx: CommandContext) -> CommandResult:
    from app.email import views

    return CommandResult(views.emails_command(ctx.session))


@command("EMAIL_THREAD", r"(E-?\d+)", paused_ok=True, help="E-12 — details of an email thread")
def _email_thread(ctx: CommandContext) -> CommandResult:
    from app.email import threads, views

    return CommandResult(views.thread_detail(ctx.session, threads.get_by_ref(ctx.session, ctx.match.group(1))))


@command("WAITING", r"waiting", paused_ok=True, help="WAITING — emails waiting on other people")
def _waiting(ctx: CommandContext) -> CommandResult:
    from app.email import views

    return CommandResult(views.waiting_command(ctx.session))


@command("DRAFTS", r"drafts", paused_ok=True, help="DRAFTS — open email drafts")
def _drafts(ctx: CommandContext) -> CommandResult:
    from app.email import views

    return CommandResult(views.drafts_command(ctx.session))


@command("DRAFT_DETAIL", r"(D-?\d+)", paused_ok=True, help="D-3 — show a draft")
def _draft_detail(ctx: CommandContext) -> CommandResult:
    from app.email import drafts, views

    return CommandResult(views.draft_detail(ctx.session, drafts.get_by_ref(ctx.session, ctx.match.group(1))))


def _thread_status(ctx: CommandContext, status: str, word: str) -> CommandResult:
    from app.email import threads

    thread = threads.get_by_ref(ctx.session, ctx.match.group(1))
    threads.set_status(ctx.session, thread, status, Actor.user(ctx.user.id))
    return CommandResult(f"👍 {thread.ref} marked {word}.")


@command("HANDLED", r"handled\s+(E-?\d+)", paused_ok=True, help="HANDLED E-12 — mark an email handled")
def _handled(ctx: CommandContext) -> CommandResult:
    return _thread_status(ctx, "handled", "handled")


@command("IGNORE", r"ignore\s+(E-?\d+)", paused_ok=True, help="IGNORE E-12 — ignore an email thread")
def _ignore(ctx: CommandContext) -> CommandResult:
    return _thread_status(ctx, "ignored", "ignored")


# ---------- Amazon (Phase 3) ----------
@command("LISTINGS", r"listings?", paused_ok=True, help="LISTINGS — your Amazon listings and their health")
def _listings(ctx: CommandContext) -> CommandResult:
    from app.amazon import views

    return CommandResult(views.listings_command(ctx.session))


@command("LISTING_DETAIL", r"(L-?\d+)", paused_ok=True, help="L-4 — one listing in detail")
def _listing_detail(ctx: CommandContext) -> CommandResult:
    from app.amazon import listings, views

    listing = listings.get_by_ref(ctx.session, ctx.match.group(1))
    return CommandResult(views.listing_detail(ctx.session, listing))


@command("SKU_LOOKUP", r"sku\s+([\w.\-/]+)", paused_ok=True, help="SKU <sku> — find a listing by SKU")
def _sku_lookup(ctx: CommandContext) -> CommandResult:
    from app.amazon import listings, views

    return CommandResult(_one_listing(ctx, listings.find(ctx.session, sku=ctx.match.group(1)),
                                      f"SKU {ctx.match.group(1)}", views))


@command("ASIN_LOOKUP", r"asin\s+([A-Z0-9]{10})", paused_ok=True, help="ASIN <asin> — find a listing by ASIN")
def _asin_lookup(ctx: CommandContext) -> CommandResult:
    from app.amazon import listings, views

    return CommandResult(_one_listing(ctx, listings.find(ctx.session, asin=ctx.match.group(1)),
                                      f"ASIN {ctx.match.group(1)}", views))


def _one_listing(ctx: CommandContext, rows, what: str, views) -> str:
    if not rows:
        return f"I have no listing for {what}. Reply LISTINGS to see what I do have."
    if len(rows) == 1:
        return views.listing_detail(ctx.session, rows[0])
    from app.amazon import marketplaces as mkt

    lines = [f"{what} exists in {len(rows)} marketplaces:"]
    lines += [f"• {r.ref} — {mkt.by_id(r.marketplace_id).code}" for r in rows]
    return "\n".join(lines) + "\nReply with a ref for details."


@command("STOCK", r"stock", paused_ok=True, help="STOCK — FBA stock, lowest cover first")
def _stock(ctx: CommandContext) -> CommandResult:
    from app.amazon import views

    return CommandResult(views.stock_command(ctx.session))


@command("VERSIONS", r"versions?\s+(L-?\d+)", paused_ok=True, help="VERSIONS L-4 — listing history")
def _versions(ctx: CommandContext) -> CommandResult:
    from app.amazon import listings, views

    listing = listings.get_by_ref(ctx.session, ctx.match.group(1))
    return CommandResult(views.versions_command(ctx.session, listing))


@command("CHANGES", r"changes", paused_ok=True, help="CHANGES — open listing changes")
def _changes(ctx: CommandContext) -> CommandResult:
    from app.amazon import views

    return CommandResult(views.changes_command(ctx.session))


@command("CHANGE_DETAIL", r"(LC-?\d+)(\s+details)?", paused_ok=True, help="LC-3 [DETAILS] — one change")
def _change_detail(ctx: CommandContext) -> CommandResult:
    from app.amazon import changes, views

    request = changes.get_by_ref(ctx.session, ctx.match.group(1))
    return CommandResult(views.change_detail(ctx.session, request, full=bool(ctx.match.group(2))))


@command("SUBMIT_CHANGE", r"submit\s+(LC-?\d+)", paused_ok=False,
         help="SUBMIT LC-3 — ask for approval to send a change to Amazon")
def _submit_change(ctx: CommandContext) -> CommandResult:
    from app.amazon import changes

    request = changes.get_by_ref(ctx.session, ctx.match.group(1))
    return CommandResult(changes.request_approval(ctx.session, request, ctx.user))


@command("CANCEL_CHANGE", r"cancel\s+(LC-?\d+)", paused_ok=True, help="CANCEL LC-3 — drop a change")
def _cancel_change(ctx: CommandContext) -> CommandResult:
    from app.amazon import changes

    request = changes.get_by_ref(ctx.session, ctx.match.group(1))
    return CommandResult(changes.cancel(ctx.session, request, ctx.user))


@command("REVERT_LISTING", r"revert\s+(L-?\d+)\s+v?(\d+)", paused_ok=False,
         help="REVERT L-4 V7 — go back to an earlier version")
def _revert_listing(ctx: CommandContext) -> CommandResult:
    from app.amazon import changes, listings, views

    listing = listings.get_by_ref(ctx.session, ctx.match.group(1))
    request = changes.build_revert(ctx.session, listing, int(ctx.match.group(2)), ctx.user)
    detail = views.change_detail(ctx.session, request)
    if request.status == "ready_for_approval":
        return CommandResult(detail + "\n\n" + changes.request_approval(ctx.session, request, ctx.user))
    return CommandResult(detail)


@command("COPY_LISTING", r"copy\s+(L-?\d+)\s+to\s+(CA|US)(?:\s*(?:at|for)?\s*\$?([\d.]+))?", paused_ok=False,
         help="COPY L-4 TO CA 32.99 — copy a listing to another marketplace")
def _copy_listing(ctx: CommandContext) -> CommandResult:
    from app.amazon import canada, changes, listings, views

    listing = listings.get_by_ref(ctx.session, ctx.match.group(1))
    price = float(ctx.match.group(3)) if ctx.match.group(3) else None
    request = canada.copy_listing(ctx.session, listing, target_code=ctx.match.group(2), price=price,
                                  source_message_id=ctx.message.id)
    lines = [canada.copy_summary(ctx.session, request, listing, ctx.match.group(2)),
             views.change_detail(ctx.session, request)]
    if request.status == "ready_for_approval":
        lines.append(changes.request_approval(ctx.session, request, ctx.user))
    return CommandResult("\n".join(lines))


@command("ENABLE_MARKETPLACE", r"enable\s+(?:amazon\s+)?(canada|ca|united states|us)", paused_ok=False,
         help="ENABLE CANADA — allow changes in another marketplace")
def _enable_marketplace(ctx: CommandContext) -> CommandResult:
    from app.amazon import canada

    code = "CA" if ctx.match.group(1).lower() in ("canada", "ca") else "US"
    codes = sorted(set(canada.enabled_codes(ctx.session)) | {code})
    return CommandResult(canada.set_enabled(ctx.session, codes, Actor.user(ctx.user.id)))


@command("ISSUES", r"issues", paused_ok=True, help="ISSUES — listings Amazon is unhappy with")
def _issues(ctx: CommandContext) -> CommandResult:
    from app.amazon import monitor

    return CommandResult(monitor.issues_command(ctx.session))


@command("SALES", r"sales", paused_ok=True, help="SALES — yesterday and the last 7 days")
def _sales(ctx: CommandContext) -> CommandResult:
    from app.amazon import monitor

    return CommandResult(monitor.sales_command(ctx.session))


@command("SHIPMENTS", r"shipments?", paused_ok=True, help="SHIPMENTS — open FBA shipments")
def _shipments(ctx: CommandContext) -> CommandResult:
    from app.amazon import fba_plan

    return CommandResult(fba_plan.list_message(ctx.session))


@command("SHIPMENT_DETAIL", r"(FB-?\d+)", paused_ok=True, help="FB-2 — one shipment in detail")
def _shipment_detail(ctx: CommandContext) -> CommandResult:
    from app.amazon import fba_plan

    request = fba_plan.get_by_ref(ctx.session, ctx.match.group(1))
    return CommandResult(fba_plan.detail_message(ctx.session, request))


@command("SHIP_OPTION", r"ship\s+(FB-?\d+)\s+option\s+(\d+)(?:\s+(partnered|own\s*carrier))?", paused_ok=False,
         help="SHIP FB-2 OPTION 1 PARTNERED — pick a warehouse option")
def _ship_option(ctx: CommandContext) -> CommandResult:
    from app.amazon import fba_confirm, fba_plan

    request = fba_plan.get_by_ref(ctx.session, ctx.match.group(1))
    carrier = (ctx.match.group(3) or "").replace(" ", "").lower() or None
    return CommandResult(fba_confirm.choose_option(ctx.session, request, int(ctx.match.group(2)),
                                                   carrier_choice=carrier, user=ctx.user))


@command("OPTION_ANSWER",
         r"(?:take\s+|pick\s+|go\s+with\s+)?(?:option\s*(\d{1,2})(?:\s+(own\s*carrier|partnered))?"
         r"|(\d{1,2})\s+(own\s*carrier|partnered))",
         paused_ok=False, help="option 1 own carrier — pick a warehouse option")
def _option_answer(ctx: CommandContext) -> CommandResult:
    """"option 2" is an answer to the options I just sent; no need to retype the whole command."""
    from app.amazon import fba_confirm, fba_plan

    number = int(ctx.match.group(1) or ctx.match.group(3))
    carrier = ctx.match.group(2) or ctx.match.group(4)
    request = fba_plan.awaiting_option(ctx.session)
    if request is None:
        return CommandResult("No shipment is waiting on an option right now. Send SHIPMENTS to see what's open.")
    return CommandResult(fba_confirm.choose_option(ctx.session, request, number,
                                                   carrier_choice=carrier, user=ctx.user))


@command("CARRIER_ANSWER", r"(own\s*carrier|partnered|amazon'?s?\s*carrier)", paused_ok=False, help="")
def _carrier_answer(ctx: CommandContext) -> CommandResult:
    """The whole reply is "own carrier" — an answer to the question we just asked."""
    from app.amazon import fba_plan

    reply = fba_plan._carrier_answer(ctx.session, user=ctx.user, answers=ctx.match.group(1))
    return CommandResult(reply or "No shipment is waiting on a carrier choice. Send SHIPMENTS to see what's open.")


@command("SHIP_CARRIER", r"ship\s+(FB-?\d+)\s+(?:carrier|partnered|own\s*carrier)(?:\s+(.+))?", paused_ok=False,
         help="SHIP FB-2 CARRIER [name] — approve the carrier, naming it when you book it yourself")
def _ship_carrier(ctx: CommandContext) -> CommandResult:
    from app.amazon import fba_confirm, fba_plan

    request = fba_plan.get_by_ref(ctx.session, ctx.match.group(1))
    return CommandResult(fba_confirm.choose_carrier(ctx.session, request, user=ctx.user,
                                                    carrier=ctx.match.group(2)))


@command("SHIPMENT_FORM",
         r"(?:(?:new|create(?:\s+an?)?)\s+(?:fba\s+)?shipment|(?:fba|shipment)\s+form)",
         paused_ok=False, help="NEW SHIPMENT — get the form to fill in")
def _shipment_form(ctx: CommandContext) -> CommandResult:
    from app.amazon import fba_form

    return CommandResult(fba_form.FORM)


@command("READY_DATE", r"(?:(?:ready|ship\s*date)\s+(FB-?\d+)|(FB-?\d+)\s+ready)\s+(?:on\s+)?(.+)",
         paused_ok=False, help="READY FB-2 Monday — change when the boxes leave")
def _ready_date(ctx: CommandContext) -> CommandResult:
    from app.amazon import fba_plan

    ref = ctx.match.group(1) or ctx.match.group(2)
    request = fba_plan.get_by_ref(ctx.session, ref)
    return CommandResult(fba_plan.set_ready_date(ctx.session, request, ctx.match.group(3)))


@command("TRACKING", r"tracking\s+(FB-?\d+)\s+(.+)", paused_ok=False,
         help="TRACKING FB-2 1Z… — send your carrier's tracking numbers")
def _tracking(ctx: CommandContext) -> CommandResult:
    from app.amazon import fba_confirm, fba_plan

    request = fba_plan.get_by_ref(ctx.session, ctx.match.group(1))
    numbers = [n for n in re.split(r"[\s,;]+", ctx.match.group(2).strip()) if len(n) >= 6]
    if not numbers:
        return CommandResult("Send the tracking numbers after the ref, e.g. \"TRACKING FB-2 1Z999 1Z998\".")
    return CommandResult(fba_confirm.request_tracking(ctx.session, request, numbers, user=ctx.user))


@command("LABELS", r"labels?\s+(FB-?\d+)", paused_ok=False, help="LABELS FB-2 — resend the labels")
def _labels(ctx: CommandContext) -> CommandResult:
    from app.amazon import fba_plan
    from app.jobs import queue

    request = fba_plan.get_by_ref(ctx.session, ctx.match.group(1))
    if request.status not in ("confirmed", "in_transit", "receiving"):
        return CommandResult(f"{request.ref} isn't confirmed yet, so there are no labels.")
    queue.enqueue(ctx.session, "fba_fetch_labels", {"request_ref": request.ref}, priority=15)
    return CommandResult(f"🏷️ Fetching the {request.ref} labels again now.")


@command("CANCEL_SHIPMENT", r"cancel\s+(FB-?\d+)", paused_ok=True, help="CANCEL FB-2 — drop a shipment")
def _cancel_shipment(ctx: CommandContext) -> CommandResult:
    from app.amazon import fba_plan

    from app.amazon import fba_confirm

    request = fba_plan.get_by_ref(ctx.session, ctx.match.group(1))
    if request.status in ("confirmed", "in_transit", "receiving"):
        return CommandResult(fba_confirm.request_cancel_confirmed(ctx.session, request, user=ctx.user))
    return CommandResult(fba_plan.cancel_plan(ctx.session, request, reason="you cancelled it"))


@command("AMAZON_WRITE_MODE", r"amazon\s+(live|validation[\s-]?only|write\s*mode|mode)", paused_ok=True,
         help="AMAZON LIVE / AMAZON VALIDATION ONLY — submit approved changes, or only check them")
def _amazon_write_mode(ctx: CommandContext) -> CommandResult:
    from app.amazon import mode

    what = ctx.match.group(1).lower()
    if what.startswith("live"):
        return CommandResult(mode.request_live(ctx.session, ctx.user))
    if what.startswith("validation"):
        return CommandResult(mode.go_validation_only(ctx.session, ctx.user))
    return CommandResult(mode.describe(ctx.session))


@command("AMAZON_PAUSE", r"amazon\s+pause", paused_ok=True,
         help="AMAZON PAUSE / AMAZON RESUME — pause Amazon syncing and writes")
def _amazon_pause(ctx: CommandContext) -> CommandResult:
    settings_store.set_value(ctx.session, "amazon_paused", True, Actor.user(ctx.user.id))
    return CommandResult("⏸️ Amazon paused: no syncing, no alerts and no writes, including approved ones "
                         "that haven't run yet. Send AMAZON RESUME to continue.")


@command("AMAZON_RESUME", r"amazon\s+resume", paused_ok=True, help="")
def _amazon_resume(ctx: CommandContext) -> CommandResult:
    from app.jobs import queue

    settings_store.set_value(ctx.session, "amazon_paused", False, Actor.user(ctx.user.id))
    queue.enqueue(ctx.session, "amazon_sync_listings", {}, idempotency_key="amazon_sync_listings")
    return CommandResult("▶️ Amazon resumed. Re-reading your listings now.")


@command("SYNC_AMAZON", r"sync\s+amazon", paused_ok=False, help="SYNC AMAZON — re-read listings now")
def _sync_amazon(ctx: CommandContext) -> CommandResult:
    from app.jobs import queue

    queue.enqueue(ctx.session, "amazon_sync_listings", {}, idempotency_key="amazon_sync_listings")
    queue.enqueue(ctx.session, "amazon_inventory_snapshot", {}, idempotency_key="amazon_inventory_snapshot")
    return CommandResult("🔄 Re-reading your Amazon listings and stock. Reply LISTINGS in a minute.")


@command("RULES", r"(?:email\s+)?rules", paused_ok=True, help="RULES — your email alert rules")
def _rules(ctx: CommandContext) -> CommandResult:
    from app.email import views

    return CommandResult(views.rules_command(ctx.session))


@command("REMOVE_RULE", r"(?:remove|delete)\s+rule\s+#?(\d+)", paused_ok=True,
         help="REMOVE RULE 2 — delete one of your email rules")
def _remove_rule(ctx: CommandContext) -> CommandResult:
    from app.email import rules

    n = int(ctx.match.group(1))
    mine = rules.owner_rules(ctx.session)
    if not 1 <= n <= len(mine):
        return CommandResult(f"There's no rule {n}. Send RULES to see the list.")
    rule = mine[n - 1]
    rules.remove_owner_rule(ctx.session, rule, Actor.user(ctx.user.id))
    return CommandResult(f"🗑️ Removed rule {n}: {rules.describe(rule)}.")


@command("EMAIL_PAUSE", r"email\s+pause", paused_ok=True, help="EMAIL PAUSE / EMAIL RESUME — pause email only")
def _email_pause(ctx: CommandContext) -> CommandResult:
    settings_store.set_value(ctx.session, "email_paused", True, Actor.user(ctx.user.id))
    return CommandResult("⏸️ Email paused: no syncing, alerts, drafting or sending. Mail isn't lost; "
                         "send EMAIL RESUME to catch up.")


@command("EMAIL_RESUME", r"email\s+resume", paused_ok=True, help="")
def _email_resume(ctx: CommandContext) -> CommandResult:
    settings_store.set_value(ctx.session, "email_paused", False, Actor.user(ctx.user.id))
    _restart_email(ctx.session)
    return CommandResult("▶️ Email resumed. Catching up on new mail now.")


# ---------- Amazon Ads / PPC (Phase 4) ----------
# The specific patterns come first: COMMANDS is matched in order, so a bare `PPC` must not swallow
# `PPC ASIN B0…`.
@command("PPC_ASIN", r"ppc\s+(?:asin\s+([A-Z0-9]{10})|(B0[A-Z0-9]{8}))", paused_ok=True,
         help="PPC ASIN <asin> — one product's advertising against what it can afford")
def _ppc_asin(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    # Either spelling: "PPC ASIN <anything ten characters>" or just "PPC B0…".
    return CommandResult(views.asin_summary(ctx.session, ctx.match.group(1) or ctx.match.group(2)))


@command("PPC_BRANDS", r"ppc\s+brands?", paused_ok=True, help="PPC BRANDS — advertising by brand")
def _ppc_brands(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.brands_summary(ctx.session))


@command("PPC_ECONOMICS", r"ppc\s+(?:economics|margins?|breakeven|break[\s-]?even)", paused_ok=True,
         help="PPC ECONOMICS — margins and break-even ACOS per product")
def _ppc_economics(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.economics_summary(ctx.session))


@command("PPC_FINDINGS", r"ppc\s+(?:findings?|analys[ei]s|analyse|analyze|ideas?)", paused_ok=True,
         help="PPC FINDINGS — what I'd change and why, without changing anything")
def _ppc_findings(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.findings_summary(ctx.session))


@command("PPC_OUTCOMES", r"(?:(?:ppc\s+)?outcomes?|ppc\s+results?)", paused_ok=True,
         help="OUTCOMES — what the changes I made actually did")
def _ppc_outcomes(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.outcomes_summary(ctx.session))


@command("PPC_CALIBRATION", r"(?:(?:ppc\s+)?calibration|how\s+good\s+are\s+your\s+predictions)",
         paused_ok=True, help="CALIBRATION — how often my predictions come true")
def _ppc_calibration(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.calibration_summary(ctx.session))


@command("PPC_DEEP", r"ppc\s+(?:deep|deep\s+dive|everything)\s+(?:on\s+)?([A-Z0-9]{10})",
         paused_ok=True, help="PPC DEEP <asin> — one product from every angle")
def _ppc_deep(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.asin_deep_dive(ctx.session, ctx.match.group(1)))


@command("PPC_RESEARCH", r"(?:(?:ppc\s+)?research|what\s+have\s+you\s+read)", paused_ok=True,
         help="RESEARCH — claims I've read, and what I've tested")
def _ppc_research(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.research_summary(ctx.session))


@command("PPC_REPORT", r"(?:(?:ppc\s+)?(?:weekly\s+)?report|ppc\s+weekly)", paused_ok=True,
         help="PPC REPORT — the weekly strategy report, on demand")
def _ppc_report(ctx: CommandContext) -> CommandResult:
    from app.ppc import profiles, report

    profile = profiles.require(ctx.session)
    return CommandResult(report.weekly(ctx.session, profile))


@command("PPC_PAUSE", r"ppc\s+pause", paused_ok=True,
         help="PPC PAUSE / PPC RESUME — stop or restart advertising data and analysis")
def _ppc_pause(ctx: CommandContext) -> CommandResult:
    settings_store.set_value(ctx.session, "ppc_paused", True, Actor.user(ctx.user.id))
    return CommandResult("⏸️ Advertising paused: no data collection, no analysis, no "
                         "recommendations. Your campaigns keep running at Amazon exactly as they "
                         "are — this only stops me. Send PPC RESUME to start again.")


@command("PPC_RESUME", r"ppc\s+resume", paused_ok=True, help="")
def _ppc_resume(ctx: CommandContext) -> CommandResult:
    settings_store.set_value(ctx.session, "ppc_paused", False, Actor.user(ctx.user.id))
    return CommandResult("▶️ Advertising back on. I'll pick the data up on the next run.")


# Grouped: `ppc|ads|advertising` un-grouped binds the anchors to one alternative each, so
# "ppc please cut the bids on skyzz" matched and was answered with a summary instead of reaching
# the agent. Every alternation in a command pattern needs its own group.
@command("PPC", r"(?:ppc|ads|advertising)", paused_ok=True,
         help="PPC — advertising spend, ACOS and TACOS")
def _ppc(ctx: CommandContext) -> CommandResult:
    from app.ppc import views

    return CommandResult(views.ppc_summary(ctx.session))


@command("PPC_COST", r"cost\s+(B0[A-Z0-9]{8})\s+\$?([\d.,]+)", paused_ok=True,
         help="COST <asin> <amount> — tell me what a unit costs you, so targets can be trusted")
def _ppc_cost(ctx: CommandContext) -> CommandResult:
    from app.ppc import economics

    asin = ctx.match.group(1).upper()
    try:
        amount = float(ctx.match.group(2).replace(",", ""))
    except ValueError:
        return CommandResult(f"I couldn't read {ctx.match.group(2)!r} as an amount.")
    if amount <= 0:
        return CommandResult("A unit cost has to be more than zero.")
    if amount > 100_000:
        return CommandResult(f"{amount:,.2f} doesn't look like a unit cost. If it really is, tell me "
                             "again and I'll take your word for it — but check the decimal point "
                             "first.")
    return CommandResult(economics.set_unit_cost(ctx.session, asin, amount, Actor.user(ctx.user.id)))
