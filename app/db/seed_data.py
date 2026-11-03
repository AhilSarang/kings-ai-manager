"""Static seed data inserted by the initial migration.

Kept separate from the migration so tests and docs can refer to it.
"""

from __future__ import annotations

# max_tier: highest permission tier the agent may *request* (Section 9).
AGENTS = [
    {
        "key": "supervisor",
        "display_name": "Supervisor (AI Manager)",
        "status": "active",
        "description": "Logs and tracks tasks, routes work, answers questions, drafts text.",
        "permissions": {"max_tier": 2},
        "config": {"phase": 1},
    },
    {
        "key": "email",
        "display_name": "Email Agent",
        "status": "active",
        "description": "Inbox triage, replies and follow-ups.",
        "permissions": {"max_tier": 2},
        "config": {"phase": 2},
    },
    {
        "key": "amazon",
        "display_name": "Amazon Agent",
        "status": "inactive",
        "description": "Seller Central cases, listings, inventory, reimbursements, account health.",
        "permissions": {"max_tier": 3},
        "config": {"phase": 3},
    },
    {
        "key": "ppc",
        "display_name": "Amazon PPC Agent",
        "status": "inactive",
        "description": "Ads, ACOS, bids, keywords, campaigns.",
        "permissions": {"max_tier": 3},
        "config": {"phase": 4},
    },
    {
        "key": "sourcing",
        "display_name": "Sourcing Agent",
        "status": "inactive",
        "description": "Supplier and product sourcing.",
        "permissions": {"max_tier": 2},
        "config": {"phase": 5},
    },
    {
        "key": "reporting",
        "display_name": "Reporting Agent",
        "status": "inactive",
        "description": "Daily executive PDF report and ad-hoc reports.",
        "permissions": {"max_tier": 1},
        "config": {"phase": 6},
    },
]

DEFAULT_SETTINGS = {
    "system_paused": False,
    "daily_summary_time": None,  # None = use DAILY_SUMMARY_TIME from env
    "quiet_hours": {"start": "22:00", "end": "07:00"},
    "llm_concurrency": None,  # None = use LLM_CONCURRENCY from env
    "daily_summary_llm_note": True,
}

# ---- Phase 2: email ----
# Built-in alert categories (PHASE_2_EMAIL_BRIEF Section 5). `value` is the category letter.
EMAIL_ALERT_RULES = [
    {"name": "A. Amazon account health / policy", "rule_type": "builtin_category", "value": "A",
     "alert_level": "instant", "bypass_quiet_hours": True, "created_by": "system", "is_active": True},
    {"name": "B. Legal notices / disputes", "rule_type": "builtin_category", "value": "B",
     "alert_level": "instant", "bypass_quiet_hours": True, "created_by": "system", "is_active": True},
    {"name": "C. Large customer / B2B order problems", "rule_type": "builtin_category", "value": "C",
     "alert_level": "instant", "bypass_quiet_hours": False, "created_by": "system", "is_active": True},
    {"name": "D. Supplier problems (delays, price changes)", "rule_type": "builtin_category", "value": "D",
     "alert_level": "instant", "bypass_quiet_hours": False, "created_by": "system", "is_active": True},
]

BRANDS = [
    {"name": "Skyzz", "brand_registry": True, "owned_by_company": True, "notes": "Kings Distro own brand"},
    {"name": "Vibezy", "brand_registry": True, "owned_by_company": True, "notes": "Kings Distro own brand"},
]

SHIP_FROM_ADDRESSES = [
    {"label": "Richmond warehouse", "name": "Kings Distro Inc.", "company": "Kings Distro Inc.",
     "address_line1": "9012 Hermitage Rd", "address_line2": None, "city": "Richmond",
     "state_or_province": "VA", "postal_code": "23228", "country_code": "US",
     "phone": None, "email": None, "is_default": True},
]

AMAZON_SETTINGS = {
    "amazon_price_change_warn_pct": 15,
    "amazon_price_change_pin_pct": 40,
    "amazon_low_stock_days_cover": 21,
    "amazon_sales_drop_alert_pct": 40,
    "amazon_sync_interval_minutes": 60,
    "amazon_paused": False,
    "fba_default_shipping_mode": "small_parcel",
    # the owner books his own freight and picks "Other" in Seller Central, so that is the default when
    # Amazon offers it for every shipment. Naming a carrier on the command overrides it.
    "fba_default_own_carrier": "Other",
    # "lowest_fee": pre-pick the split Amazon charges least for (usually the most warehouses).
    # "ask": list the options and wait for the owner to name one. Either way he approves the fee.
    "fba_placement_preference": "lowest_fee",
    "fba_default_label_owner": "SELLER",
    "fba_default_carrier_choice": "ask",
    "fba_label_page_type": "PackageLabel_Thermal_NonPCP",
    "fba_pin_required_above_usd": 250,
    "fba_ready_to_ship_default": "next_business_day",
    "fba_labels_drive_folder": "FBA Shipment Labels",
}

EMAIL_SETTINGS = {
    "email_followup_business_days": 3,
    "email_large_order_threshold_usd": None,
    "email_triage_batch_size": 10,
    "email_paused": False,
}
DEFAULT_SETTINGS.update(EMAIL_SETTINGS)
# ---- PPC agent (Phase 4). Brief Section 5; every one of these is the owner's to change. ----
PPC_SETTINGS = {
    # Targets are derived from margin unless the owner overrides them per ASIN or brand.
    "ppc_default_target_acos_source": "derived",
    "ppc_target_acos_safety_margin_pct": 70,
    # Change sizing and the evidence needed before a change is proposed at all.
    "ppc_max_bid_change_pct": 25,
    "ppc_min_clicks_for_bid_change": 15,
    "ppc_min_clicks_for_pause": 30,
    "ppc_harvest_min_orders": 2,
    "ppc_cooldown_days_after_change": 14,
    # Experiments: few at a time, each with a hard ceiling on what it can lose.
    "ppc_max_experiments_running": 3,
    "ppc_experiment_max_spend_at_risk_usd": 300,
    "ppc_daily_recommendation_cap": 25,
    # Alerting and reporting.
    "ppc_anomaly_spend_spike_pct": 60,
    "ppc_report_time": "08:00",
    # Safety: no single approval may move more than this share of account spend without the PIN.
    "ppc_max_blast_radius_pct": 25,
    "ppc_paused": False,
}

DEFAULT_SETTINGS.update(AMAZON_SETTINGS)
DEFAULT_SETTINGS.update(PPC_SETTINGS)
