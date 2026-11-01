"""Merchant courier accounts API (M1, Milestone 4 support).

A courier account links one workspace (merchant) to one global
courier code (steadfast / pathao / paperfly / redx / ecourier --
codes only, from app/db/seed_couriers.sql; no rates or templates
live here). An import always belongs to one account: that is what
ties statement rows to a courier and scopes the weak-fallback
match. Creating an account records only the merchant's own account
reference/label -- never a courier password, API key or secret.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_csrf
from app.db.auth_models import User
from app.db.models import Courier, MerchantCourierAccount
from app.db.session import get_db, set_tenant_context
from app.services import workspace_service

router = APIRouter(prefix="/workspaces/{workspace_id}/courier-accounts", tags=["courier-accounts"])


class CreateAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    courier_code: str = Field(min_length=1, max_length=50)
    merchant_account_ref: str | None = Field(default=None, max_length=200)
    display_label: str | None = Field(default=None, max_length=200)


class AccountOut(BaseModel):
    id: str
    courier_code: str
    merchant_account_ref: str | None
    display_label: str | None
    status: str


def _require_membership(db: Session, user: User, workspace_id: uuid.UUID):
    membership = workspace_service.get_membership(db, user.id, workspace_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    return membership


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_csrf)])
def create_account(
    workspace_id: uuid.UUID,
    body: CreateAccountRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AccountOut:
    _require_membership(db, user, workspace_id)
    courier = db.execute(
        select(Courier).where(Courier.code == body.courier_code.strip().lower())
    ).scalar_one_or_none()
    if courier is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown courier code: {body.courier_code!r}.",
        )
    set_tenant_context(db, workspace_id)
    account = MerchantCourierAccount(
        merchant_id=workspace_id,
        courier_id=courier.id,
        merchant_account_ref=body.merchant_account_ref,
        display_label=body.display_label,
    )
    db.add(account)
    db.commit()
    return AccountOut(
        id=str(account.id),
        courier_code=courier.code,
        merchant_account_ref=account.merchant_account_ref,
        display_label=account.display_label,
        status=account.status,
    )


@router.get("")
def list_accounts(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AccountOut]:
    _require_membership(db, user, workspace_id)
    set_tenant_context(db, workspace_id)
    rows = db.execute(
        select(MerchantCourierAccount, Courier.code)
        .join(Courier, Courier.id == MerchantCourierAccount.courier_id)
        .where(MerchantCourierAccount.merchant_id == workspace_id)
        .order_by(MerchantCourierAccount.created_at)
    ).all()
    return [
        AccountOut(
            id=str(a.id),
            courier_code=code,
            merchant_account_ref=a.merchant_account_ref,
            display_label=a.display_label,
            status=a.status,
        )
        for a, code in rows
    ]
