"""The delivery window agreed with Amazon for a shipment.

Amazon refuses to confirm a carrier for some options until a window is agreed, so the one we
confirmed is kept with the shipment.

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fba_shipments", sa.Column("delivery_window", postgresql.JSONB(astext_type=sa.Text()),
                                             nullable=True))


def downgrade() -> None:
    op.drop_column("fba_shipments", "delivery_window")
