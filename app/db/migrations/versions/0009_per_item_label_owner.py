"""Per-item label and prep owner, and NONE as a label owner.

Amazon decides who labels each SKU (listPrepDetails): a stickerless SKU must go up as
labelOwner NONE, and sending SELLER makes it reject the whole inbound plan.

Revision ID: 0009
Revises: 0008
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fba_inbound_items", sa.Column("label_owner", sa.String(length=10), nullable=True))
    op.add_column("fba_inbound_items", sa.Column("prep_owner", sa.String(length=10), nullable=True))
    op.drop_constraint("label_owner", "fba_inbound_requests", type_="check")  # convention adds ck_<table>_
    op.create_check_constraint("label_owner", "fba_inbound_requests",
                               "label_owner IN ('SELLER', 'AMAZON', 'NONE')")


def downgrade() -> None:
    op.drop_constraint("label_owner", "fba_inbound_requests", type_="check")  # convention adds ck_<table>_
    op.create_check_constraint("label_owner", "fba_inbound_requests",
                               "label_owner IN ('SELLER', 'AMAZON')")
    op.drop_column("fba_inbound_items", "prep_owner")
    op.drop_column("fba_inbound_items", "label_owner")
