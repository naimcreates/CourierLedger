"""Audit trail (Phase 1.1, B-F10).

The ``audit_logs`` table (v0.1 C.7) existed with RLS + an append-only
trigger but NOTHING wrote to it. This module is the single writer:
every consequential mutation records one row in the same transaction
as the mutation, so the audit row and the change commit (or roll back)
together.

Rules:
- ``actor_user_id`` / ``merchant_id`` come ONLY from server-side
  arguments (the authenticated user, the path-verified workspace) --
  never from client input.
- ``detail`` is small and safe: ids, old/new role or status, counts,
  amounts in poisha. NEVER passwords, tokens, bank account numbers,
  raw file contents, or email addresses (login_failed records no
  email at all).
- ``record_audit`` never commits: the caller owns the transaction.
  A call site that has no commit of its own (e.g. the login endpoint
  after ``create_session`` already committed) commits right after
  recording.
- ``merchant_id`` is NULL only for pre-workspace identity events
  (register / login / logout / password reset / step-up). The
  ``audit_logs`` INSERT policy allows NULL merchant for exactly those
  rows; tenant rows require the transaction-local tenant context to
  match (call sites run under it already -- see each service).
- ``created_at`` is set explicitly (application clock, UTC) rather
  than left to the ``server_default now()``. The ORM emits
  ``INSERT ... RETURNING`` for server-defaulted columns, and for
  INSERT ... RETURNING PostgreSQL ALSO applies the table's SELECT
  policy to the returned row -- a NULL-merchant identity row can
  never pass that policy, so the insert would fail with a row-level
  security violation even though the INSERT policy allows it
  (verified live: a plain INSERT succeeds, the RETURNING form does
  not). An explicit value keeps the insert RETURNING-free.
- The action vocabulary is closed: an unknown action raises
  ValueError rather than silently writing a free-text row.

What is NOT audited (deliberate): read endpoints; idempotent
replays that change nothing (re-invite of an already-invited member,
accept of an already-active membership, complete of an already-
complete receipt, idempotency replays of runs/imports); failed
validations (422) and rate-limited requests, which mutate nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import AuditLog

# ---- Action vocabulary (closed) ------------------------------------
# Auth / identity (merchant_id NULL).
AUTH_REGISTER = "auth.register"
AUTH_LOGIN_SUCCESS = "auth.login_success"
AUTH_LOGIN_FAILED = "auth.login_failed"
AUTH_LOGOUT = "auth.logout"
AUTH_REVOKE_ALL = "auth.revoke_all"
AUTH_PASSWORD_RESET_REQUESTED = "auth.password_reset_requested"
AUTH_PASSWORD_RESET_COMPLETED = "auth.password_reset_completed"
AUTH_STEP_UP_VERIFIED = "auth.step_up_verified"
# Membership.
MEMBERS_INVITED = "members.invited"
MEMBERS_INVITATION_ACCEPTED = "members.invitation_accepted"
MEMBERS_ROLE_CHANGED = "members.role_changed"
MEMBERS_REVOKED = "members.revoked"
MEMBERS_OWNER_TRANSFERRED = "members.owner_transferred"
MEMBERS_SESSIONS_FORCE_REVOKED = "members.sessions_force_revoked"
# Imports / runs / settlements / settings.
IMPORTS_UPLOADED = "imports.uploaded"
RUNS_CREATED = "runs.created"
SETTLEMENTS_BATCH_CREATED = "settlements.batch_created"
SETTLEMENTS_RECEIPT_CREATED = "settlements.receipt_created"
SETTLEMENTS_RECEIPT_COMPLETED = "settlements.receipt_completed"
SETTLEMENTS_ALLOCATED = "settlements.allocated"
SETTLEMENTS_REVERSED = "settlements.reversed"
SETTLEMENTS_VARIANCE_CALCULATED = "settlements.variance_calculated"
SETTINGS_UPDATED = "settings.updated"

ALL_ACTIONS = frozenset(
    {
        AUTH_REGISTER,
        AUTH_LOGIN_SUCCESS,
        AUTH_LOGIN_FAILED,
        AUTH_LOGOUT,
        AUTH_REVOKE_ALL,
        AUTH_PASSWORD_RESET_REQUESTED,
        AUTH_PASSWORD_RESET_COMPLETED,
        AUTH_STEP_UP_VERIFIED,
        MEMBERS_INVITED,
        MEMBERS_INVITATION_ACCEPTED,
        MEMBERS_ROLE_CHANGED,
        MEMBERS_REVOKED,
        MEMBERS_OWNER_TRANSFERRED,
        MEMBERS_SESSIONS_FORCE_REVOKED,
        IMPORTS_UPLOADED,
        RUNS_CREATED,
        SETTLEMENTS_BATCH_CREATED,
        SETTLEMENTS_RECEIPT_CREATED,
        SETTLEMENTS_RECEIPT_COMPLETED,
        SETTLEMENTS_ALLOCATED,
        SETTLEMENTS_REVERSED,
        SETTLEMENTS_VARIANCE_CALCULATED,
        SETTINGS_UPDATED,
    }
)


def _coerce_uuid(value) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def record_audit(
    db: Session,
    *,
    actor,
    merchant_id,
    action: str,
    entity_table: str | None = None,
    entity_id=None,
    detail: dict | None = None,
    actor_type: str = "user",
) -> AuditLog:
    """Insert one AuditLog row in the caller's transaction. Does NOT
    commit -- the caller commits, so the audit row shares the
    mutation's transaction (both land or neither does).

    ``actor`` is a User object or None (None only for failed/unknown-
    account identity events such as a failed login). ``merchant_id``
    is a UUID/str or None (None only for pre-workspace identity
    events). ``detail`` must contain only safe values (ids as
    strings, statuses, counts, poisha amounts) -- never passwords,
    tokens, bank account numbers or raw file contents.
    """
    if action not in ALL_ACTIONS:
        raise ValueError(f"Unknown audit action: {action!r}")
    if actor_type not in ("user", "system"):
        raise ValueError(f"Unknown audit actor_type: {actor_type!r}")
    row = AuditLog(
        actor_user_id=getattr(actor, "id", None),
        merchant_id=_coerce_uuid(merchant_id),
        actor_type=actor_type,
        action=action,
        entity_table=entity_table,
        entity_id=_coerce_uuid(entity_id),
        detail=dict(detail) if detail else {},
        # Explicit created_at: keeps the INSERT free of RETURNING
        # (see the module docstring -- a RETURNING insert would fail
        # the audit_logs SELECT policy for NULL-merchant rows).
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    return row
