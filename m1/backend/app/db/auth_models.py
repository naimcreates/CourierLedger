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
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Uuid


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Phase 3 (Workstream 3) — naming convention for constraints the models
# declare WITHOUT an explicit name. PostgreSQL auto-names such
# constraints when migration 0001 created them (``<table>_<column>_key``
# for UNIQUE, ``<table>_<column>_fkey`` for FOREIGN KEY,
# ``ix_<table>_<column>`` for indexes, ``<table>_pkey`` for PRIMARY
# KEY); the convention below reproduces exactly those auto-names in
# the metadata, so ``alembic check`` (autogenerate) can pair the
# metadata constraint with the live one instead of reporting a
# spurious add/remove. It changes NO database object: every name it
# produces was verified live against pg_constraint / pg_indexes on
# courierledger_app before it was adopted (see
# MIGRATION_VERIFICATION.md §3). Explicitly named constraints
# (every __table_args__ entry, every named Index) are untouched by a
# convention.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "%(table_name)s_%(column_0_name)s_key",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}
# NOTE: no "ck" entry on purpose — every CHECK constraint in the
# models is explicitly named (ck_*), so a convention would never
# apply; leaving it out keeps an accidentally unnamed CHECK visibly
# unnamed instead of silently misnamed.


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


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
        # Phase 3: explicit unique constraint named exactly as migration
        # 0001's PostgreSQL auto-name (users_email_key). The column's
        # unique=True + index=True collapses in SQLAlchemy metadata to
        # a unique INDEX only, so without this the metadata under-
        # declares the live schema and autogenerate reports the live
        # constraint as "removed". The DB already has both the
        # constraint and the unique index ix_users_email; this only
        # makes the model say so. Metadata-only, no DDL.
        UniqueConstraint("email", name="users_email_key"),
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
    user_id: Mapped[uuid.UUID] = mapped_column(
        # Phase 3: index=True declares the live 0006 index
        # ix_user_sessions_user_id in the metadata so autogenerate no
        # longer reports it as a removal. Metadata-only; the index
        # already exists in every migrated database.
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Phase 1.1 (B-F11): step-up verification. Timestamp until which
    # this session counts as step-up-verified (password re-checked via
    # POST /api/auth/step-up). NULL = never step-up-verified.
    step_up_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    device_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        # Phase 3: see User.email — the live unique constraint
        # (migration 0001 auto-name user_sessions_token_hash_key)
        # declared explicitly so autogenerate can pair it. The column's
        # unique=True + index=True already declares the unique index
        # ix_user_sessions_token_hash. Metadata-only, no DDL.
        UniqueConstraint("token_hash", name="user_sessions_token_hash_key"),
    )


class PasswordResetToken(Base):
    """Hashed one-time password-reset token. Single use, 1h expiry."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        # Phase 3: index=True declares the live 0006 index
        # ix_password_reset_tokens_user_id in the metadata so
        # autogenerate no longer reports it as a removal.
        # Metadata-only; the index already exists.
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        # Phase 3: see User.email — the live unique constraint
        # (migration 0001 auto-name
        # password_reset_tokens_token_hash_key) declared explicitly so
        # autogenerate can pair it. Metadata-only, no DDL.
        UniqueConstraint("token_hash", name="password_reset_tokens_token_hash_key"),
    )
