"""attachments on outgoing email drafts

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("email_drafts", sa.Column("attachment_file_ids", postgresql.JSONB(astext_type=sa.Text()),
                                            server_default=sa.text("'[]'::jsonb"), nullable=False))


def downgrade() -> None:
    op.drop_column("email_drafts", "attachment_file_ids")
