"""Session trace id, for end-to-end correlation of logs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import alembic
import sqlalchemy as sa

op = alembic.op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Adds the trace identifier of the creating request to sessions."""
    op.add_column("sessions", sa.Column("trace_id", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drops the trace identifier."""
    op.drop_column("sessions", "trace_id")
