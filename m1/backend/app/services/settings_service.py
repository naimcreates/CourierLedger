"""Workspace settings service (M1, Milestone 9).

One ``WorkspaceSettings`` row per merchant (v0.1 C.7). The only
settable controls are the columns on that model:

- ``tolerance_poisha``: integer >= 0. The single asserted exception
  to the no-money-default rule (v0.1 C.7 / IMPLEMENTATION_CHECKLIST
  discrepancy 5): the effective default is 0 when no row exists.
  A tolerance change affects future runs only -- each run copies
  the tolerance it used (``run_service._resolve_tolerance`` reads
  this same row, and returns 0 when no row exists, so GET/PATCH
  here and run creation agree on the effective value).
- ``locale``: ``"en"`` or ``"en_bn"`` (default ``"en_bn"``). Stored
  display preference only; nothing else in the backend reads it yet.

NULL policy (decided for M9, part 2): an explicit JSON null for
either field is rejected with 422 at the API layer -- null is NOT
a silent "reset to default". To restore the default tolerance,
PATCH ``tolerance_poisha: 0`` explicitly. This service also refuses
a None value defensively if one ever reaches it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.auth_models import User
from app.db.models import WorkspaceSettings
from app.db.session import set_tenant_context
from app.services import audit_service

DEFAULT_TOLERANCE_POISHA = 0
DEFAULT_LOCALE = "en_bn"
LOCALES = ("en", "en_bn")

# Sentinel for "field not provided" (distinct from an explicit null,
# which the API layer rejects before calling here).
UNSET: object = object()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_settings_row(db: Session, merchant_id: uuid.UUID) -> WorkspaceSettings | None:
    return db.execute(
        select(WorkspaceSettings).where(WorkspaceSettings.merchant_id == merchant_id)
    ).scalar_one_or_none()


def settings_dict(merchant_id: uuid.UUID, row: WorkspaceSettings | None) -> dict:
    """Effective settings: the stored row, or the documented defaults
    when no row exists yet (``is_default`` says which it is)."""
    if row is None:
        return {
            "workspace_id": str(merchant_id),
            "tolerance_poisha": DEFAULT_TOLERANCE_POISHA,
            "locale": DEFAULT_LOCALE,
            "is_default": True,
            "updated_by_user_id": None,
            "updated_at": None,
        }
    return {
        "workspace_id": str(merchant_id),
        "tolerance_poisha": row.tolerance_poisha,
        "locale": row.locale,
        "is_default": False,
        "updated_by_user_id": (
            str(row.updated_by_user_id) if row.updated_by_user_id is not None else None
        ),
        "updated_at": row.updated_at.isoformat() if row.updated_at is not None else None,
    }


def get_effective_settings(db: Session, merchant_id: uuid.UUID) -> dict:
    """Effective settings for a workspace (stored row, else defaults).
    Read-only: never creates a row."""
    return settings_dict(merchant_id, get_settings_row(db, merchant_id))


def update_settings(
    db: Session,
    *,
    user: User,
    merchant_id: uuid.UUID,
    tolerance_poisha: int | object = UNSET,
    locale: str | object = UNSET,
) -> dict:
    """Create the settings row if missing, else update the fields
    provided. Fields not provided are left unchanged. A caller that
    passes an explicit None for either field gets ValueError (the
    API layer maps this to 422 before it can happen from HTTP)."""
    if tolerance_poisha is not UNSET:
        if tolerance_poisha is None:
            raise ValueError("tolerance_poisha must not be null; send 0 to restore the default.")
        if isinstance(tolerance_poisha, bool) or not isinstance(tolerance_poisha, int):
            raise ValueError("tolerance_poisha must be an integer.")
        if tolerance_poisha < 0:
            raise ValueError("tolerance_poisha must be >= 0.")
    if locale is not UNSET:
        if locale is None:
            raise ValueError("locale must not be null.")
        if locale not in LOCALES:
            raise ValueError("locale must be 'en' or 'en_bn'.")

    row = get_settings_row(db, merchant_id)
    # Old values for the audit trail (Phase 1.1, B-F10): a workspace
    # with no row yet has the documented effective defaults.
    old_tolerance = row.tolerance_poisha if row is not None else DEFAULT_TOLERANCE_POISHA
    old_locale = row.locale if row is not None else DEFAULT_LOCALE
    if row is None:
        if tolerance_poisha is UNSET and locale is UNSET:
            # No-op PATCH on a workspace with no row yet: return the
            # effective defaults without creating a row (reading never
            # creates one either).
            return settings_dict(merchant_id, None)
        row = WorkspaceSettings(
            merchant_id=merchant_id,
            tolerance_poisha=(
                tolerance_poisha if tolerance_poisha is not UNSET
                else DEFAULT_TOLERANCE_POISHA
            ),
            locale=locale if locale is not UNSET else DEFAULT_LOCALE,
            updated_by_user_id=user.id,
        )
        db.add(row)
    else:
        if tolerance_poisha is not UNSET:
            row.tolerance_poisha = tolerance_poisha
        if locale is not UNSET:
            row.locale = locale
        row.updated_by_user_id = user.id
    # _updated_at() carries a server default but no onupdate, so set
    # it explicitly on every write.
    row.updated_at = _utcnow()
    # Flush so a newly created row has its id for the audit row below.
    db.flush()
    # Audit only when a value actually changed (a PATCH that sends
    # no fields, or the same values, still bumps updated_at above
    # but records no settings event -- there is nothing to audit).
    changes: dict = {}
    if tolerance_poisha is not UNSET and row.tolerance_poisha != old_tolerance:
        changes["tolerance_poisha"] = {"old": old_tolerance, "new": row.tolerance_poisha}
    if locale is not UNSET and row.locale != old_locale:
        changes["locale"] = {"old": old_locale, "new": row.locale}
    if changes:
        audit_service.record_audit(
            db,
            actor=user,
            merchant_id=merchant_id,
            action=audit_service.SETTINGS_UPDATED,
            entity_table="workspace_settings",
            entity_id=row.id,
            detail=changes,
        )
    db.commit()
    # Commit ends the transaction and clears the transaction-local
    # RLS tenant context; re-assert it before refresh, or the
    # workspace_settings SELECT policy hides the row from the runtime
    # role and refresh raises "Could not refresh instance" (same
    # B-1 pattern as workspace_service.create_workspace).
    set_tenant_context(db, merchant_id)
    db.refresh(row)
    return settings_dict(merchant_id, row)
