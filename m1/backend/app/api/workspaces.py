"""Workspace (merchant) API (M1).

Tenancy rule (v0.2 CL-11): tenant scope comes ONLY from the
authenticated user's server-side membership. Request bodies do not
accept a merchant_id at all (extra="forbid"); path ids are verified
against merchant_memberships before any data is returned.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_csrf
from app.db.auth_models import User
from app.db.session import get_db
from app.services import workspace_service

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class CreateWorkspaceRequest(BaseModel):
    # NOTE: no merchant_id field by design. extra="forbid" rejects any
    # client-supplied merchant_id (or other unknown field) with 422.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    trading_name: str | None = Field(default=None, max_length=200)


class WorkspaceOut(BaseModel):
    id: str
    name: str
    trading_name: str | None
    status: str
    role: str
    created_at: str


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_csrf)])
def create_workspace(
    body: CreateWorkspaceRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkspaceOut:
    merchant = workspace_service.create_workspace(db, user, body.name, body.trading_name)
    return WorkspaceOut(
        id=str(merchant.id),
        name=merchant.name,
        trading_name=merchant.trading_name,
        status=merchant.status,
        role="owner",
        created_at=merchant.created_at.isoformat(),
    )


@router.get("")
def list_workspaces(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[WorkspaceOut]:
    return [WorkspaceOut(**w) for w in workspace_service.list_workspaces_for_user(db, user)]


@router.get("/{workspace_id}")
def get_workspace(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkspaceOut:
    result = workspace_service.get_workspace_for_user(db, user, workspace_id)
    if result is None:
        # 404 for non-members as well as for missing ids: existence
        # of another tenant's workspace is not leaked.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    return WorkspaceOut(**result)
