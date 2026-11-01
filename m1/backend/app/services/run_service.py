"""Reconciliation run store (M1 Milestone 6).

Persists one reconciliation run per eligible M4 CSV import batch:
the run row, one result per strongly matched shipment, the fee lines
in all three bases, and the independent findings list. All money is
integer poisha; the engine's statuses are stored exactly as returned,
never recomputed here.

SCOPE AND HONESTY RULES (kickoff brief + V2.2.1):
- A persisted run ALWAYS requires an ``import_batch_id`` (the approved
  schema: ``reconciliation_runs.import_batch_id`` is NOT NULL). There
  is no way to persist a run from ``POST /api/reconcile`` inputs, and
  no synthetic import batch may be fabricated to work around that --
  ``POST /api/reconcile`` stays compute-only by design. Runs are
  created only from real import batches in this workspace.
- No courier rates or merchant data are invented here. Rule context
  comes either from a merchant-confirmed ``CourierRuleVersion`` or
  from one of the labelled synthetic fixtures in
  ``app.domain.engine.FIXTURES`` (never presented as a real courier
  contract). Missing or unverified information stays NULL / needs
  review, never an invented amount or an implicit zero.
- Findings are a list and are never netted; none hides another. An
  unexplained difference is never automatically a confirmed loss.
- Idempotency (CL-04/CL-14/CL-24): ``run_create`` keys, claimed via
  ``app.services.idempotency_service``. The request hash covers the
  merchant, the import batch, the courier account, the resolved rule
  context and the tolerance. Same key + same hash replays the stored
  run; a different hash conflicts. CL-05 defence in depth: the run
  hash covers merchant + batch + account + rule snapshot + tolerance
  + engine inputs, and a run whose hash already exists is returned as
  a replay instead of inserting a duplicate.
- Transaction discipline: ``create_run_from_import()`` owns one
  transaction -- run, results, fee lines, findings, the sanctioned
  narrow status UPDATE (running -> completed) and the idempotency
  completion commit together; any failure rolls all of it back and
  fails the idempotency key (conditional fail, CL-24). The caller
  must NOT commit on an exception.
- Runs are append-only after creation: the only post-creation update
  is the sanctioned narrow UPDATE of ``status`` / ``completed_at``
  (see ``fn_reconciliation_run_guard`` in triggers.sql, which permits
  exactly those two columns to change). There are no update or delete
  endpoints for runs, results, fee lines or findings.

SCHEMA INSUFFICIENCIES FOUND WHILE BUILDING (no schema invented to
work around them; documented here for the coordinator):
- Finding-code uniqueness: ``reconciliation_findings`` is unique on
  (merchant_id, result_id, finding_code), but the engine can emit the
  same DQ code twice for one result (e.g. two fee lines both blank
  give two ``DQ_BLANK_VALUE`` findings, one per fee line). The task
  formula ``code + (":" + basis if basis else "")`` disambiguates the
  two STATEMENT findings but not repeated DQ codes. This service
  therefore appends a deterministic occurrence suffix (``:2``,
  ``:3``, ...) to a repeated code within one result, in engine
  emission order, so no finding is dropped or merged. The plain code
  is kept for the first occurrence.
- Result identity CHECKs vs engine definitions (resolved by M6-A1,
  2026-11-01): the 0001 CHECKs required ``v_cond_poisha`` /
  ``v_total_poisha`` NULL whenever a term was NULL, but the engine
  legitimately emits a non-NULL dependent from a partial identity
  (e.g. blank collected -> V_total -2000 with C/delta/V_R/S NULL;
  blank COD-fee line -> V_cond 0 with S/V_R NULL). Migration 0002
  relaxes both CHECKs to enforce the identity only when all terms
  are non-NULL; the engine is unchanged. See backend/README.md
  ("M6-A1") and tests/test_m6a1_live_pg.py.
- Rule-version fallback: when a rule version leaves
  ``cod_fee_base`` / ``cod_fee_rate_bps`` NULL, the engine falls
  back to its Fixture Alpha defaults (pre-existing engine behaviour,
  documented in ``app/domain/engine.py``). The resolved values are
  recorded in ``rule_snapshot`` so the fallback is visible, not
  silent, but a rule version that means "no fee" should state its
  base/rate explicitly.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    CourierRuleVersion,
    ImportBatch,
    ImportRow,
    Order,
    ReconciliationFeeLine,
    ReconciliationFinding,
    ReconciliationResult,
    ReconciliationRun,
    Shipment,
    StatementLine,
    WorkspaceSettings,
)
from app.db.session import set_tenant_context
from app.domain.engine import FIXTURES, reconcile_batch
from app.logging_config import get_logger
from app.services import audit_service, idempotency_service
from app.services.idempotency_service import IdempotencyConflictError
from app.services.workspace_service import get_membership

logger = get_logger(__name__)

ENGINE_VERSION = "V2.2.1"

# Engine fee-line type -> stored fee_code (v0.1 C.5, CL-08/CL-09).
_FEE_CODE_MAP = {
    "delivery": "delivery_fee",
    "cod": "cod_fee",
    "return": "return_fee",
    "vat": "vat_on_fee",
    "tax": "vat_on_fee",
}
# Stored fee_code -> canonical statement field (source_statement_field).
_FEE_SOURCE_FIELD = {
    "delivery_fee": "delivery_fee",
    "cod_fee": "cod_fee",
    "return_fee": "return_fee",
    "other_fee": "other_fee",
    "vat_on_fee": None,
}
# Engine finding code -> stored finding_type (v0.1 C.5, CL-01).
_FINDING_TYPE_MAP = {
    "COLLECTION": "collection",
    "FEE_TAX": "fee_tax",
    "STATEMENT": "statement_arithmetic",
    "PAYOUT_COND": "conditional_payout",
    "SCENARIO": "scenario",
}
# Engine parse state -> stored per-basis DQ state (CL-08).
_PARSE_STATE_MAP = {
    "missing_column": "column_absent",
    "blank_value": "blank",
    "invalid_value": "invalid",
    "valid": "present_valid",
    "explicit_zero": "explicit_zero",
}
# Model cod_fee_base -> engine cod_fee_base.
_BASE_MAP = {"expected_collectible": "full_cod"}
# Model vat_mode -> (engine vat_mode, engine vat_formula).
_VAT_MAP = {
    "included_rate_on_net": ("included", "rate_on_net"),
    "included_rate_on_gross": ("included", "rate_on_gross"),
    "excluded": ("excluded", None),
    "unknown": ("unknown", None),
}

_OVERALL_STATUSES = ("matched", "open", "mismatch", "needs_review", "matched_with_tolerance")

_EXCLUDED_REASONS = {
    "duplicate": "duplicate tracking in batch",
    "ambiguous": "ambiguous match — needs human confirmation",
    "unmatched": "no shipment matched",
}

# Canonical fee fields on a statement line, in engine fee-line order.
_FEE_FIELDS = ("delivery", "cod", "return", "other")
_FEE_STATE_ATTR = {
    "delivery": "reported_delivery_fee_state",
    "cod": "reported_cod_fee_state",
    "return": "reported_return_fee_state",
    "other": "reported_other_fee_state",
}
_FEE_VALUE_ATTR = {
    "delivery": "reported_delivery_fee_poisha",
    "cod": "reported_cod_fee_poisha",
    "return": "reported_return_fee_poisha",
    "other": "reported_other_fee_poisha",
}
_FEE_CANONICAL = {
    "delivery": "delivery_fee",
    "cod": "cod_fee",
    "return": "return_fee",
    "other": "other_fee",
}


class RunCreationError(Exception):
    """The run cannot be created (API maps to 422).

    Carries a machine-readable ``reason`` and, for the zero-eligible
    case, per-reason ``counts`` of the excluded statement lines."""

    def __init__(self, message: str, *, reason: str | None = None, counts: dict | None = None):
        super().__init__(message)
        self.reason = reason or "run_creation_failed"
        self.counts = counts or {}


class RunNotFoundError(Exception):
    """The run (or its import batch) is not in this workspace (API maps
    to 404). No existence is leaked to non-members."""


def _coerce_uuid(value):
    """Accept a uuid.UUID or its string form; anything else becomes
    None (the caller treats it as "no such row")."""
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _counts_by_status(counts: dict) -> dict:
    return {status: int(counts.get(status, 0)) for status in _OVERALL_STATUSES}


# ---------------------------------------------------------------------------
# Rule context
# ---------------------------------------------------------------------------

def _rule_int(value):
    """A rule-definition JSON value is usable only as a real int (never
    a bool, never a string). Anything else is unknown (None), never a
    silent zero."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _resolve_rule_context(db: Session, merchant_id, account_id, *,
                          rule_version_id, courier_fixture):
    """Resolve exactly one rule context.

    Returns (rule_context, rule_version_id, rule_version_no,
    rule_content_hash) where rule_context is the full resolved context
    stored as ``rule_snapshot`` and hashed into the request/run hashes.
    Raises RunCreationError (422) when both or neither source is given,
    the fixture is unknown, or the rule version is not this merchant's
    version for this account."""
    if rule_version_id is not None and courier_fixture is not None:
        raise RunCreationError(
            "Provide exactly one of courier_fixture or rule_version_id, not both.",
            reason="ambiguous_rule",
        )
    if rule_version_id is None and courier_fixture is None:
        courier_fixture = "Fixture Alpha"
    if courier_fixture is not None:
        fixture = FIXTURES.get(courier_fixture)
        if fixture is None:
            raise RunCreationError(
                f"Unknown synthetic fixture: {courier_fixture!r}.",
                reason="unknown_fixture",
            )
        context = {
            "kind": "synthetic_fixture",
            "courier_fixture": courier_fixture,
            "cod_fee_base": fixture["cod_fee_base"],
            "cod_fee_rate_bp": fixture["cod_fee_rate_bp"],
            "label": "SYNTHETIC FIXTURE — not a real courier contract",
            "contractual_delivery_fee_poisha": None,
            "dc_confirmed": False,
            "dc_evidence_ref": None,
            "delivery_fee_poisha": None,
            "return_fee_poisha": None,
            "other_fees_poisha": None,
            "excluded_tax_poisha": None,
            "vat_mode": "unknown",
            "vat_formula": None,
            "vat_rate_bp": None,
        }
        return context, None, None, None

    version = db.execute(
        select(CourierRuleVersion).where(
            (CourierRuleVersion.merchant_id == merchant_id)
            & (CourierRuleVersion.id == rule_version_id)
            & (CourierRuleVersion.merchant_courier_account_id == account_id)
        )
    ).scalar_one_or_none()
    if version is None:
        raise RunCreationError(
            "Rule version not found for this workspace and courier account.",
            reason="rule_version_not_found",
        )
    base = version.cod_fee_base
    mapped_base = _BASE_MAP.get(base, base)
    vat_mode, vat_formula = _VAT_MAP.get(version.vat_mode, ("unknown", None))
    definition = version.rule_definition or {}
    if not isinstance(definition, dict):
        definition = {}
    context = {
        "kind": "rule_version",
        "rule_version_id": str(version.id),
        "rule_version_no": version.version_no,
        "rule_content_hash": version.content_hash,
        "cod_fee_base": mapped_base,
        "cod_fee_rate_bp": version.cod_fee_rate_bps,
        "contractual_delivery_fee_poisha": version.contractual_delivery_fee_poisha,
        "dc_confirmed": bool(version.dc_confirmed),
        "dc_evidence_ref": version.dc_evidence_ref,
        "delivery_fee_poisha": version.delivery_fee_poisha,
        "return_fee_poisha": version.return_fee_poisha,
        "other_fees_poisha": _rule_int(definition.get("other_fees_poisha")),
        "excluded_tax_poisha": _rule_int(definition.get("excluded_tax_poisha")),
        "vat_mode": vat_mode,
        "vat_formula": vat_formula,
        "vat_rate_bp": version.vat_rate_bps,
    }
    return context, version.id, version.version_no, version.content_hash


