"""SQLAlchemy models.

PostgreSQL is the source of truth for the number pool, the masking sessions and
the call journal. Two invariants are enforced by the schema rather than by
application code, because they must hold even under concurrent writers:

* at most one active *exclusive* session per (proxy number, subscriber);
* at most one active PIN per (proxy number, PIN).
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.dialects import postgresql

NUMBER_STATUSES = ("enabled", "disabled")
SESSION_STATUSES = ("active", "expired", "closed")
CALL_STATUSES = (
    "in_progress",
    "answered",
    "no_answer",
    "busy",
    "rejected",
    "expired",
    "unknown_caller",
    "failed",
)


class Base(orm.DeclarativeBase):
    """Declarative base for every model in the service."""


class Number(Base):
    """A proxy number (DID) from the pool.

    Attributes:
        e164: The number in strict E.164 form.
        status: ``enabled`` or ``disabled``; a disabled number is never
            allocated but keeps serving its live sessions.
        provider: Free-form origin marker, for example ``demo``.
        released_at: When the last session on this number closed. Drives the
            cooldown that keeps a number out of circulation for a while.
    """

    __tablename__ = "numbers"

    id: orm.Mapped[int] = orm.mapped_column(
        sa.BigInteger, primary_key=True, autoincrement=True
    )
    e164: orm.Mapped[str] = orm.mapped_column(
        sa.Text, nullable=False, unique=True
    )
    status: orm.Mapped[str] = orm.mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'enabled'")
    )
    provider: orm.Mapped[str | None] = orm.mapped_column(sa.Text)
    released_at: orm.Mapped[datetime.datetime | None] = orm.mapped_column(
        sa.DateTime(timezone=True)
    )
    created_at: orm.Mapped[datetime.datetime] = orm.mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('enabled','disabled')", name="numbers_status_check"
        ),
    )


class Session(Base):
    """A masking session: a pair of real numbers bound to one proxy number.

    Attributes:
        number_id: The allocated proxy number.
        ext_code: DTMF PIN, or None when the number serves this session
            exclusively for both parties.
        status: ``active``, ``expired`` or ``closed``.
        external_id: Identifier of the order in the customer's system.
        max_calls: Cap on answered calls, or None for no cap.
        trace_id: Trace of the request that created the session; every call
            routed to it logs the same value, which is what makes the trace
            end to end.
        expires_at: After this moment no new call is connected.
        closed_at: When the session left the ``active`` state.
    """

    __tablename__ = "sessions"

    id: orm.Mapped[uuid.UUID] = orm.mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.func.gen_random_uuid(),
    )
    number_id: orm.Mapped[int] = orm.mapped_column(
        sa.BigInteger, sa.ForeignKey("numbers.id"), nullable=False, index=True
    )
    ext_code: orm.Mapped[str | None] = orm.mapped_column(sa.Text)
    status: orm.Mapped[str] = orm.mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'active'")
    )
    external_id: orm.Mapped[str | None] = orm.mapped_column(sa.Text, index=True)
    max_calls: orm.Mapped[int | None] = orm.mapped_column(sa.Integer)
    trace_id: orm.Mapped[str | None] = orm.mapped_column(sa.Text)
    created_at: orm.Mapped[datetime.datetime] = orm.mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    expires_at: orm.Mapped[datetime.datetime] = orm.mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    closed_at: orm.Mapped[datetime.datetime | None] = orm.mapped_column(
        sa.DateTime(timezone=True)
    )

    number: orm.Mapped[Number] = orm.relationship(lazy="joined")
    parties: orm.Mapped[list[SessionParty]] = orm.relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('active','expired','closed')",
            name="sessions_status_check",
        ),
        sa.Index("ix_sessions_status_expires_at", "status", "expires_at"),
        sa.Index(
            "uniq_active_ext_code_per_number",
            "number_id",
            "ext_code",
            unique=True,
            postgresql_where=sa.text(
                "status = 'active' AND ext_code IS NOT NULL"
            ),
        ),
    )

    @property
    def is_active(self) -> bool:
        """True while the session still accepts new calls."""
        return self.status == "active"


class SessionParty(Base):
    """One side of a session; two rows per session, roles ``a`` and ``b``.

    Attributes:
        party_e164_enc: AES-256-GCM ciphertext of the real number. Never
            logged, never returned raw.
        party_hash: HMAC-SHA256 of the real number, the only searchable form.
        is_active: Mirrors ``sessions.status = 'active'``.
        has_ext_code: Mirrors ``sessions.ext_code IS NOT NULL``.
        released_at: Copy of ``sessions.closed_at`` for the per-party cooldown.
    """

    __tablename__ = "session_parties"

    session_id: orm.Mapped[uuid.UUID] = orm.mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: orm.Mapped[str] = orm.mapped_column(sa.String(1), primary_key=True)
    number_id: orm.Mapped[int] = orm.mapped_column(
        sa.BigInteger, sa.ForeignKey("numbers.id"), nullable=False
    )
    party_e164_enc: orm.Mapped[bytes] = orm.mapped_column(
        sa.LargeBinary, nullable=False
    )
    party_hash: orm.Mapped[str] = orm.mapped_column(sa.Text, nullable=False)
    is_active: orm.Mapped[bool] = orm.mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("true")
    )
    has_ext_code: orm.Mapped[bool] = orm.mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    released_at: orm.Mapped[datetime.datetime | None] = orm.mapped_column(
        sa.DateTime(timezone=True)
    )

    session: orm.Mapped[Session] = orm.relationship(back_populates="parties")

    __table_args__ = (
        sa.CheckConstraint(
            "role IN ('a','b')", name="session_parties_role_check"
        ),
        sa.Index(
            "uniq_active_party_per_number",
            "number_id",
            "party_hash",
            unique=True,
            postgresql_where=sa.text("is_active AND NOT has_ext_code"),
        ),
        sa.Index("ix_session_parties_lookup", "number_id", "party_hash"),
    )


class Call(Base):
    """One call attempt through a proxy number.

    Holds no real phone numbers: only ``caller_hash``, the proxy number and the
    session. Conversation content is never recorded.

    Attributes:
        direction: ``a2b`` when party ``a`` called, ``b2a`` otherwise.
        duration_sec: Talk time, from answer to hangup.
        status: One of :data:`CALL_STATUSES`.
        hangup_cause: Q.850 cause code as reported by Asterisk.
    """

    __tablename__ = "calls"

    id: orm.Mapped[int] = orm.mapped_column(
        sa.BigInteger, primary_key=True, autoincrement=True
    )
    session_id: orm.Mapped[uuid.UUID | None] = orm.mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("sessions.id", ondelete="SET NULL"),
    )
    direction: orm.Mapped[str | None] = orm.mapped_column(sa.Text)
    caller_hash: orm.Mapped[str] = orm.mapped_column(sa.Text, nullable=False)
    proxy_e164: orm.Mapped[str] = orm.mapped_column(sa.Text, nullable=False)
    started_at: orm.Mapped[datetime.datetime] = orm.mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    answered_at: orm.Mapped[datetime.datetime | None] = orm.mapped_column(
        sa.DateTime(timezone=True)
    )
    ended_at: orm.Mapped[datetime.datetime | None] = orm.mapped_column(
        sa.DateTime(timezone=True)
    )
    duration_sec: orm.Mapped[int | None] = orm.mapped_column(sa.Integer)
    status: orm.Mapped[str] = orm.mapped_column(sa.Text, nullable=False)
    hangup_cause: orm.Mapped[str | None] = orm.mapped_column(sa.Text)
    channel_id: orm.Mapped[str | None] = orm.mapped_column(sa.Text, index=True)
    bridge_id: orm.Mapped[str | None] = orm.mapped_column(sa.Text)

    __table_args__ = (
        sa.CheckConstraint(
            "direction IS NULL OR direction IN ('a2b','b2a')",
            name="calls_dir_check",
        ),
        sa.Index("ix_calls_session_started", "session_id", "started_at"),
        sa.Index("ix_calls_started", "started_at"),
    )
