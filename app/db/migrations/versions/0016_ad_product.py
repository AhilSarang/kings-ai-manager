"""Which ad product a row belongs to — Sponsored Products, Brands or Display.

Everything built so far is Sponsored Products, and said so nowhere. `ppc_campaigns.campaign_type`
existed, was hardcoded to the literal "sponsoredProducts" in one place, and was read by nothing at
all. So every query in the system was written as though it meant "Sponsored Products" while actually
meaning "all ad products" — harmless with one ad product, wrong the moment there are three.

Two distinct problems, and the second is the dangerous one.

**Blending.** Sponsored Display reporting carries view-through attribution: sales credited to an
impression nobody clicked. Adding those to Sponsored Products click-attributed sales and dividing by
the combined spend produces an ACOS that describes no real thing. Sponsored Brands runs a different
auction again, and both support vCPM where cost-per-click is not even defined. The brief's M12 line
is explicit — "keep SP learning separate from SB/SD learning (different mechanics, separate playbook
scopes)" — and separation has to exist in the schema before it can be enforced in a query.

**Collision.** Every uniqueness key here was `(profile, …, amazon_id, …)`. Amazon's campaign,
ad group and target ids come from three independent services, and nothing documents a shared id
space across them. If an SB campaign ever carried the same id as an SP campaign in the same profile,
`ppc_metrics_daily`'s `(profile, entity_type, amazon_entity_id, date)` upsert would overwrite one ad
product's day with another's and nobody would ever know. "Their ids probably do not overlap" is not
a thing to build measurement on, so `ad_product` joins every one of those keys.

`ad_product` is `sp` | `sb` | `sd`, matching the prefixes Amazon's own report types use (spCampaigns,
sbCampaigns, sdCampaigns) so a loader can take it from the report it just asked for.

**NOT NULL with no server default**, deliberately, on everything that holds account data. A default
of 'sp' would mean a future Sponsored Display loader that forgot to set the column would record its
rows as Sponsored Products — silently, permanently, and in the one place where being wrong corrupts
every measurement downstream. Without a default that same mistake is an IntegrityError on the first
row. Loud and stopped beats quiet and wrong.

Three tables take it as nullable, where null genuinely means "all ad products": the owner's own
constraints in `ppc_rules`, research notes that may be about advertising in general, and analysis
runs that may cover everything.

Revision ID: 0016
Revises: 0015
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0016'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

AD_PRODUCTS = ("sp", "sb", "sd")

# Account data: every row belongs to exactly one ad product.
REQUIRED = (
    "ppc_campaigns", "ppc_ad_groups", "ppc_targets", "ppc_product_ads", "ppc_negatives",
    "ppc_metrics_daily", "ppc_search_terms", "ppc_placements", "ppc_purchased_products",
    "ppc_hourly_metrics",
    "ppc_metrics_restatements", "ppc_search_term_decisions",
    "ppc_recommendations", "ppc_actions", "ppc_action_outcomes", "ppc_predictions",
    "ppc_experiments", "ppc_experiment_units", "ppc_playbook", "ppc_alerts",
)

# Null means "all ad products" and is a meaningful answer.
OPTIONAL = ("ppc_rules", "ppc_research_notes", "ppc_analysis_runs")

# Uniqueness keys that would otherwise let two ad products' rows collide. Name, table, and the
# columns the key becomes — `ad_product` added, everything else unchanged.
KEYS = (
    ("campaign", "ppc_campaigns", ["profile_id", "ad_product", "amazon_campaign_id"]),
    ("ad_group", "ppc_ad_groups", ["profile_id", "ad_product", "amazon_ad_group_id"]),
    ("target", "ppc_targets", ["profile_id", "ad_product", "target_kind", "amazon_target_id"]),
    ("product_ad", "ppc_product_ads", ["profile_id", "ad_product", "amazon_ad_id"]),
    ("entity_day", "ppc_metrics_daily",
     ["amazon_profile_id", "ad_product", "entity_type", "amazon_entity_id", "date"]),
    ("search_term_day", "ppc_search_terms",
     ["amazon_profile_id", "ad_product", "amazon_ad_group_id", "amazon_target_id", "search_term",
      "date"]),
    ("placement_day", "ppc_placements",
     ["amazon_profile_id", "ad_product", "amazon_campaign_id", "placement", "date"]),
    ("purchased_day", "ppc_purchased_products",
     ["amazon_profile_id", "ad_product", "amazon_campaign_id", "advertised_asin", "purchased_asin",
      "date"]),
    ("campaign_hour", "ppc_hourly_metrics",
     ["amazon_profile_id", "ad_product", "amazon_campaign_id", "date", "hour"]),
    ("negative", "ppc_negatives", ["profile_id", "ad_product", "target_kind",
                                   "amazon_negative_id"]),
    ("term_decision", "ppc_search_term_decisions",
     ["amazon_profile_id", "ad_product", "amazon_ad_group_id", "search_term"]),
)


def _check(table: str) -> str:
    values = ", ".join(f"'{p}'" for p in AD_PRODUCTS)
    return f"ad_product IN ({values})"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    for table in REQUIRED:
        if table not in existing:
            continue
        # Added nullable, backfilled, then made NOT NULL. Adding it NOT NULL in one step would need
        # a server default, which is the thing this migration exists to avoid.
        op.add_column(table, sa.Column("ad_product", sa.String(length=2), nullable=True))
        op.execute(f"UPDATE {table} SET ad_product = 'sp' WHERE ad_product IS NULL")
        op.alter_column(table, "ad_product", nullable=False)
        op.create_check_constraint(f"ck_{table}_ad_product", table, _check(table))
        op.create_index(f"ix_{table}_ad_product", table, ["ad_product"])

    for table in OPTIONAL:
        if table not in existing:
            continue
        op.add_column(table, sa.Column("ad_product", sa.String(length=2), nullable=True))
        op.create_check_constraint(
            f"ck_{table}_ad_product", table,
            f"ad_product IS NULL OR {_check(table)}")

    # Uniqueness keys, rebuilt to include the ad product.
    for name, table, columns in KEYS:
        if table not in existing:
            continue
        op.drop_constraint(name, table, type_="unique")
        op.create_unique_constraint(name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    for key_name, key_table, columns in KEYS:
        if key_table not in existing:
            continue
        op.drop_constraint(key_name, key_table, type_="unique")
        op.create_unique_constraint(key_name, key_table,
                                    [c for c in columns if c != "ad_product"])

    for table in OPTIONAL:
        if table in existing:
            op.drop_constraint(f"ck_{table}_ad_product", table, type_="check")
            op.drop_column(table, "ad_product")

    for table in REQUIRED:
        if table not in existing:
            continue
        op.drop_index(f"ix_{table}_ad_product", table_name=table)
        op.drop_constraint(f"ck_{table}_ad_product", table, type_="check")
        op.drop_column(table, "ad_product")
