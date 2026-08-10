"""Initial schema: number pool, sessions, parties, call journal.

Revision ID: 0001
Revises:
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence

import alembic
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.db import triggers

op = alembic.op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Creates every table, index and trigger of the service."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "numbers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("e164", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'enabled'"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('enabled','disabled')", name="numbers_status_check"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("e164"),
    )

    op.create_table(
        "sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("number_id", sa.BigInteger(), nullable=False),
        sa.Column("ext_code", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("max_calls", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active','expired','closed')",
            name="sessions_status_check",
        ),
        sa.ForeignKeyConstraint(["number_id"], ["numbers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sessions_number_id", "sessions", ["number_id"])
    op.create_index("ix_sessions_external_id", "sessions", ["external_id"])
    op.create_index(
        "ix_sessions_status_expires_at", "sessions", ["status", "expires_at"]
    )
    op.execute(
        "CREATE UNIQUE INDEX uniq_active_ext_code_per_number "
        "ON sessions (number_id, ext_code) "
        "WHERE status = 'active' AND ext_code IS NOT NULL"
    )

    op.create_table(
        "session_parties",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=1), nullable=False),
        sa.Column("number_id", sa.BigInteger(), nullable=False),
        sa.Column("party_e164_enc", sa.LargeBinary(), nullable=False),
        sa.Column("party_hash", sa.Text(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "has_ext_code",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "role IN ('a','b')", name="session_parties_role_check"
        ),
        sa.ForeignKeyConstraint(["number_id"], ["numbers.id"]),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("session_id", "role"),
    )
    op.create_index(
        "ix_session_parties_lookup",
        "session_parties",
        ["number_id", "party_hash"],
    )
    op.execute(
        "CREATE UNIQUE INDEX uniq_active_party_per_number "
        "ON session_parties (number_id, party_hash) "
        "WHERE is_active AND NOT has_ext_code"
    )

    op.create_table(
        "calls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("direction", sa.Text(), nullable=True),
        sa.Column("caller_hash", sa.Text(), nullable=False),
        sa.Column("proxy_e164", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("hangup_cause", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.Text(), nullable=True),
        sa.Column("bridge_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN ('a2b','b2a')",
            name="calls_dir_check",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_calls_session_started", "calls", ["session_id", "started_at"]
    )
    op.create_index("ix_calls_started", "calls", ["started_at"])
    op.create_index("ix_calls_channel_id", "calls", ["channel_id"])

    op.execute(triggers.SYNC_FUNCTION)
    op.execute(triggers.SYNC_TRIGGER)


def downgrade() -> None:
    """Drops everything :func:`upgrade` created."""
    op.execute(triggers.DROP_TRIGGER)
    op.execute(triggers.DROP_FUNCTION)
    op.drop_table("calls")
    op.drop_table("session_parties")
    op.drop_table("sessions")
    op.drop_table("numbers")