def _resolve_tolerance(db: Session, merchant_id, tolerance_poisha):
    """Explicit tolerance wins; otherwise the workspace setting (0
    when no settings row exists -- the single asserted exception to
    the no-money-default rule, v0.1 C.7). Must be an int >= 0."""
    if tolerance_poisha is not None:
        if isinstance(tolerance_poisha, bool) or not isinstance(tolerance_poisha, int):
            raise RunCreationError("tolerance_poisha must be an integer.", reason="bad_tolerance")
        if tolerance_poisha < 0:
            raise RunCreationError("tolerance_poisha must be >= 0.", reason="bad_tolerance")
        return tolerance_poisha
    settings = db.execute(
        select(WorkspaceSettings).where(WorkspaceSettings.merchant_id == merchant_id)
    ).scalar_one_or_none()
    if settings is None:
        return 0
    return settings.tolerance_poisha


# ---------------------------------------------------------------------------
# Engine inputs
# ---------------------------------------------------------------------------

def _raw_lookup(raw_values: dict, mapping_snapshot: dict, canonical: str):
    """Raw text of one mapped source header for a row, looked up
    case-insensitively (the file header may differ in case from the
    mapped header). Returns None when the header is absent."""
    source = (mapping_snapshot or {}).get(canonical)
    if not source:
        return None
    by_key = {str(k).strip().lower(): v for k, v in (raw_values or {}).items()}
    return by_key.get(str(source).strip().lower())


