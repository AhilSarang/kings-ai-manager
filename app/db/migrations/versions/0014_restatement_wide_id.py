"""Let a restatement name a search term, not just an id.

`ppc_metrics_restatements.amazon_entity_id` was String(40), which holds a campaign or target id but
not "G12345|nicotine pouches mint flavour". Search term, placement and purchased-product rows were
therefore being overwritten by a re-pull with nothing recording what changed, while acceptance 1
asks for restatements to be logged. Widened so every fact table's restatements can be recorded.

Revision ID: 0014
Revises: 0013
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0014'
down_revision: Union[str, None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("ppc_metrics_restatements", "amazon_entity_id",
                    existing_type=sa.String(length=40), type_=sa.String(length=300),
                    existing_nullable=False)


def downgrade() -> None:
    op.execute("delete from ppc_metrics_restatements where length(amazon_entity_id) > 40")
    op.alter_column("ppc_metrics_restatements", "amazon_entity_id",
                    existing_type=sa.String(length=300), type_=sa.String(length=40),
                    existing_nullable=False)
