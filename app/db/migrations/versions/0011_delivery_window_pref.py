"""The owner's delivery-window preference, taken on the shipment form.

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fba_inbound_requests", sa.Column("delivery_window_pref", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("fba_inbound_requests", "delivery_window_pref")