def _build_engine_inputs(lines_with_rows, rule_context, mapping_snapshot):
    """Build engine inputs for the eligible lines.

    ``lines_with_rows`` is a list of (StatementLine, ImportRow,
    Shipment, Order) in import row-number order. Returns (inputs,
    eligible) where eligible carries the same tuples for persistence.
    Fee-line raw values follow V2.2.1 §3: the parsed integer when the
    state is present_valid / explicit_zero, the preserved raw text
    when invalid, else None; column_present is False exactly when the
    state is column_absent."""
    inputs = []
    for line, row, shipment, order in lines_with_rows:
        fee_lines = []
        for fee_type in _FEE_FIELDS:
            state = getattr(line, _FEE_STATE_ATTR[fee_type])
            if state in ("present_valid", "explicit_zero"):
                raw = getattr(line, _FEE_VALUE_ATTR[fee_type])
            elif state == "invalid":
                raw = _raw_lookup(row.raw_values, mapping_snapshot, _FEE_CANONICAL[fee_type])
            else:
                raw = None
            fee_lines.append({
                "fee_type": fee_type,
                "raw": raw,
                "column_present": state != "column_absent",
            })
        inputs.append({
            "merchant_order_id": order.merchant_order_ref,
            "tracking_id": line.tracking_id_normalised or line.tracking_id_raw,
            "courier_fixture": (
                rule_context.get("courier_fixture")
                if rule_context.get("kind") == "synthetic_fixture" else None
            ),
            "cod_fee_base": rule_context.get("cod_fee_base"),
            "cod_fee_rate_bp": rule_context.get("cod_fee_rate_bp"),
            "outcome": shipment.outcome,
            "amount_to_collect_poisha": order.order_cod_poisha,
            "documented_partial_poisha": (
                shipment.expected_collectible_cod_poisha
                if shipment.outcome == "partially_delivered" else None
            ),
            "reported_collected_poisha": line.reported_collected_poisha,
            "reported_payout_poisha": line.reported_payout_poisha,
            "reported_delivery_fee_poisha": line.reported_delivery_fee_poisha,
            "contractual_delivery_fee_poisha": rule_context.get("contractual_delivery_fee_poisha"),
            "contractual_delivery_fee_confirmed": rule_context.get("dc_confirmed", False),
            "contractual_delivery_fee_evidence": rule_context.get("dc_evidence_ref"),
            "other_fees_poisha": rule_context.get("other_fees_poisha"),
            "return_fee_poisha": rule_context.get("return_fee_poisha"),
            "excluded_tax_poisha": rule_context.get("excluded_tax_poisha"),
            "reported_excluded_tax_poisha": rule_context.get("excluded_tax_poisha"),
            "reported_deductions_total_poisha": None,
            "expected_delivery_fee_poisha": rule_context.get("delivery_fee_poisha"),
            "fee_lines": fee_lines,
            "vat_mode": rule_context.get("vat_mode"),
            "vat_formula": rule_context.get("vat_formula"),
            "vat_rate_bp": rule_context.get("vat_rate_bp"),
        })
    return inputs


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _basis_state(value, *, contract_missing: bool = False) -> str:
    """CL-08 per-basis state for a stored fee-line basis value."""
    if value is None:
        return "contract_missing" if contract_missing else "rule_unknown"
    if value == 0:
        return "explicit_zero"
    return "present_valid"


