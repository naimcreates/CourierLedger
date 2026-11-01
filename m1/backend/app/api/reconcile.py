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

Auth: no auth integration is invented here. The placeholder dependency
below returns ``None`` and does nothing; the coordinator / auth agent wires
``require_auth`` in its place.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.domain.engine import FIXTURES, reconcile_batch

router = APIRouter(tags=["reconcile"])


class ReconcileRequest(BaseModel):
    """POST /api/reconcile body: shipments to reconcile + tolerance."""

    shipments: list[dict] = Field(default_factory=list)
    tolerance_poisha: int = 0


# TODO(auth): coordinator/auth agent replaces this placeholder with the real
# require_auth dependency. It intentionally does nothing in Milestone 3.
async def _auth_placeholder():
    return None


@router.post("/api/reconcile")
async def post_reconcile(
    body: ReconcileRequest,
    _current_user=Depends(_auth_placeholder),
):
    """Reconcile a batch of shipments. Computed result only — nothing is
    persisted. Persisted runs are created from a real import batch via
    POST /api/workspaces/{id}/imports/{batch_id}/runs (M6); this
    endpoint stays compute-only by design."""
    return reconcile_batch(body.shipments, body.tolerance_poisha)


@router.get("/api/rules")
async def get_rules():
    """Return the synthetic fixture rules (SYNTHETIC FIXTURE — not real
    courier contracts)."""
    return {"fixtures": list(FIXTURES.values())}
