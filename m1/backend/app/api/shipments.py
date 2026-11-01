"""Orders + shipments creation API (M1, Milestone 8, part 1).

POST /api/workspaces/{id}/orders -- create an order (201).
GET  /api/workspaces/{id}/orders -- list orders (paginated, newest first).
GET  /api/workspaces/{id}/orders/{order_id} -- one order.
POST /api/workspaces/{id}/orders/{order_id}/shipments -- create a
  shipment under one order (201).
GET  /api/workspaces/{id}/shipments -- list shipments (paginated,
  newest first; filter by order_id and/or tracking_id).
GET  /api/workspaces/{id}/shipments/{shipment_id} -- one shipment.

Money is integer poisha: StrictInt at the boundary, so a float or a
string amount is 422, never silently coerced. The merchant_id is
derived ONLY from the workspace path parameter after the membership
check; request bodies do not accept a merchant_id field
(extra="forbid"). Expected collectible COD is derived server-side
from the confirmed shipment outcome (see
app/services/shipment_service.py) -- the client cannot set it.

Creation only: there are deliberately no PUT / PATCH / DELETE routes
for orders or shipments.

Tenancy: the workspace must belong to the caller (membership check);
a non-member gets 404, never a leak. Creation is state-changing, so
it requires the CSRF double-submit check.
"""

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_csrf, require_write_role
from app.db.auth_models import User
from app.db.models import OUTCOME_SOURCES, SHIPMENT_OUTCOMES
from app.db.session import get_db
from app.services import shipment_service
from app.services import workspace_service

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["orders", "shipments"])


def _require_membership(db: Session, user: User, workspace_id: uuid.UUID):
    membership = workspace_service.get_membership(db, user.id, workspace_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    return membership


# ---------------------------------------------------------------------------
# Request bodies (extra="forbid": no merchant_id, no expected_collectible,
# no source -- those are all server-derived)
# ---------------------------------------------------------------------------

class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_order_ref: str = Field(min_length=1, max_length=500)
    order_cod_poisha: StrictInt | None = Field(default=None, ge=0, le=10**15)
    currency: str = "BDT"
    ordered_at: datetime | None = None

    @field_validator("merchant_order_ref", mode="before")
    @classmethod
    def _strip_ref(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("currency")
    @classmethod
    def _bdt_only(cls, value):
        if value != "BDT":
            raise ValueError("currency must be 'BDT'.")
        return value


class CreateShipmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracking_id: str | None = Field(default=None, max_length=200)
    outcome: str = "unknown"
    outcome_source: str | None = None
    is_cod_bearing: StrictBool = False
    amount_to_collect_poisha: StrictInt | None = Field(default=None, ge=0, le=10**15)
    partial_amount_evidence_ref: str | None = Field(default=None, max_length=500)
    merchant_courier_account_id: uuid.UUID | None = None
    shipped_on: date | None = None
    is_superseded: StrictBool = False
    superseded_by_shipment_id: uuid.UUID | None = None
    superseded_reason: str | None = Field(default=None, max_length=500)

    @field_validator("outcome")
    @classmethod
    def _known_outcome(cls, value):
        if value not in SHIPMENT_OUTCOMES:
            raise ValueError(f"outcome must be one of {list(SHIPMENT_OUTCOMES)}.")
        return value

    @field_validator("outcome_source")
    @classmethod
    def _known_outcome_source(cls, value):
        if value is not None and value not in OUTCOME_SOURCES:
            raise ValueError(f"outcome_source must be one of {list(OUTCOME_SOURCES)}.")
        return value

    @field_validator("tracking_id", "partial_amount_evidence_ref", "superseded_reason",
                     mode="before")
    @classmethod
    def _strip_optional_text(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@router.post("/orders", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_csrf)])
def create_order(
    workspace_id: uuid.UUID,
    body: CreateOrderRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_write_role(_require_membership(db, user, workspace_id))
    try:
        return shipment_service.create_order(
            db,
            user=user,
            merchant_id=workspace_id,
            merchant_order_ref=body.merchant_order_ref,
            order_cod_poisha=body.order_cod_poisha,
            currency=body.currency,
            ordered_at=body.ordered_at,
        )
    except shipment_service.ShipmentNotFoundError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    except shipment_service.ShipmentValidationError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(exc), "reason": exc.reason},
        )


@router.get("/orders")
def list_orders(
    workspace_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    items, total = shipment_service.list_orders(db, workspace_id, limit=limit, offset=offset)
    return {"orders": items, "total": total, "limit": limit, "offset": offset}


@router.get("/orders/{order_id}")
def get_order(
    workspace_id: uuid.UUID,
    order_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    order = shipment_service.get_order(db, workspace_id, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    return order


# ---------------------------------------------------------------------------
# Shipments
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/shipments", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_csrf)])
def create_shipment(
    workspace_id: uuid.UUID,
    order_id: uuid.UUID,
    body: CreateShipmentRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_write_role(_require_membership(db, user, workspace_id))
    try:
        return shipment_service.create_shipment(
            db,
            user=user,
            merchant_id=workspace_id,
            order_id=order_id,
            tracking_id=body.tracking_id,
            outcome=body.outcome,
            outcome_source=body.outcome_source,
            is_cod_bearing=body.is_cod_bearing,
            amount_to_collect_poisha=body.amount_to_collect_poisha,
            partial_amount_evidence_ref=body.partial_amount_evidence_ref,
            merchant_courier_account_id=body.merchant_courier_account_id,
            shipped_on=body.shipped_on,
            is_superseded=body.is_superseded,
            superseded_by_shipment_id=body.superseded_by_shipment_id,
            superseded_reason=body.superseded_reason,
        )
    except shipment_service.ShipmentNotFoundError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    except shipment_service.ShipmentValidationError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(exc), "reason": exc.reason},
        )


@router.get("/shipments")
def list_shipments(
    workspace_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    order_id: uuid.UUID | None = None,
    tracking_id: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    try:
        items, total = shipment_service.list_shipments(
            db, workspace_id, limit=limit, offset=offset,
            order_id=order_id, tracking_id=tracking_id,
        )
    except shipment_service.ShipmentNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    return {"shipments": items, "total": total, "limit": limit, "offset": offset}


@router.get("/shipments/{shipment_id}")
def get_shipment(
    workspace_id: uuid.UUID,
    shipment_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_membership(db, user, workspace_id)
    shipment = shipment_service.get_shipment(db, workspace_id, shipment_id)
    if shipment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shipment not found.")
    return shipment