def _persist_results(db: Session, merchant_id, run, eligible, engine_results, tolerance_poisha):
    """Insert results + fee lines + findings for one run.

    ``eligible`` is the list of (line, row, shipment, order) in the
    same order as ``engine_results``. Stores the engine's values and
    statuses exactly as returned, never recomputed."""
    for (line, _row, _shipment, _order), r in zip(eligible, engine_results):
        result = ReconciliationResult(
            merchant_id=merchant_id,
            run_id=run.id,
            shipment_id=line.shipment_id,
            statement_line_id=line.id,
            expected_collectible_poisha=r["E"],
            reported_collected_poisha=r["R"],
            c_poisha=r["C"],
            x_e_poisha=r["X_e"],
            x_r_poisha=r["X_r"],
            x_upper_r_poisha=r["X_R"],
            delta_poisha=r["delta"],
            v_r_poisha=r["V_R"],
            p_e_poisha=r["P_e"],
            p_cond_poisha=r["P_cond"],
            p_r_poisha=r["P_r"],
            s_poisha=r["S"],
            v_cond_poisha=r["V_cond"],
            v_total_poisha=r["V_total"],
            contractual_payout_status={
                "matched": "matched",
                "mismatch": "mismatch",
                "unknown": "not_calculable",
            }[r["contractual_payout_status"]],
            data_quality_status=(
                "needs_review" if r["data_quality_status"] == "needs_review" else "ok"
            ),
            overall_status=r["overall_status"],
        )
        db.add(result)
        db.flush()

        # Fee lines: bases per the task mapping; the included-tax
        # breakdown (when the engine produced one) sits on the
        # delivery_fee line only, as a breakdown, never subtracted.
        vat_tax = None
        if isinstance(r.get("vat_breakdown"), dict):
            vat_tax = r["vat_breakdown"].get("tax")
        cod_contract_missing = (
            r.get("cod_fee_base") == "cod_minus_delivery_fee"
            and not r.get("D_c_confirmed")
        )
        seq_by_code: dict[str, int] = {}
        for fee_line in r.get("fee_lines") or []:
            fee_code = _FEE_CODE_MAP.get(fee_line.get("fee_type"), "other_fee")
            seq_by_code[fee_code] = seq_by_code.get(fee_code, 0) + 1
            parsed = fee_line.get("parsed_poisha")
            actual_state = _PARSE_STATE_MAP[fee_line.get("state")]
            if fee_code == "delivery_fee":
                expected_value, reported_value = r["D_e"], r["D_r"]
                expected_state = _basis_state(expected_value)
                reported_state = _basis_state(reported_value)
                included_tax = vat_tax
            elif fee_code == "cod_fee":
                expected_value, reported_value = r["F_e"], r["F_R"]
                expected_state = _basis_state(expected_value, contract_missing=cod_contract_missing)
                reported_state = _basis_state(reported_value, contract_missing=cod_contract_missing)
                included_tax = None
            elif fee_code == "return_fee":
                expected_value = reported_value = r.get("_return_fee_input")
                expected_state = _basis_state(expected_value)
                reported_state = _basis_state(reported_value)
                included_tax = None
            elif fee_code == "other_fee":
                expected_value = reported_value = r.get("_other_fees_input")
                expected_state = _basis_state(expected_value)
                reported_state = _basis_state(reported_value)
                included_tax = None
            else:  # vat_on_fee: no expected/reported basis exists.
                expected_value = reported_value = None
                expected_state = reported_state = "rule_unknown"
                included_tax = None
            db.add(ReconciliationFeeLine(
                merchant_id=merchant_id,
                result_id=result.id,
                fee_code=fee_code,
                component_seq=seq_by_code[fee_code],
                source_statement_field=_FEE_SOURCE_FIELD[fee_code],
                expected_basis_poisha=expected_value,
                expected_basis_state=expected_state,
                reported_basis_poisha=reported_value,
                reported_basis_state=reported_state,
                reported_actual_poisha=parsed,
                reported_actual_state=actual_state,
                included_tax_poisha=included_tax,
            ))

        # Findings: never netted, none hides another. The finding_code
        # formula (code + ":" + basis) disambiguates the two STATEMENT
        # findings; a repeated code within one result (possible for DQ
        # fee-line findings -- see the module docstring) gets a
        # deterministic occurrence suffix so every finding is stored.
        seen_codes: dict[str, int] = {}
        for f in r.get("findings") or []:
            code = f["code"]
            if f.get("basis"):
                code = f"{code}:{f['basis']}"
            seen_codes[code] = seen_codes.get(code, 0) + 1
            if seen_codes[code] > 1:
                code = f"{code}:{seen_codes[code]}"
            db.add(ReconciliationFinding(
                merchant_id=merchant_id,
                run_id=run.id,
                result_id=result.id,
                category=f["kind"],
                finding_type=(
                    _FINDING_TYPE_MAP[f["code"]] if f["code"] in _FINDING_TYPE_MAP
                    else "data_quality"
                ),
                finding_code=code,
                amount_poisha=f.get("amount_poisha"),
                is_within_tolerance=(
                    f.get("amount_poisha") is not None
                    and abs(f["amount_poisha"]) <= tolerance_poisha
                ),
                explanation_en=f["message"],
                explanation_bn=None,
            ))
        db.flush()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_run_from_import(
    db: Session,
    *,
    user,
    merchant_id,
    import_batch_id,
    courier_fixture=None,
    rule_version_id=None,
    tolerance_poisha=None,
    idempotency_key=None,
    claimed_by_instance="run-create",
) -> dict:
    """Create a reconciliation run from one import batch.

    Returns a summary dict (run id, status, result counts by overall
    status, excluded counts by reason, idempotency outcome). Raises
    RunNotFoundError (batch not in this workspace), RunCreationError
    (422: parse_failed batch, bad rule, bad tolerance, zero eligible
    lines) or IdempotencyConflictError (same key, different request;
    the API maps it to 409). On any failure after the claim the
    idempotency key is failed and the caller must roll back; on
    success this function commits."""
    merchant_id = _coerce_uuid(merchant_id)
    import_batch_id = _coerce_uuid(import_batch_id)
    rule_version_id = _coerce_uuid(rule_version_id)
    if merchant_id is None or import_batch_id is None:
        raise RunNotFoundError("Import batch not found in this workspace.")
    membership = get_membership(db, user.id, merchant_id)
    if membership is None:
        raise RunNotFoundError("Workspace not found.")
    # Tenant context must be set BEFORE any tenant-table read (B-1
    # pattern: the tenant-table SELECT policies key on it).
    set_tenant_context(db, merchant_id)

    batch = db.get(ImportBatch, import_batch_id)
    if batch is None or batch.merchant_id != merchant_id:
        raise RunNotFoundError("Import batch not found in this workspace.")
    if batch.status == "parse_failed":
        raise RunCreationError(
            "Import batch failed to parse; no run can be created from it.",
            reason="batch_parse_failed",
        )
    if batch.status not in ("parsed", "ready"):
        raise RunCreationError(
            f"Import batch is not in a runnable state (status {batch.status!r}).",
            reason="batch_not_ready",
        )
    account_id = batch.merchant_courier_account_id

    rule_context, rule_version_id, rule_version_no, rule_content_hash = (
        _resolve_rule_context(
            db, merchant_id, account_id,
            rule_version_id=rule_version_id, courier_fixture=courier_fixture,
        )
    )
    tolerance = _resolve_tolerance(db, merchant_id, tolerance_poisha)

    # Statement lines in import row-number order, with their rows.
    line_rows = db.execute(
        select(StatementLine, ImportRow)
        .join(ImportRow, ImportRow.id == StatementLine.import_row_id)
        .where(StatementLine.import_batch_id == batch.id)
        .order_by(ImportRow.row_number)
    ).all()
    lined_row_ids = {row.id for _line, row in line_rows}
    unlined_q = select(ImportRow).where(ImportRow.import_batch_id == batch.id)
    if lined_row_ids:
        unlined_q = unlined_q.where(ImportRow.id.not_in(lined_row_ids))
    unlined_rows = db.execute(
        unlined_q.order_by(ImportRow.row_number)
    ).scalars().all()

    eligible = []
    excluded = []
    for line, row in line_rows:
        if line.match_status == "strong_matched" and line.shipment_id is not None:
            shipment = db.get(Shipment, line.shipment_id)
            order = db.get(Order, shipment.order_id) if shipment is not None else None
            if shipment is None or order is None:
                excluded.append({
                    "statement_line_id": str(line.id),
                    "tracking": line.tracking_id_normalised or line.tracking_id_raw,
                    "reason": "no shipment matched",
                })
                continue
            eligible.append((line, row, shipment, order))
        else:
            excluded.append({
                "statement_line_id": str(line.id),
                "tracking": line.tracking_id_normalised or line.tracking_id_raw,
                "reason": _EXCLUDED_REASONS.get(
                    line.match_status, "match status not eligible for runs"
                ),
            })
    for row in unlined_rows:
        excluded.append({
            "statement_line_id": None,
            "tracking": None,
            "reason": "row failed to parse / skipped",
        })

    if not eligible:
        counts: dict[str, int] = {}
        for entry in excluded:
            counts[entry["reason"]] = counts.get(entry["reason"], 0) + 1
        # Nothing is persisted for this request -- not even an
        # idempotency claim (eligibility is checked before claiming).
        raise RunCreationError(
            "No eligible statement lines in this import batch: no run created.",
            reason="no_eligible_lines",
            counts=counts,
        )

    inputs = _build_engine_inputs(eligible, rule_context, batch.mapping_snapshot)

    # Idempotency FIRST (mirror csv_import): the request hash covers
    # merchant + batch + account + resolved rule context + tolerance.
    request_hash = _sha256_hex(_canonical_json({
        "merchant_id": str(merchant_id),
        "import_batch_id": str(import_batch_id),
        "account_id": str(account_id),
        "rule": rule_context,
        "tolerance_poisha": tolerance,
    }).encode("utf-8"))
    key = idempotency_key or f"run-{uuid.uuid4().hex}"
    outcome, key_row = idempotency_service.claim(
        db, merchant_id, "run_create", key, request_hash, claimed_by_instance
    )
    if outcome == "replay":
        run = db.get(ReconciliationRun, key_row.result_reconciliation_run_id)
        if run is None or run.merchant_id != merchant_id:  # pragma: no cover - defensive
            raise RunCreationError(
                "Idempotency key completed but its run is missing.",
                reason="replay_run_missing",
            )
        return _run_summary(db, run, merchant_id, outcome="replay")

    # CL-05 defence in depth: the run hash covers merchant + batch +
    # account + rule snapshot + tolerance + engine inputs. A run with
    # this hash already exists (created under a different key): return
    # it as a replay instead of inserting a duplicate, and complete
    # this key against the existing run.
    run_hash = _sha256_hex(_canonical_json({
        "merchant_id": str(merchant_id),
        "import_batch_id": str(import_batch_id),
        "account_id": str(account_id),
        "rule_snapshot": rule_context,
        "tolerance_poisha": tolerance,
        "engine_inputs": inputs,
    }).encode("utf-8"))
    existing = db.execute(
        select(ReconciliationRun).where(
            (ReconciliationRun.merchant_id == merchant_id)
            & (ReconciliationRun.run_hash == run_hash)
        )
    ).scalar_one_or_none()
    if existing is not None:
        completed = idempotency_service.complete(
            db, merchant_id, "run_create", key, key_row.claim_token, existing.id
        )
        if not completed:
            raise RunCreationError(
                "Idempotency lease was lost during run creation; the run was not created.",
                reason="lease_lost",
            )
        db.commit()
        return _run_summary(db, existing, merchant_id, outcome="replay")

    try:
        engine_result = reconcile_batch(inputs, tolerance)
        # The return/other fee-line bases need the rule-context inputs;
        # carry them on the engine result dicts (underscore keys are
        # never persisted -- they are consumed by _persist_results).
        for r, inp in zip(engine_result["results"], inputs):
            r["_return_fee_input"] = inp["return_fee_poisha"]
            r["_other_fees_input"] = inp["other_fees_poisha"]

        run = ReconciliationRun(
            merchant_id=merchant_id,
            import_batch_id=batch.id,
            merchant_courier_account_id=account_id,
            rule_version_id=rule_version_id,
            rule_version_no=rule_version_no,
            rule_content_hash=rule_content_hash,
            rule_snapshot=dict(rule_context),
            input_snapshot={
                "shipments": inputs,
                "excluded": excluded,
                "engine": "app.domain.engine",
            },
            mapping_snapshot=dict(batch.mapping_snapshot or {}),
            tolerance_poisha=tolerance,
            engine_version=ENGINE_VERSION,
            status="running",
            run_hash=run_hash,
            created_by_user_id=user.id,
        )
        db.add(run)
        db.flush()
        _persist_results(db, merchant_id, run, eligible, engine_result["results"], tolerance)

        # Sanctioned narrow UPDATE (Core update WHERE id AND
        # status='running'): the run guard (triggers.sql) permits
        # exactly status + completed_at to change. Exactly one row
        # must change, else the run was never running -- raise and
        # roll back rather than completing a run that is not ours.
        updated = db.execute(
            update(ReconciliationRun)
            .where(
                (ReconciliationRun.id == run.id)
                & (ReconciliationRun.status == "running")
            )
            .values(status="completed", completed_at=_utcnow())
        ).rowcount
        if updated != 1:
            raise RunCreationError(
                "Run status transition running -> completed did not apply.",
                reason="status_transition_failed",
            )
        completed = idempotency_service.complete(
            db, merchant_id, "run_create", key, key_row.claim_token, run.id
        )
        if not completed:
            raise RunCreationError(
                "Idempotency lease was lost during run creation; the run was rolled back.",
                reason="lease_lost",
            )
    except Exception:
        # Fail the key (CL-24, conditional on still owning it) and let
        # the caller roll the whole transaction back. Never swallow the
        # original error, and never complete a failed run. The fail
        # itself is best-effort: if the failure was a database error
        # the session may already be broken, in which case the key
        # simply stays in_progress until its lease lapses.
        try:
            idempotency_service.fail(db, merchant_id, "run_create", key, key_row.claim_token)
        except Exception:
            pass
        raise
    # Capture everything the log/summary needs BEFORE commit: commit
    # expires the ORM attributes, and after commit the transaction-
    # local tenant context is cleared, so touching run.* first would
    # make the RLS policy hide the row (B-1 failure mode). The
    # summary re-asserts the context before it refreshes.
    run_id = run.id
    n_results = len(engine_result["results"])
    n_excluded = len(excluded)
    # Idempotency replays and the CL-05 existing-run path return
    # above without creating a run, so they write no audit row: the
    # event audited here is the creation of THIS run.
    audit_service.record_audit(
        db,
        actor=user,
        merchant_id=merchant_id,
        action=audit_service.RUNS_CREATED,
        entity_table="reconciliation_runs",
        entity_id=run.id,
        detail={
            "import_batch_id": str(batch.id),
            "results": n_results,
            "excluded": n_excluded,
        },
    )
    db.commit()
    logger.info("run_created", extra={
        "run_id": str(run_id), "merchant_id": str(merchant_id),
        "batch_id": str(batch.id), "results": n_results,
        "excluded": n_excluded,
    })
    return _run_summary(db, run, merchant_id, outcome="claimed")


