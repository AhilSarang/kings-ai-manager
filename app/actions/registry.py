"""Registry of every action type the system can execute, with its tier.

Unknown action types are rejected. Tier 4 types are registered only so the
system can recognise them and refuse clearly.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.actions.tiers import Tier


class UnknownAction(Exception):
    pass


class ActionNotPermitted(Exception):
    pass


@dataclass(frozen=True)
class ActionSpec:
    type: str
    tier: Tier
    description: str
    min_risk: str = "low"


ACTIONS: dict[str, ActionSpec] = {}


def register(type: str, tier: Tier, description: str, min_risk: str = "low") -> ActionSpec:
    spec = ActionSpec(type, tier, description, min_risk)
    ACTIONS[type] = spec
    return spec


def get_spec(action_type: str) -> ActionSpec:
    try:
        return ACTIONS[action_type]
    except KeyError:
        raise UnknownAction(f"Unknown action type '{action_type}'") from None


def check_permitted(action_type: str, agent_permissions: dict | None) -> ActionSpec:
    """Raise unless the agent may perform (or request) this action."""
    spec = get_spec(action_type)
    if spec.tier == Tier.PROHIBITED:
        raise ActionNotPermitted(f"'{action_type}' is never done by agents. Please do this yourself.")
    max_tier = int((agent_permissions or {}).get("max_tier", 0))
    if spec.tier > max_tier:
        raise ActionNotPermitted(f"This agent may not perform '{action_type}' (tier {int(spec.tier)})")
    return spec


# ---- Supervisor actions (Tier 0/1) ----
for _t, _tier, _d in [
    ("create_task", Tier.INTERNAL, "Create a task"),
    ("update_task", Tier.INTERNAL, "Update a task"),
    ("cancel_task", Tier.INTERNAL, "Cancel a task"),
    ("get_status", Tier.READ, "Report task status"),
    ("list_tasks", Tier.READ, "List tasks"),
    ("answer_question", Tier.READ, "Answer a question"),
    ("remember", Tier.INTERNAL, "Save company memory"),
    ("request_clarification", Tier.READ, "Ask the owner a question"),
    ("request_report", Tier.READ, "Produce a text report"),
    ("no_op", Tier.READ, "Nothing to do"),
    ("draft_text", Tier.INTERNAL, "Draft text for the owner (never sent)"),
]:
    register(_t, _tier, _d)

# ---- Email agent (Phase 2) ----
for _t, _tier, _d in [
    ("email_create_draft", Tier.INTERNAL, "Create an email draft (never sent)"),
    ("email_draft_request", Tier.INTERNAL, "Ask the Email agent for a reply draft"),
    ("email_new_draft", Tier.INTERNAL, "Ask the Email agent to write a new email (draft only)"),
    ("email_edit_draft", Tier.INTERNAL, "Revise an email draft"),
    ("email_attach_file", Tier.INTERNAL, "Attach a file the owner sent, or remove one, from a draft"),
    ("email_search", Tier.READ, "Search stored email"),
    ("email_summary", Tier.READ, "Summarise recent email"),
    ("email_set_followup", Tier.INTERNAL, "Change follow-up tracking for a thread"),
    ("email_update_thread_status", Tier.INTERNAL, "Change an email thread's status"),
    ("email_set_contact_type", Tier.INTERNAL, "Set an email contact's type or VIP flag"),
    ("email_add_alert_rule", Tier.INTERNAL, "Add or change an email alert rule"),
    ("email_remove_alert_rule", Tier.INTERNAL, "Remove one of the owner's email alert rules"),
    ("email_list_alert_rules", Tier.READ, "List email alert rules"),
]:
    register(_t, _tier, _d)
register("email_send", Tier.EXTERNAL, "Send an email (approval every time)", min_risk="medium")

# ---- Amazon agent (Phase 3) ----
for _t, _tier, _d in [
    # Tier 0: reads. Nothing on Amazon changes.
    ("amazon_read_listing", Tier.READ, "Read a listing from Amazon"),
    ("amazon_search_catalog", Tier.READ, "Search the Amazon catalogue"),
    ("amazon_fetch_schema", Tier.READ, "Fetch a product type schema"),
    ("amazon_validation_preview", Tier.READ, "Ask Amazon to validate a change without submitting it"),
    ("amazon_query", Tier.READ, "Answer a question from stored Amazon data"),
    ("amazon_edit_listing", Tier.INTERNAL, "Draft a listing edit for approval"),
    ("amazon_create_listing", Tier.INTERNAL, "Draft a new listing for approval"),
    ("amazon_create_variations", Tier.INTERNAL, "Draft a variation family for approval"),
    ("amazon_copy_marketplace", Tier.INTERNAL, "Draft a copy of a listing in another marketplace"),
    ("amazon_modify_variations", Tier.INTERNAL, "Draft adding or removing a variation child"),
    ("fba_list_options", Tier.READ, "List packing, placement or transportation options"),
    # Tier 1: build proposals and drafts locally.
    ("amazon_build_change_request", Tier.INTERNAL, "Draft a listing change for approval"),
    ("amazon_import_images", Tier.INTERNAL, "Import and check images from Drive"),
    ("amazon_provide_info", Tier.INTERNAL, "Answer the agent's questions about a draft"),
    ("amazon_set_setting", Tier.INTERNAL, "Change an Amazon agent setting"),
    ("amazon_set_product_cost", Tier.INTERNAL, "Record what a product costs us"),
    ("fba_create_shipment", Tier.INTERNAL, "Start an FBA shipment request from the owner's message"),
    ("fba_provide_info", Tier.INTERNAL, "Answer missing shipment details (weights, contents, prep)"),
    ("fba_set_case_pack", Tier.INTERNAL, "Save a product's case pack"),
    ("fba_set_ship_from", Tier.INTERNAL, "Set or change a ship-from address"),
    ("fba_set_ready_date", Tier.INTERNAL, "Change when a shipment's boxes leave"),
    ("fba_create_inbound_plan", Tier.INTERNAL, "Create a draft plan in Amazon to see options (no fees)"),
    ("fba_set_packing_information", Tier.INTERNAL, "Send box details to Amazon for the draft plan"),
    ("fba_generate_options", Tier.INTERNAL, "Ask Amazon for placement and transport options"),
]:
    register(_t, _tier, _d)

# Tier 3: every write to Amazon. Approval always; PIN for the critical ones (Section 5).
for _t, _d in [
    ("amazon_patch_attributes", "Edit listing attributes"),
    ("amazon_create_offer_existing_asin", "Add our offer to an existing ASIN"),
    ("amazon_update_price", "Change a price"),
    ("amazon_add_variation_child", "Add a child to a variation family"),
    ("amazon_revert", "Revert a listing to an earlier version"),
]:
    register(_t, Tier.EXTERNAL, _d, min_risk="high")
for _t, _d in [
    ("amazon_create_new_asin", "Create a new ASIN"),
    ("amazon_create_variation_family", "Create a variation family"),
    ("amazon_remove_variation_child", "Remove a child from a variation family"),
    ("amazon_full_replace", "Replace every attribute on a listing"),
    ("amazon_change_brand_or_product_type", "Change a listing's brand or product type"),
    ("amazon_delete_listing", "Delete a listing (PIN and SKU typed back)"),
]:
    register(_t, Tier.EXTERNAL, _d, min_risk="critical")
register("fba_confirm_shipment", Tier.EXTERNAL, "Confirm the warehouse placement and its fee", min_risk="high")
register("fba_confirm_transport", Tier.EXTERNAL, "Confirm the carrier and its cost", min_risk="high")
register("fba_update_tracking", Tier.EXTERNAL, "Send own-carrier tracking numbers to Amazon", min_risk="medium")
register("fba_cancel_confirmed_plan", Tier.EXTERNAL, "Cancel a confirmed shipment", min_risk="critical")
register("amazon_set_write_mode", Tier.EXTERNAL, "Switch Amazon writes from validation only to live",
         min_risk="critical")

# ---- PPC agent (Phase 4) ----
for _t, _tier, _d in [
    # Tier 0/1: read, compute, propose. Nothing reaches Amazon Ads.
    ("ppc_query", Tier.READ, "Answer a question from stored PPC data"),
    ("ppc_analyse_asin", Tier.READ, "Deep analysis of one ASIN's advertising"),
    ("ppc_read_reports", Tier.READ, "Pull Amazon Ads reports"),
    ("ppc_build_recommendations", Tier.INTERNAL, "Produce recommendations for approval"),
    ("ppc_design_experiment", Tier.INTERNAL, "Design an experiment for approval"),
    # Safety actions: never blocked behind an approval, always logged and reported.
    ("ppc_stop_experiment", Tier.INTERNAL, "Abort a running experiment"),
    ("ppc_rollback_action", Tier.INTERNAL, "Undo an executed PPC change"),
]:
    register(_t, _tier, _d)
# Tier 2: shapes what the agent will recommend in future, so the owner approves it.
register("ppc_activate_playbook_rule", Tier.EXTERNAL, "Make a learned rule active", min_risk="medium")
register("ppc_retire_playbook_rule", Tier.EXTERNAL, "Retire a learned rule", min_risk="medium")
# Tier 3: every write to Amazon Ads. Approval always; PIN for the big ones (brief Section 12).
register("ppc_apply_change", Tier.EXTERNAL, "Apply an approved change to Amazon Ads", min_risk="high")
register("ppc_start_experiment", Tier.EXTERNAL, "Start an experiment with spend at risk", min_risk="high")
register("ppc_set_write_mode", Tier.EXTERNAL, "Switch PPC from read only to live", min_risk="critical")
# the owner's own constraints. There is deliberately no agent path to these.
register("ppc_set_rule", Tier.PROHIBITED, "the owner's PPC constraints — owner only, never an agent")

# ---- Need approval (Tier 2/3). Executors are added by later phases. ----
register("demo_external_action", Tier.EXTERNAL, "Phase 1 demo: only logs", min_risk="low")
for _t, _d in [("send_email", "Send an email"), ("message_supplier", "Message a supplier")]:
    register(_t, Tier.EXTERNAL, _d, min_risk="medium")
for _t, _d in [("ppc_budget_change", "Change ad spend"), ("price_change", "Change a price"),
               ("listing_edit", "Edit a listing"), ("purchase_order", "Place a purchase order"),
               ("case_submission", "Submit an Amazon case")]:
    register(_t, Tier.FINANCIAL, _d, min_risk="high")

# ---- Prohibited for agents, whatever the approval ----
for _t in ("payment", "fund_transfer", "delete_listing", "change_credentials", "change_permissions",
           "change_settings", "approve_approval"):
    register(_t, Tier.PROHIBITED, "Prohibited for agents")
