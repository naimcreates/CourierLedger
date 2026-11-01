"""Orders + shipments creation API service (M1 Milestone 8, part 1).

Creates merchant orders and their shipments. All money is integer
poisha; the API never stores an invented amount or an implicit zero
where the spec says NULL.

SCOPE AND HONESTY RULES (V2.1):
- The tenant (merchant_id) is derived ONLY from the workspace path
  parameter after the membership check. Request bodies do not accept
  a merchant_id field (Pydantic extra="forbid"); a client-supplied
  merchant_id is never trusted for scoping.
- Expected collectible COD is DERIVED server-side from the confirmed
  shipment outcome (see derive_expected_collectible); the client
  cannot set expected_collectible_cod_poisha directly:
    delivered            -> amount to collect (the merchant-stated
                            amount when given, else the order COD;
                            NULL when neither is known)
    partially_delivered  -> the documented partial amount ONLY
                            (amount + evidence ref both present),
                            else NULL -- never the full order COD
    returned / cancelled -> verified 0
    superseded           -> verified 0
    in_transit / unknown -> NULL
  A merchant-stated amount that differs from the order total is a
  split: the shipment is recorded with the stated amount and flagged
  needs_review (split_amount_differs_from_order_total) -- never
  silently assumed to carry the full order COD.
- MVP one COD-bearing shipment per order: creating a second active
  COD-bearing shipment for the same order is 422. This is checked in
  the service and enforced again by the partial unique index
  uq_shipments_one_active_cod_bearing_per_order (a race that trips
  the index is mapped back to the same 422). A superseded shipment
  is excluded on both sides: it is recorded AFTER its successor
  exists (the successor must be a shipment of the same order), and
  recording it never blocks or counts as the active COD-bearing
  shipment.
- needs_review is COMPUTED from stored fields at read time (there is
  no needs-review column on shipments): it is True exactly when the
  expected collectible could not be fixed from confirmed information
  or the stated amount conflicts with the order total. The reason is
  returned alongside, so a quarantined shipment is visible, never
  silent.
- Insert-only creation: there are deliberately no PUT / PATCH /
  DELETE routes for orders or shipments (append-only posture; the
  runtime role does hold UPDATE on these tables for outcome
  corrections, but no update endpoint exists yet).
- Transaction discipline: create_*() owns one transaction and
  commits on success; the caller must roll back on an exception.
  Commit clears the transaction-local tenant context, so the row is
  re-read only after re-asserting it from the known merchant id
  (B-1 pattern).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    OUTCOME_SOURCES,
    SHIPMENT_OUTCOMES,
    MerchantCourierAccount,
    Order,
    Shipment,
)
from app.db.session import set_tenant_context
from app.services import csv_import  # tracking normalisation, shared with M4
from app.services.workspace_service import get_membership

# Reasons returned as needs_review_reason (machine-readable, stable).
REASON_OUTCOME_NOT_CONFIRMED = "outcome_not_confirmed"
REASON_ORDER_COD_UNKNOWN = "order_cod_unknown"
REASON_SPLIT_AMOUNT = "split_amount_differs_from_order_total"
REASON_PARTIAL_NOT_DOCUMENTED = "partial_amount_not_documented"

# Outcomes for which an amount to collect is meaningless: supplying
# one contradicts the outcome, so it is rejected (422) rather than
# stored or silently ignored.
_AMOUNTLESS_OUTCOMES = ("returned", "cancelled", "in_transit", "unknown")


class ShipmentNotFoundError(Exception):
    """The order / shipment / workspace is not in this workspace
    (API maps to 404). No existence is leaked to non-members."""


class ShipmentValidationError(ValueError):
    """The request cannot be honoured (API maps to 422).

    Carries a machine-readable ``reason``."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def derive_expected_collectible(
    *,
    outcome: str,
    order_cod_poisha: int | None,
    amount_to_collect_poisha: int | None,
    partial_amount_evidence_ref: str | None,
    is_superseded: bool,
) -> tuple[int | None, bool, str | None]:
    """Derive (expected_collectible_poisha, needs_review, reason).

    Pure function of confirmed information only; used by
    create_shipment and by the response builder so the stored value
    and the reported flag can never disagree. See the module docstring
    for the per-outcome rules."""
    if is_superseded:
        # V2.1: a superseded shipment collects nothing -- verified 0,
        # whatever its outcome was.
        return 0, False, None
    if outcome == "delivered":
        if amount_to_collect_poisha is not None:
            if order_cod_poisha is not None and amount_to_collect_poisha != order_cod_poisha:
                return (
                    amount_to_collect_poisha,
                    True,
                    REASON_SPLIT_AMOUNT,
                )
            return amount_to_collect_poisha, False, None
        if order_cod_poisha is None:
            return None, True, REASON_ORDER_COD_UNKNOWN
        return order_cod_poisha, False, None
    if outcome == "partially_delivered":
        if amount_to_collect_poisha is not None and partial_amount_evidence_ref:
            return amount_to_collect_poisha, False, None
        # Documented partial amount only; a bare number without
        # evidence, or evidence without a number, fixes nothing.
        return None, True, REASON_PARTIAL_NOT_DOCUMENTED
    if outcome in ("returned", "cancelled"):
        return 0, False, None
    # in_transit / unknown: the outcome is not confirmed, so no
    # collectible amount can be fixed -- NULL, flagged for review.
    return None, True, REASON_OUTCOME_NOT_CONFIRMED


