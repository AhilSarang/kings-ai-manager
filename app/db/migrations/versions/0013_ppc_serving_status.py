"""Amazon's serving status for campaigns, ad groups, product ads and targets.

An entity can be ENABLED and still not be advertising — out of budget, payment failed, ineligible,
ASIN not buyable. Amazon reports that separately from `state`, in `extendedData.servingStatus`, and
without it stored there is no way to raise the `ad_serving_problem` alert the schema already names,
and "enabled" would be misread as "running".

Revision ID: 0013
Revises: 0012
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("ppc_campaigns", "ppc_ad_groups", "ppc_product_ads", "ppc_targets")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("serving_status", sa.String(length=60), nullable=True))
        op.add_column(table, sa.Column("serving_status_details",
                                       postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                                       server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "serving_status_details")
        op.drop_column(table, "serving_status")
