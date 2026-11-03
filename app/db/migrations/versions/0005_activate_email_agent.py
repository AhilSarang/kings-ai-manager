"""activate the email agent

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE agents SET status = 'active' WHERE key = 'email'")


def downgrade() -> None:
    op.execute("UPDATE agents SET status = 'inactive' WHERE key = 'email'")
