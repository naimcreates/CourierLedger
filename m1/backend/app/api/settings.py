"""Workspace settings API (M1, Milestone 9).

GET   /api/workspaces/{id}/settings -- any active member may read.
      Returns the stored settings, or the effective defaults
      (tolerance 0 poisha, locale "en_bn") when no settings row
      exists yet. Reading never creates a row.
PATCH /api/workspaces/{id}/settings -- Owner/Admin only, CSRF.
      Creates the settings row when none exists. A tolerance change
      affects future runs only (each run copies the tolerance it
      used); it never rewrites a past run.

Fields (exactly the settable columns on WorkspaceSettings -- no
other preference is invented here):
- tolerance_poisha: integer >= 0 (StrictInt: floats, strings and
  bools are 422). An explicit JSON null is rejected with 422 --
  null is NOT a silent "reset to default"; send 0 to restore the
  documented default.
- locale: "en" or "en_bn". An explicit null is likewise 422.

Tenancy: the workspace must belong to the caller (membership
check); a non-member gets 404, never a leak. The client never
supplies a merchant_id -- the path id is checked against
merchant_memberships, and the tenant context is asserted from
that verified id before any settings row is touched (B-1 pattern).
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_csrf
from app.db.auth_models import User
from app.db.session import get_db, set_tenant_context
from app.services import settings_service, workspace_service

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["settings"])

# Roles allowed to change settings (v0.1 C.7: tolerance settable
# only by Owner/Admin -- enforced here in the application, not in
# the RLS policy, which cannot express roles).
SETTINGS_WRITE_ROLES = ("owner", "admin")


class UpdateSettingsRequest(BaseModel):
    """PATCH body. Every field is optional; omitted fields are left
    unchanged. An explicit null for either field is rejected in the
    endpoint (see module docstring) -- it is not a reset."""

    model_config = ConfigDict(extra="forbid")

    tolerance_poisha: StrictInt | None = Field(default=None, ge=0, le=10**15)
    locale: Literal["en", "en_bn"] | None = None


def _require_membership(db: Session, user: User, workspace_id: uuid.UUID):
    membership = workspace_service.get_membership(db, user.id, workspace_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    return membership


@router.get("/settings")
def get_settings(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    # Tenant context must be set before any tenant-table read (B-1
    # pattern: the workspace_settings SELECT policy keys on it).
    set_tenant_context(db, workspace_id)
    return settings_service.get_effective_settings(db, workspace_id)


@router.patch(
    "/settings",
    dependencies=[Depends(require_csrf)],
)
def update_settings(
    workspace_id: uuid.UUID,
    body: UpdateSettingsRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership = _require_membership(db, user, workspace_id)
    if membership.role not in SETTINGS_WRITE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only workspace owners and admins can change settings.",
        )
    # Explicit nulls are rejected, never silently converted to a
    # default. model_fields_set distinguishes "sent null" from
    # "field omitted" (omitted fields are left unchanged).
    if "tolerance_poisha" in body.model_fields_set and body.tolerance_poisha is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tolerance_poisha must not be null; send 0 to restore the default tolerance.",
        )
    if "locale" in body.model_fields_set and body.locale is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="locale must not be null.",
        )
    set_tenant_context(db, workspace_id)
    try:
        return settings_service.update_settings(
            db,
            user=user,
            merchant_id=workspace_id,
            tolerance_poisha=(
                body.tolerance_poisha
                if "tolerance_poisha" in body.model_fields_set
                else settings_service.UNSET
            ),
            locale=(
                body.locale
                if "locale" in body.model_fields_set
                else settings_service.UNSET
            ),
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
