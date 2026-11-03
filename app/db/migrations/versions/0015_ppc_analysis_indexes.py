"""Indexes the daily analysis needs, and the one the search-term aggregate was missing.

The 30-day search-term rollup filters on (profile, date, is_provisional) and nothing else; the three
indexes 0012 created all lead with a campaign or a term, so none of them helped and the query fell
back to reading the table. On an account seeing fifty thousand search-term rows a month that is the
slowest thing in the analysis cycle.

Revision ID: 0015
Revises: 0014
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0015'
down_revision: Union[str, None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_ppc_search_terms_profile_date", "ppc_search_terms",
                    ["amazon_profile_id", "date", "is_provisional"])


def downgrade() -> None:
    op.drop_index("ix_ppc_search_terms_profile_date", table_name="ppc_search_terms")
