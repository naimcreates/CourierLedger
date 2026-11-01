"""Workspace member management API (M1, Milestone 9, part 1).

GET    /api/workspaces/{id}/members                 -- list members (any active member)
POST   /api/workspaces/{id}/members/invite          -- invite (owner/admin only)
POST   /api/workspaces/{id}/members/{mid}/accept    -- accept invitation (the invited user only)
PATCH  /api/workspaces/{id}/members/{mid}/role      -- change role (owner/admin only)
POST   /api/workspaces/{id}/members/{mid}/revoke    -- revoke (owner/admin only)
POST   /api/workspaces/{id}/members/transfer-owner  -- transfer ownership (current owner only, M10 step 2)

Tenancy (v0.2 CL-11): the workspace id comes from the URL path and
is checked against merchant_memberships for the authenticated user
before any data is returned; a non-member gets 404, never a leak.
Request bodies do not accept a merchant_id (extra="forbid").

Rules live in app/services/member_service.py -- see its module
docstring for the CL-20 advisory-lock protocol, the last-owner
guard (OD-11), and why revocation denies workspace access
immediately without deleting any session row. State-changing
requests require the CSRF double-submit check.

Path choice for transfer-owner: a collection-level POST, like
/invite, not /{membership_id}/transfer -- the transfer is about a
PERSON (identified by user_id in the body), not about a membership
row, and the acting owner is identified by their session, not by a
membership id in the URL. The body carries the target member's
user_id (the members list already carries user_id for every row),
so the frontend needs no extra lookup.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_csrf
from app.db.auth_models import User
from app.db.session import get_db
from app.services import member_service
from app.services.member_service import (
    AlreadyMemberError,
    MemberValidationError,
    MembershipNotFoundError,
    NotManagerError,
)

router = APIRouter(prefix="/workspaces/{workspace_id}/members", tags=["members"])


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    role: str = Field(min_length=1, max_length=20)


class ChangeRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=20)


class TransferOwnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The target member's USER id (not a membership id). The members
    # list carries user_id for every row, so the frontend needs no
    # extra lookup; a transfer is about a person, not a row.
    user_id: uuid.UUID


class MemberOut(BaseModel):
    id: str
    user_id: str
    email: str | None
    display_name: str | None
    role: str
    status: str
    invited_by: str | None
    created_at: str | None
    updated_at: str | None


def _map_errors(fn, *args, **kwargs):
    """Run a member_service call, mapping its errors to HTTP."""
    try:
        return fn(*args, **kwargs)
    except MembershipNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except NotManagerError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except AlreadyMemberError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except MemberValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{exc.reason}: {exc.message}",
        )


@router.get("")
def list_members(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[MemberOut]:
    members = _map_errors(member_service.list_members, db, user, workspace_id)
    return [MemberOut(**m) for m in members]


@router.post("/invite", dependencies=[Depends(require_csrf)])
def invite_member(
    workspace_id: uuid.UUID,
    body: InviteRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    member, created = _map_errors(
        member_service.invite_member, db, user, workspace_id, body.email, body.role
    )
    # A new invitation is 201; a re-invite of an already-invited (or
    # previously revoked) membership is not a creation: 200.
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=MemberOut(**member).model_dump(),
    )


@router.post("/{membership_id}/accept", dependencies=[Depends(require_csrf)])
def accept_invitation(
    workspace_id: uuid.UUID,
    membership_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MemberOut:
    member = _map_errors(
        member_service.accept_invitation, db, user, workspace_id, membership_id
    )
    return MemberOut(**member)


@router.patch("/{membership_id}/role", dependencies=[Depends(require_csrf)])
def change_role(
    workspace_id: uuid.UUID,
    membership_id: uuid.UUID,
    body: ChangeRoleRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MemberOut:
    member = _map_errors(
        member_service.change_role, db, user, workspace_id, membership_id, body.role
    )
    return MemberOut(**member)


@router.post("/{membership_id}/revoke", dependencies=[Depends(require_csrf)])
def revoke_member(
    workspace_id: uuid.UUID,
    membership_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MemberOut:
    member = _map_errors(
        member_service.revoke_member, db, user, workspace_id, membership_id
    )
    return MemberOut(**member)


@router.post("/transfer-owner", dependencies=[Depends(require_csrf)])
def transfer_owner(
    workspace_id: uuid.UUID,
    body: TransferOwnerRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MemberOut:
    """Transfer ownership to another active member. Only the current
    owner may call it (403 otherwise); non-members get 404. The old
    owner becomes an admin (see transfer_owner in member_service.py
    for why). Returns the NEW owner's membership."""
    member = _map_errors(
        member_service.transfer_owner, db, user, workspace_id, body.user_id
    )
    return MemberOut(**member)