def _run_summary(db: Session, run: ReconciliationRun, merchant_id, *, outcome: str) -> dict:
    # Commit ends the transaction and clears the transaction-local
    # app.current_merchant_id setting; re-assert it from the KNOWN
    # merchant id (never from run.* -- those attributes are expired
    # after commit and reading them first would fail under RLS)
    # before refresh, or the RLS policy hides the row from the
    # runtime role and refresh raises "Could not refresh instance"
    # (B-1 pattern).
    set_tenant_context(db, merchant_id)
    db.refresh(run)
    counts = db.execute(
        select(ReconciliationResult.overall_status, func.count())
        .where(ReconciliationResult.run_id == run.id)
        .group_by(ReconciliationResult.overall_status)
    ).all()
    excluded = (run.input_snapshot or {}).get("excluded") or []
    excluded_counts: dict[str, int] = {}
    for entry in excluded:
        reason = entry.get("reason", "unknown")
        excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
    return {
        "run_id": str(run.id),
        "merchant_id": str(run.merchant_id),
        "import_batch_id": str(run.import_batch_id),
        "status": run.status,
        "tolerance_poisha": run.tolerance_poisha,
        "result_counts": _counts_by_status(dict(counts)),
        "excluded_counts": excluded_counts,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "idempotency_outcome": outcome,
    }


