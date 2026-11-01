"""Auth + workspace (identity/tenancy) tables for M1 — Milestone 1 scope.

IMPORTANT — TEMPORARY HOME, CONSOLIDATION PENDING:
The DB-models agent owns backend/app/db/models.py and backend/alembic/.
At the time this file was written models.py did not exist yet, so the
tables the auth service needs are defined here, in this SEPARATE file,
so the auth endpoints could be built and tested. The DB-models agent
will consolidate these tables into models.py (per the v0.1 spec,
Section C.1, as amended by v0.2 CL-11 / v0.3 CL-15 / v0.4 CL-20);
when that happens this file must be deleted and auth_service must
import from models.py instead. Do NOT duplicate these table
definitions in models.py — consolidate, don't copy.

Column mapping to the v0.1 spec (Section C.1):
- users: id UUID PK, email lower-cased UK, display_name, password_hash
  (NULL when an external identity provider is used, OD-03), status in
  ('active','invited','suspended','closed').
- merchants: id UUID PK (= merchant_id on every tenant table), name
  1-200 chars, trading_name, status in
  ('active','suspended','closing','closed'), created_by_user_id FK.
- merchant_memberships: (merchant_id, user_id) UK, role in
  ('owner','admin','member','viewer'), status in
  ('active','invited','revoked'). Owner memberships created by the
  workspace-create flow are 'active'.
- user_sessions: one row per server-managed session. Only the SHA-256
  hash of the opaque token is stored (column token_hash; v0.1 calls
  this refresh_token_hash), plus expiry, revocation and device label.
  A raw token, password or one-time code is NEVER stored here.
- password_reset_tokens (M1 addition, not in v0.1): hashed one-time
  token, 1h expiry, single use (used_at). Raw token is emailed/logged
  only, never stored.

Money rule reminder: no money columns exist in this file. All amounts
in CourierLedger are integer poisha (BIGINT ..._poisha); no FLOAT
money column is permitted anywhere in M1.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Uuid


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint("status IN ('active','invited','suspended','closed')", name="ck_users_status"),
        CheckConstraint("email LIKE '%@%'", name="ck_users_email_has_at"),
    )


class Merchant(Base):
    """A merchant workspace — the tenant root. Its id IS merchant_id
    on every tenant-owned table."""

    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    trading_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # CL-15 (v0.3): tombstone merchant -- the row is anonymised in place,
    # its id (every table's merchant_id) never moves. Added by the
    # DB-models agent; nullable, so existing auth behaviour is unchanged.
    anonymised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('active','suspended','closing','closed')", name="ck_merchants_status"),
        CheckConstraint("length(name) BETWEEN 1 AND 200", name="ck_merchants_name_len"),
        CheckConstraint(
            "anonymised_at IS NULL OR status = 'closed'",
            name="ck_merchants_anonymised_only_when_closed",
        ),
    )


class MerchantMembership(Base):
    __tablename__ = "merchant_memberships"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("merchant_id", "user_id", name="uq_membership_merchant_user"),
        # Added by the DB-models agent: UNIQUE (merchant_id, id) so this
        # table follows the same tenant-parent pattern as every other
        # tenant-owned table (v0.1 A.7/R2). Harmless (id is already the PK).
        UniqueConstraint("merchant_id", "id", name="uq_memberships_merchant_id"),
        CheckConstraint("role IN ('owner','admin','member','viewer')", name="ck_membership_role"),
        CheckConstraint("status IN ('active','invited','revoked')", name="ck_membership_status"),
    )


class UserSession(Base):
    """One server-managed session. token_hash is the SHA-256 hex of the
    opaque cookie token; the raw token is never stored."""

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    device_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class PasswordResetToken(Base):
    """Hashed one-time password-reset token. Single use, 1h expiry."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