def _coerce_uuid(value):
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _require_membership(db: Session, user, merchant_id: uuid.UUID):
    membership = get_membership(db, user.id, merchant_id)
    if membership is None:
        raise ShipmentNotFoundError("Workspace not found.")
    return membership


def _order_dict(order: Order) -> dict:
    return {
        "order_id": str(order.id),
        "merchant_id": str(order.merchant_id),
        "merchant_order_ref": order.merchant_order_ref,
        "order_cod_poisha": order.order_cod_poisha,
        "currency": order.currency,
        "ordered_at": order.ordered_at.isoformat() if order.ordered_at else None,
        "source": order.source,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
    }


def _shipment_dict(shipment: Shipment, order_cod_poisha: int | None) -> dict:
    expected, needs_review, reason = derive_expected_collectible(
        outcome=shipment.outcome,
        order_cod_poisha=order_cod_poisha,
        amount_to_collect_poisha=(
            shipment.expected_collectible_cod_poisha
            if shipment.outcome in ("delivered", "partially_delivered")
            and not shipment.is_superseded
            else None
        ),
        partial_amount_evidence_ref=shipment.partial_amount_evidence_ref,
        is_superseded=shipment.is_superseded,
    )
    # The stored value must equal the derived value for the cases
    # where the derivation is a pure function of stored fields. For
    # delivered-with-stated-amount and partial-with-evidence the
    # stored value IS the input amount, so they agree by
    # construction; for delivered-without-amount the stored value is
    # the order COD, which the derivation also returns. Report the
    # stored value (the record of what was fixed at creation) and
    # the derived flag/reason together.
    return {
        "shipment_id": str(shipment.id),
        "merchant_id": str(shipment.merchant_id),
        "order_id": str(shipment.order_id),
        "merchant_courier_account_id": (
            str(shipment.merchant_courier_account_id)
            if shipment.merchant_courier_account_id
            else None
        ),
        "tracking_id": shipment.tracking_id_raw,
        "tracking_id_normalised": shipment.tracking_id_normalised,
        "outcome": shipment.outcome,
        "outcome_source": shipment.outcome_source,
        "expected_collectible_cod_poisha": shipment.expected_collectible_cod_poisha,
        "expected_collectible_derived_poisha": expected,
        "partial_amount_evidence_ref": shipment.partial_amount_evidence_ref,
        "is_cod_bearing": shipment.is_cod_bearing,
        "is_superseded": shipment.is_superseded,
        "superseded_by_shipment_id": (
            str(shipment.superseded_by_shipment_id)
            if shipment.superseded_by_shipment_id
            else None
        ),
        "superseded_reason": shipment.superseded_reason,
        "shipped_on": shipment.shipped_on.isoformat() if shipment.shipped_on else None,
        "needs_review": needs_review,
        "needs_review_reason": reason,
        "created_at": shipment.created_at.isoformat() if shipment.created_at else None,
        "updated_at": shipment.updated_at.isoformat() if shipment.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def create_order(
    db: Session,
    *,
    user,
    merchant_id,
    merchant_order_ref: str,
    order_cod_poisha: int | None,
    currency: str = "BDT",
    ordered_at: datetime | None = None,
) -> dict:
    """Create an order in this workspace. Commits on success.

    Raises ShipmentNotFoundError (workspace not the caller's) or
    ShipmentValidationError (422: bad ref / money)."""
    merchant_id = _coerce_uuid(merchant_id)
    if merchant_id is None:
        raise ShipmentNotFoundError("Workspace not found.")
    _require_membership(db, user, merchant_id)
    ref = (merchant_order_ref or "").strip()
    if not ref:
        raise ShipmentValidationError(
            "merchant_order_ref must not be empty.", reason="empty_order_ref"
        )
    if order_cod_poisha is not None and order_cod_poisha < 0:
        raise ShipmentValidationError(
            "order_cod_poisha must be >= 0.", reason="negative_order_cod"
        )
    set_tenant_context(db, merchant_id)
    order = Order(
        merchant_id=merchant_id,
        merchant_order_ref=ref,
        order_cod_poisha=order_cod_poisha,
        currency=currency,
        ordered_at=ordered_at,
        source="manual",
    )
    db.add(order)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise
    # Commit clears the transaction-local tenant context; re-assert
    # it before refresh (B-1 pattern).
    set_tenant_context(db, merchant_id)
    db.refresh(order)
    return _order_dict(order)


def list_orders(db: Session, merchant_id, limit: int = 20, offset: int = 0):
    """(items, total): orders newest first. Raises
    ShipmentNotFoundError when the workspace is not the caller's
    (the API checks membership first; this is defence in depth)."""
    merchant_id = _coerce_uuid(merchant_id)
    if merchant_id is None:
        raise ShipmentNotFoundError("Workspace not found.")
    set_tenant_context(db, merchant_id)
    total = db.execute(
        select(func.count()).select_from(Order).where(Order.merchant_id == merchant_id)
    ).scalar_one()
    orders = db.execute(
        select(Order)
        .where(Order.merchant_id == merchant_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    return [_order_dict(o) for o in orders], total


def get_order(db: Session, merchant_id, order_id) -> dict | None:
    """One order dict, or None when it is not in this workspace."""
    merchant_id = _coerce_uuid(merchant_id)
    order_id = _coerce_uuid(order_id)
    if merchant_id is None or order_id is None:
        return None
    set_tenant_context(db, merchant_id)
    order = db.get(Order, order_id)
    if order is None or order.merchant_id != merchant_id:
        return None
    return _order_dict(order)


# ---------------------------------------------------------------------------
# Shipments
# ---------------------------------------------------------------------------

def create_shipment(
    db: Session,
    *,
    user,
    merchant_id,
    order_id,
    tracking_id: str | None = None,
    outcome: str = "unknown",
    outcome_source: str | None = None,
    is_cod_bearing: bool = False,
    amount_to_collect_poisha: int | None = None,
    partial_amount_evidence_ref: str | None = None,
    merchant_courier_account_id=None,
    shipped_on: date | None = None,
    is_superseded: bool = False,
    superseded_by_shipment_id=None,
    superseded_reason: str | None = None,
) -> dict:
    """Create a shipment under one order in this workspace. Commits
    on success. Raises ShipmentNotFoundError (workspace / order not
    the caller's) or ShipmentValidationError (422)."""
    merchant_id = _coerce_uuid(merchant_id)
    order_id = _coerce_uuid(order_id)
    merchant_courier_account_id = _coerce_uuid(merchant_courier_account_id)
    superseded_by_shipment_id = _coerce_uuid(superseded_by_shipment_id)
    if merchant_id is None or order_id is None:
        raise ShipmentNotFoundError("Order not found.")
    _require_membership(db, user, merchant_id)
    if outcome not in SHIPMENT_OUTCOMES:
        raise ShipmentValidationError(
            f"Unknown outcome: {outcome!r}.", reason="unknown_outcome"
        )
    if outcome_source is not None and outcome_source not in OUTCOME_SOURCES:
        raise ShipmentValidationError(
            f"Unknown outcome_source: {outcome_source!r}.", reason="unknown_outcome_source"
        )
    if amount_to_collect_poisha is not None and amount_to_collect_poisha < 0:
        raise ShipmentValidationError(
            "amount_to_collect_poisha must be >= 0.", reason="negative_amount"
        )
    # An amount to collect is meaningless for these outcomes (and for
    # a superseded shipment, whose collectible is verified 0):
    # reject the contradiction instead of storing or ignoring it.
    if amount_to_collect_poisha is not None and (
        is_superseded or outcome in _AMOUNTLESS_OUTCOMES
    ):
        raise ShipmentValidationError(
            "amount_to_collect_poisha is not applicable for "
            f"outcome {outcome!r}{' (superseded)' if is_superseded else ''}.",
            reason="amount_not_applicable_for_outcome",
        )
    if is_superseded and superseded_by_shipment_id is None:
        raise ShipmentValidationError(
            "A superseded shipment must name superseded_by_shipment_id.",
            reason="superseded_requires_successor",
        )
    if not is_superseded and superseded_by_shipment_id is not None:
        raise ShipmentValidationError(
            "superseded_by_shipment_id requires is_superseded=true.",
            reason="successor_requires_superseded_flag",
        )

    set_tenant_context(db, merchant_id)
    order = db.get(Order, order_id)
    if order is None or order.merchant_id != merchant_id:
        raise ShipmentNotFoundError("Order not found.")

    if merchant_courier_account_id is not None:
        account = db.get(MerchantCourierAccount, merchant_courier_account_id)
        if account is None or account.merchant_id != merchant_id:
            raise ShipmentValidationError(
                "Courier account not found in this workspace.",
                reason="courier_account_not_found",
            )
    successor = None
    if superseded_by_shipment_id is not None:
        successor = db.get(Shipment, superseded_by_shipment_id)
        if successor is None or successor.merchant_id != merchant_id:
            raise ShipmentValidationError(
                "superseded_by_shipment_id does not name a shipment in this workspace.",
                reason="successor_not_found",
            )
        if successor.order_id != order.id:
            raise ShipmentValidationError(
                "superseded_by_shipment_id must name a shipment of the same order.",
                reason="successor_different_order",
            )

    # MVP one COD-bearing shipment per order (service check; the
    # partial unique index enforces it again at the database). A
    # superseded shipment is excluded on both sides: recording a
    # superseded COD-bearing shipment never blocks, and never counts
    # as the order's active COD-bearing shipment.
    if is_cod_bearing and not is_superseded:
        existing_cod = db.execute(
            select(Shipment.id).where(
                (Shipment.merchant_id == merchant_id)
                & (Shipment.order_id == order.id)
                & (Shipment.is_cod_bearing.is_(True))
                & (Shipment.is_superseded.is_(False))
            )
        ).scalar_one_or_none()
        if existing_cod is not None:
            raise ShipmentValidationError(
                "This order already has an active COD-bearing shipment "
                "(MVP: one COD-bearing shipment per order).",
                reason="second_cod_bearing_shipment",
            )

    expected, _needs_review, _reason = derive_expected_collectible(
        outcome=outcome,
        order_cod_poisha=order.order_cod_poisha,
        amount_to_collect_poisha=amount_to_collect_poisha,
        partial_amount_evidence_ref=(
            (partial_amount_evidence_ref or "").strip() or None
        ),
        is_superseded=is_superseded,
    )

    tracking_raw = (tracking_id or "").strip() or None
    shipment = Shipment(
        merchant_id=merchant_id,
        order_id=order.id,
        merchant_courier_account_id=merchant_courier_account_id,
        tracking_id_raw=tracking_raw,
        tracking_id_normalised=csv_import.normalise_tracking(tracking_raw),
        outcome=outcome,
        outcome_source=outcome_source,
        expected_collectible_cod_poisha=expected,
        partial_amount_evidence_ref=(
            (partial_amount_evidence_ref or "").strip() or None
        ),
        is_cod_bearing=is_cod_bearing,
        is_superseded=is_superseded,
        superseded_by_shipment_id=superseded_by_shipment_id,
        superseded_reason=(superseded_reason or "").strip() or None,
        shipped_on=shipped_on,
    )
    db.add(shipment)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # A race with the one-active-COD-bearing partial unique index
        # maps back to the same 422 the service check gives.
        if "uq_shipments_one_active_cod_bearing_per_order" in str(exc.orig or exc):
            raise ShipmentValidationError(
                "This order already has an active COD-bearing shipment "
                "(MVP: one COD-bearing shipment per order).",
                reason="second_cod_bearing_shipment",
            ) from exc
        raise
    set_tenant_context(db, merchant_id)
    db.refresh(shipment)
    return _shipment_dict(shipment, order.order_cod_poisha)


def list_shipments(
    db: Session,
    merchant_id,
    limit: int = 20,
    offset: int = 0,
    *,
    order_id=None,
    tracking_id: str | None = None,
):
    """(items, total): shipments newest first, optionally filtered by
    order_id and/or tracking_id (normalised, so ' trk-1001 ' matches
    'TRK-1001'). Raises ShipmentNotFoundError for a bad workspace id
    or an order_id outside this workspace."""
    merchant_id = _coerce_uuid(merchant_id)
    order_id = _coerce_uuid(order_id) if order_id is not None else None
    if merchant_id is None:
        raise ShipmentNotFoundError("Workspace not found.")
    set_tenant_context(db, merchant_id)
    conditions = [Shipment.merchant_id == merchant_id]
    order_cod: dict = {}
    if order_id is not None:
        order = db.get(Order, order_id)
        if order is None or order.merchant_id != merchant_id:
            raise ShipmentNotFoundError("Order not found.")
        conditions.append(Shipment.order_id == order_id)
    normalised = csv_import.normalise_tracking(tracking_id)
    if normalised is not None:
        conditions.append(Shipment.tracking_id_normalised == normalised)
    total = db.execute(
        select(func.count()).select_from(Shipment).where(*conditions)
    ).scalar_one()
    shipments = db.execute(
        select(Shipment)
        .where(*conditions)
        .order_by(Shipment.created_at.desc(), Shipment.id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    order_ids = {s.order_id for s in shipments}
    if order_ids:
        for o in db.execute(
            select(Order).where(Order.id.in_(order_ids))
        ).scalars().all():
            order_cod[o.id] = o.order_cod_poisha
    return (
        [_shipment_dict(s, order_cod.get(s.order_id)) for s in shipments],
        total,
    )


def get_shipment(db: Session, merchant_id, shipment_id) -> dict | None:
    """One shipment dict, or None when it is not in this workspace."""
    merchant_id = _coerce_uuid(merchant_id)
    shipment_id = _coerce_uuid(shipment_id)
    if merchant_id is None or shipment_id is None:
        return None
    set_tenant_context(db, merchant_id)
    shipment = db.get(Shipment, shipment_id)
    if shipment is None or shipment.merchant_id != merchant_id:
        return None
    order = db.get(Order, shipment.order_id)
    order_cod = order.order_cod_poisha if order is not None else None
    return _shipment_dict(shipment, order_cod)