# ---------------------------------------------------------------------------
# Readers (all set tenant context first; None / RunNotFoundError when the
# run or batch is not in this workspace)
# ---------------------------------------------------------------------------

def _run_dict(run: ReconciliationRun, result_counts: dict | None = None) -> dict:
    return {
        "run_id": str(run.id),
        "merchant_id": str(run.merchant_id),
        "import_batch_id": str(run.import_batch_id),
        "merchant_courier_account_id": str(run.merchant_courier_account_id),
        "rule_version_id": str(run.rule_version_id) if run.rule_version_id else None,
        "rule_version_no": run.rule_version_no,
        "rule_content_hash": run.rule_content_hash,
        "rule_snapshot": run.rule_snapshot,
        "tolerance_poisha": run.tolerance_poisha,
        "engine_version": run.engine_version,
        "status": run.status,
        "run_hash": run.run_hash,
        "created_by_user_id": str(run.created_by_user_id) if run.created_by_user_id else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "result_counts": _counts_by_status(result_counts or {}),
    }


def _fee_line_dict(line: ReconciliationFeeLine) -> dict:
    return {
        "fee_code": line.fee_code,
        "component_seq": line.component_seq,
        "source_statement_field": line.source_statement_field,
        "expected_basis_poisha": line.expected_basis_poisha,
        "expected_basis_state": line.expected_basis_state,
        "reported_basis_poisha": line.reported_basis_poisha,
        "reported_basis_state": line.reported_basis_state,
        "reported_actual_poisha": line.reported_actual_poisha,
        "reported_actual_state": line.reported_actual_state,
        "included_tax_poisha": line.included_tax_poisha,
        "rounding_note": line.rounding_note,
    }


