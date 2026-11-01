"""Thin HTTP adapter over the pure-Python domain engine (M1 Milestone 3).

Routes are declared with their full ``/api`` paths; the coordinator mounts
this router as-is in ``main.py`` (do NOT add another ``/api`` prefix there).

This endpoint does NOT persist runs to the database in this session — there
is no run store yet. POST /api/reconcile returns the computed result only.

Persisted runs (M1 Milestone 6) ALWAYS require an import_batch_id:
``app.services.run_service.create_run_from_import`` creates a run only
from a real M4 import batch, via
``POST /api/workspaces/{id}/imports/{batch_id}/runs``. This endpoint
stays compute-only by design, and no synthetic import batch may be
fabricated to work around that — a run without a real import batch
does not exist in the approved schema
(``reconciliation_runs.import_batch_id`` is NOT NULL).

Auth (Phase 1, B-F4/C-1.1): POST /api/reconcile and GET /api/rules
require a live session (get_current_user) and the CSRF double-submit
check on POST. The request body is a strict Pydantic model — StrictInt
money fields with an upper bound, enums, extra="forbid" — so malformed
input is rejected with 422 instead of being cleaned into a
valid-looking result by the engine (C-1.2). The engine itself is
untouched (V2.2.1); the model only shapes what reaches it.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from app.api.deps import get_current_user, require_csrf
from app.db.auth_models import User
from app.domain.engine import FIXTURES, reconcile_batch

router = APIRouter(tags=["reconcile"])

# Upper bound for any money amount at the API boundary (C-1.5):
# 10**15 poisha ≈ ৳10^13. Anything larger is a 422, never a DB
# bigint overflow or a 500.
MAX_MONEY_POISHA = 10**15


class ReconcileFeeLine(BaseModel):
    """One fee line as the engine's parse_money() expects it."""

    model_config = ConfigDict(extra="forbid")

    fee_type: str = Field(max_length=100)
    raw: str | int | float | None = None
    column_present: StrictBool = True


class ReconcileShipment(BaseModel):
    """One shipment for POST /api/reconcile. Every field the engine
    reads, typed strictly; anything else is rejected (extra="forbid").
    Money is StrictInt poisha, never a bool/string/float, with the
    shared upper bound. ``None`` means unknown — never verified zero."""

    model_config = ConfigDict(extra="forbid")

    merchant_order_id: str | None = Field(default=None, max_length=500)
    tracking_id: str | None = Field(default=None, max_length=200)
    courier_fixture: str | None = Field(default=None, max_length=200)
    outcome: Literal["delivered", "partially_delivered", "returned", "cancelled", "superseded", "unknown"] = "unknown"
    amount_to_collect_poisha: StrictInt | None = Field(default=None, ge=0, le=MAX_MONEY_POISHA)
    documented_partial_poisha: StrictInt | None = Field(default=None, ge=0, le=MAX_MONEY_POISHA)
    reported_collected_poisha: StrictInt | None = Field(default=None, ge=0, le=MAX_MONEY_POISHA)
    expected_delivery_fee_poisha: StrictInt | None = Field(default=None, ge=0, le=MAX_MONEY_POISHA)
    reported_delivery_fee_poisha: StrictInt | None = Field(default=None, ge=0, le=MAX_MONEY_POISHA)
    contractual_delivery_fee_poisha: StrictInt | None = Field(default=None, ge=0, le=MAX_MONEY_POISHA)
    contractual_delivery_fee_confirmed: StrictBool = False
    contractual_delivery_fee_evidence: str | None = Field(default=None, max_length=500)
    cod_fee_base: Literal["full_cod", "cod_minus_delivery_fee", "reported_collected"] | None = None
    cod_fee_rate_bp: StrictInt | None = Field(default=None, ge=0, le=10000)
    other_fees_poisha: StrictInt | None = Field(default=None, ge=0, le=MAX_MONEY_POISHA)
    return_fee_poisha: StrictInt | None = Field(default=None, ge=0, le=MAX_MONEY_POISHA)
    excluded_tax_poisha: StrictInt | None = Field(default=None, ge=0, le=MAX_MONEY_POISHA)
    reported_excluded_tax_poisha: StrictInt | None = Field(default=None, ge=0, le=MAX_MONEY_POISHA)
    reported_deductions_total_poisha: StrictInt | None = Field(default=None, ge=0, le=MAX_MONEY_POISHA)
    reported_payout_poisha: StrictInt | None = Field(default=None, ge=0, le=MAX_MONEY_POISHA)
    fee_lines: list[ReconcileFeeLine] | None = Field(default=None, max_length=100)
    vat_mode: str | None = Field(default=None, max_length=50)
    vat_formula: Literal["rate_on_net", "rate_on_gross"] | None = None
    vat_rate_bp: StrictInt | None = Field(default=None, ge=0, le=10000)


class ReconcileRequest(BaseModel):
    """POST /api/reconcile body: shipments to reconcile + tolerance."""

    model_config = ConfigDict(extra="forbid")

    shipments: list[ReconcileShipment] = Field(default_factory=list, max_length=1000)
    tolerance_poisha: StrictInt = Field(default=0, ge=0, le=MAX_MONEY_POISHA)


@router.post("/api/reconcile")
async def post_reconcile(
    body: ReconcileRequest,
    _current_user: User = Depends(get_current_user),
    # Declared after the user dependency on purpose: an unauthenticated
    # caller gets 401 from get_current_user before the CSRF check runs.
    _csrf: None = Depends(require_csrf),
):
    """Reconcile a batch of shipments. Computed result only — nothing is
    persisted. Persisted runs are created from a real import batch via
    POST /api/workspaces/{id}/imports/{batch_id}/runs (M6); this
    endpoint stays compute-only by design."""
    # exclude_none=True: the engine distinguishes an absent key from
    # an explicit None in one place (reported_excluded_tax_poisha
    # falls back to excluded_tax_poisha only when absent), so None
    # fields must be dropped, not passed through.
    shipments = [s.model_dump(exclude_none=True) for s in body.shipments]
    return reconcile_batch(shipments, body.tolerance_poisha)


@router.get("/api/rules")
async def get_rules(_current_user: User = Depends(get_current_user)):
    """Return the synthetic fixture rules (SYNTHETIC FIXTURE — not real
    courier contracts). Authenticated only (Phase 1, B-F4)."""
    return {"fixtures": list(FIXTURES.values())}