def _finding_dict(finding: ReconciliationFinding) -> dict:
    return {
        "finding_id": str(finding.id),
        "result_id": str(finding.result_id),
        "category": finding.category,
        "finding_type": finding.finding_type,
        "finding_code": finding.finding_code,
        "amount_poisha": finding.amount_poisha,
        "is_within_tolerance": finding.is_within_tolerance,
        "explanation_en": finding.explanation_en,
        "explanation_bn": finding.explanation_bn,
        "created_at": finding.created_at.isoformat() if finding.created_at else None,
    }


def _result_dict(result: ReconciliationResult, fee_lines, findings) -> dict:
    return {
        "result_id": str(result.id),
        "run_id": str(result.run_id),
        "shipment_id": str(result.shipment_id),
        "statement_line_id": str(result.statement_line_id) if result.statement_line_id else None,
        "expected_collectible_poisha": result.expected_collectible_poisha,
        "reported_collected_poisha": result.reported_collected_poisha,
        "c_poisha": result.c_poisha,
        "x_e_poisha": result.x_e_poisha,
        "x_r_poisha": result.x_r_poisha,
        "x_upper_r_poisha": result.x_upper_r_poisha,
        "delta_poisha": result.delta_poisha,
        "v_r_poisha": result.v_r_poisha,
        "p_e_poisha": result.p_e_poisha,
        "p_cond_poisha": result.p_cond_poisha,
        "p_r_poisha": result.p_r_poisha,
        "s_poisha": result.s_poisha,
        "v_cond_poisha": result.v_cond_poisha,
        "v_total_poisha": result.v_total_poisha,
        "contractual_payout_status": result.contractual_payout_status,
        "data_quality_status": result.data_quality_status,
        "overall_status": result.overall_status,
        "created_at": result.created_at.isoformat() if result.created_at else None,
        "fee_lines": [_fee_line_dict(fl) for fl in fee_lines],
        "findings": [_finding_dict(f) for f in findings],
    }


def _get_run(db: Session, merchant_id, run_id) -> ReconciliationRun | None:
    merchant_id = _coerce_uuid(merchant_id)
    run_id = _coerce_uuid(run_id)
    if merchant_id is None or run_id is None:
        return None
    set_tenant_context(db, merchant_id)
    run = db.get(ReconciliationRun, run_id)
    if run is None or run.merchant_id != merchant_id:
        return None
    return run


def _result_counts_for_runs(db: Session, run_ids) -> dict:
    """{run_id: {overall_status: count}} for the given runs."""
    if not run_ids:
        return {}
    rows = db.execute(
        select(ReconciliationResult.run_id, ReconciliationResult.overall_status, func.count())
        .where(ReconciliationResult.run_id.in_(run_ids))
        .group_by(ReconciliationResult.run_id, ReconciliationResult.overall_status)
    ).all()
    counts: dict = {}
    for run_id, status, count in rows:
        counts.setdefault(run_id, {})[status] = count
    return counts


def get_run_detail(db: Session, merchant_id, run_id) -> dict | None:
    """Full run detail: run + results (each with fee lines +
    findings) + result counts. None when the run is not in this
    workspace."""
    run = _get_run(db, merchant_id, run_id)
    if run is None:
        return None
    results = db.execute(
        select(ReconciliationResult)
        .where(ReconciliationResult.run_id == run.id)
        .order_by(ReconciliationResult.created_at, ReconciliationResult.id)
    ).scalars().all()
    result_ids = [r.id for r in results]
    fee_lines = {}
    findings = {}
    if result_ids:
        for fl in db.execute(
            select(ReconciliationFeeLine)
            .where(ReconciliationFeeLine.result_id.in_(result_ids))
            .order_by(ReconciliationFeeLine.fee_code, ReconciliationFeeLine.component_seq)
        ).scalars().all():
            fee_lines.setdefault(fl.result_id, []).append(fl)
        for f in db.execute(
            select(ReconciliationFinding)
            .where(ReconciliationFinding.result_id.in_(result_ids))
            .order_by(ReconciliationFinding.created_at, ReconciliationFinding.id)
        ).scalars().all():
            findings.setdefault(f.result_id, []).append(f)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.overall_status] = counts.get(r.overall_status, 0) + 1
    detail = _run_dict(run, counts)
    detail["input_snapshot"] = run.input_snapshot
    detail["mapping_snapshot"] = run.mapping_snapshot
    detail["results"] = [
        _result_dict(r, fee_lines.get(r.id, []), findings.get(r.id, [])) for r in results
    ]
    return detail


def list_runs(db: Session, merchant_id, limit: int = 20, offset: int = 0):
    """(items, total): runs newest first, each with result counts by
    overall status."""
    merchant_id = _coerce_uuid(merchant_id)
    if merchant_id is None:
        return [], 0
    set_tenant_context(db, merchant_id)
    total = db.execute(
        select(func.count())
        .select_from(ReconciliationRun)
        .where(ReconciliationRun.merchant_id == merchant_id)
    ).scalar_one()
    runs = db.execute(
        select(ReconciliationRun)
        .where(ReconciliationRun.merchant_id == merchant_id)
        .order_by(ReconciliationRun.created_at.desc(), ReconciliationRun.id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    counts = _result_counts_for_runs(db, [r.id for r in runs])
    return [_run_dict(r, counts.get(r.id, {})) for r in runs], total


def list_run_results(db: Session, merchant_id, run_id, limit: int = 20, offset: int = 0):
    """(items, total): results of one run, in creation order. Raises
    RunNotFoundError when the run is not in this workspace."""
    run = _get_run(db, merchant_id, run_id)
    if run is None:
        raise RunNotFoundError("Run not found in this workspace.")
    total = db.execute(
        select(func.count())
        .select_from(ReconciliationResult)
        .where(ReconciliationResult.run_id == run.id)
    ).scalar_one()
    results = db.execute(
        select(ReconciliationResult)
        .where(ReconciliationResult.run_id == run.id)
        .order_by(ReconciliationResult.created_at, ReconciliationResult.id)
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    result_ids = [r.id for r in results]
    fee_lines = {}
    findings = {}
    if result_ids:
        for fl in db.execute(
            select(ReconciliationFeeLine)
            .where(ReconciliationFeeLine.result_id.in_(result_ids))
            .order_by(ReconciliationFeeLine.fee_code, ReconciliationFeeLine.component_seq)
        ).scalars().all():
            fee_lines.setdefault(fl.result_id, []).append(fl)
        for f in db.execute(
            select(ReconciliationFinding)
            .where(ReconciliationFinding.result_id.in_(result_ids))
            .order_by(ReconciliationFinding.created_at, ReconciliationFinding.id)
        ).scalars().all():
            findings.setdefault(f.result_id, []).append(f)
    return (
        [_result_dict(r, fee_lines.get(r.id, []), findings.get(r.id, [])) for r in results],
        total,
    )


def list_run_findings(db: Session, merchant_id, run_id, limit: int = 20, offset: int = 0):
    """(items, total): findings of one run, in creation order. Raises
    RunNotFoundError when the run is not in this workspace."""
    run = _get_run(db, merchant_id, run_id)
    if run is None:
        raise RunNotFoundError("Run not found in this workspace.")
    total = db.execute(
        select(func.count())
        .select_from(ReconciliationFinding)
        .where(ReconciliationFinding.run_id == run.id)
    ).scalar_one()
    rows = db.execute(
        select(ReconciliationFinding)
        .where(ReconciliationFinding.run_id == run.id)
        .order_by(ReconciliationFinding.created_at, ReconciliationFinding.id)
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    return [_finding_dict(f) for f in rows], total


def list_runs_for_batch(db: Session, merchant_id, batch_id):
    """Runs for one import batch, newest first, each with result
    counts. Raises RunNotFoundError when the batch is not in this
    workspace."""
    merchant_id = _coerce_uuid(merchant_id)
    batch_id = _coerce_uuid(batch_id)
    if merchant_id is None or batch_id is None:
        raise RunNotFoundError("Import batch not found in this workspace.")
    set_tenant_context(db, merchant_id)
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.merchant_id != merchant_id:
        raise RunNotFoundError("Import batch not found in this workspace.")
    runs = db.execute(
        select(ReconciliationRun)
        .where(
            (ReconciliationRun.merchant_id == merchant_id)
            & (ReconciliationRun.import_batch_id == batch.id)
        )
        .order_by(ReconciliationRun.created_at.desc(), ReconciliationRun.id.desc())
    ).scalars().all()
    counts = _result_counts_for_runs(db, [r.id for r in runs])
    return [_run_dict(r, counts.get(r.id, {})) for r in runs]
