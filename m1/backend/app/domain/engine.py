"""CourierLedger M1 — deterministic COD reconciliation engine (domain module).

Ported from the P0 disposable prototype engine
(``prototype-p0/app/engine.py``, 548 lines), which implements Specification
V2 + Amendments 2.1, 2.2 and 2.2.1 plus the P0 patch (latest financial-logic
authority). Pure Python, no I/O, no UI dependency, no FastAPI / SQLAlchemy /
CSV / database imports — standard library only (``re``, ``dataclasses``).

What changed in this port (nothing else):
- Import path only: ``app.engine`` -> ``app.domain.engine``.
- One genuine bug fix (see ``reconcile_shipment`` and
  ``tests/test_engine.py::test_m1_invalid_cod_fee_rate_is_null_not_a_crash``):
  in P0, ``cod_fee_rate_bp`` was read into a local variable BEFORE the
  ``_clean_int`` loop, so a non-integer rate (string ``"100"``, float
  ``100.0``, bool ``True``) raised ``TypeError`` instead of becoming NULL +
  ``DQ_INVALID_VALUE``. In M1 the rate is read AFTER the cleaning loop, so
  it goes through the same cleaning as every other money/rate field. The
  fixture-rate fallback now applies only when no rate was supplied at all
  (raw value ``None``/absent); an explicitly supplied invalid rate stays
  NULL and no fee is calculated — no silent fallback to the fixture rate.
  No formula, status, or worked-example value changed.

Invariants (never broken here):
- All money is integer poisha. No float arithmetic for money.
- Half-up rounding per fee line, done in integer arithmetic.
- NULL (Python None) is never silently replaced by verified 0.
- An unexplained difference is never automatically a confirmed loss.
- Findings are a list; they are never netted and none hides another.
- Overall status is needs_review / mismatch / open / matched_with_tolerance
  / matched: mismatch is reserved for fee, statement or contractual
  payout findings; a collection-only discrepancy is `open`, never a
  contractual payout mismatch. Contractual payout status comes from
  V_cond alone.
- D_c is contractual only with an explicit confirmation flag AND a
  non-empty evidence reference; a D_c value alone is not confirmation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fictional synthetic fixtures only. These are NOT real courier contracts.
# Never present them as Steadfast / Pathao / Paperfly terms.
FIXTURES = {
    "Fixture Alpha": {
        "courier_fixture": "Fixture Alpha",
        "cod_fee_base": "full_cod",
        "cod_fee_rate_bp": 100,
        "label": "SYNTHETIC FIXTURE — not a real courier contract",
    },
    "Fixture Beta": {
        "courier_fixture": "Fixture Beta",
        "cod_fee_base": "cod_minus_delivery_fee",
        "cod_fee_rate_bp": 100,
        "label": "SYNTHETIC FIXTURE — not a real courier contract",
    },
    "Fixture Gamma": {
        "courier_fixture": "Fixture Gamma",
        "cod_fee_base": "reported_collected",
        "cod_fee_rate_bp": 100,
        "label": "SYNTHETIC FIXTURE — not a real courier contract",
    },
}

COD_FEE_BASES = ("full_cod", "cod_minus_delivery_fee", "reported_collected")
OUTCOMES = (
    "delivered",
    "partially_delivered",
    "returned",
    "cancelled",
    "superseded",
    "unknown",
)

BN = {
    "matched": "মিলেছে",
    "mismatch": "অমিল",
    "needs_review": "যাচাই প্রয়োজন",
    "open": "খোলা",
    "under_collection": "কম সংগ্রহ",
    "over_collection": "বেশি সংগ্রহ",
    "matched_with_tolerance": "সহনশীলতার মধ্যে মিলেছে",
    "unknown": "অজানা",
}


# ---------------------------------------------------------------------------
# Integer money helpers
# ---------------------------------------------------------------------------

def round_half_up_fee(base_poisha: int, rate_bp: int) -> int:
    """round_half_up(max(base, 0) * rate_bp / 10000) in integer arithmetic.

    base and rate must be non-negative ints. 5000 is half of 10000, so
    adding it before floor division rounds halves up. E.g. 1050p at
    100bp (1%) -> (1050*100 + 5000)//10000 = 110000//10000 = 11p.
    """
    if not isinstance(base_poisha, int) or isinstance(base_poisha, bool):
        raise TypeError("base_poisha must be an int")
    if not isinstance(rate_bp, int) or isinstance(rate_bp, bool):
        raise TypeError("rate_bp must be an int")
    if base_poisha < 0 or rate_bp < 0:
        raise ValueError("base_poisha and rate_bp must be non-negative")
    return (base_poisha * rate_bp + 5000) // 10000


def cod_fee(amount_poisha, rate_bp: int):
    """COD fee on a collected/expected amount, or None if amount is None."""
    if amount_poisha is None:
        return None
    return round_half_up_fee(max(amount_poisha, 0), rate_bp)


# ---------------------------------------------------------------------------
# Fee-line parsing (V2.2.1 §3)
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"^-?\d+$")


def parse_money(raw, column_present: bool):
    """Parse one fee-line value.

    Returns (parsed_poisha_or_None, state, raw_text_or_None).
    States: missing_column | blank_value | invalid_value | valid |
    explicit_zero. Only an explicit zero is verified 0.
    """
    if not column_present:
        return None, "missing_column", None
    if raw is None:
        return None, "blank_value", None
    if isinstance(raw, bool):
        return None, "invalid_value", str(raw)
    if isinstance(raw, int):
        return (raw, "explicit_zero", "0") if raw == 0 else (raw, "valid", str(raw))
    if isinstance(raw, float):
        # JSON floats: accept only integral values, otherwise invalid.
        if raw.is_integer():
            v = int(raw)
            return (v, "explicit_zero", "0") if v == 0 else (v, "valid", str(v))
        return None, "invalid_value", str(raw)
    text = str(raw).strip()
    if text == "":
        return None, "blank_value", text
    compact = text.replace(",", "").replace(" ", "")
    if _INT_RE.match(compact):
        v = int(compact)
        return (v, "explicit_zero", text) if v == 0 else (v, "valid", text)
    return None, "invalid_value", text


# ---------------------------------------------------------------------------
# Included-tax breakdown (V2.1 — breakdown only, never subtracted again)
# ---------------------------------------------------------------------------

def included_tax_breakdown(gross_poisha, formula, rate_bp):
    """Return {'net': int, 'tax': int} or None.

    rate_on_net:  net = round_half_up(gross*10000/(10000+rate)), tax=gross-net
    rate_on_gross: tax = round_half_up(gross*rate/10000), net=gross-tax
    Unknown formula or rate -> None (breakdown unknown, gross still counts
    once — the V2.1 included-mode/unknown-rate exception).
    """
    if gross_poisha is None or formula is None or rate_bp is None:
        return None
    if formula == "rate_on_net":
        net = (gross_poisha * 10000 + (10000 + rate_bp) // 2) // (10000 + rate_bp)
        return {"net": net, "tax": gross_poisha - net}
    if formula == "rate_on_gross":
        tax = round_half_up_fee(gross_poisha, rate_bp)
        return {"net": gross_poisha - tax, "tax": tax}
    return None


def _clean_int(value, field_name: str, findings: list):
    """Coerce a money/rate field to int|None.

    A non-integer value (e.g. '1,5x0' from a CSV) becomes None with a
    DQ_INVALID_VALUE finding; the raw text is preserved in the message.
    Never silently becomes zero.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        findings.append(_dq("DQ_INVALID_VALUE",
                            f"Field {field_name} has invalid value {value!r}; NULL, never zero."))
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    findings.append(_dq("DQ_INVALID_VALUE",
                        f"Field {field_name} has invalid monetary value {value!r}; "
                        "treated as NULL, never zero. Raw value preserved."))
    return None


# ---------------------------------------------------------------------------
# Expected collectible COD by confirmed outcome (V2.1)
# ---------------------------------------------------------------------------

def expected_collectible(shipment: dict, findings: list):
    """Return E (int|None), appending DQ findings when E cannot be fixed."""
    outcome = (shipment.get("outcome") or "").strip() or None
    amount = shipment.get("amount_to_collect_poisha")
    if outcome is None or outcome == "unknown":
        findings.append(_dq("DQ_MISSING_OUTCOME",
                            "Shipment outcome is missing or unknown, so expected "
                            "collectible COD cannot be fixed."))
        return None
    if outcome == "delivered":
        if amount is None:
            findings.append(_dq("DQ_MISSING_OUTCOME",
                                "Outcome is delivered but amount to collect is missing."))
            return None
        return amount
    if outcome == "partially_delivered":
        partial = shipment.get("documented_partial_poisha")
        if partial is None:
            findings.append(_dq("DQ_MISSING_OUTCOME",
                                "Partial delivery has no documented partial amount, "
                                "so expected collectible COD is NULL, not the full order COD."))
            return None
        return partial
    if outcome in ("returned", "cancelled", "superseded"):
        return 0  # verified 0 for a confirmed return / cancellation / superseded
    findings.append(_dq("DQ_MISSING_OUTCOME",
                        f"Unsupported outcome value: {outcome!r}."))
    return None


def _dq(code: str, message: str) -> dict:
    return {"code": code, "kind": "data_quality", "amount_poisha": None,
            "basis": None, "message": message}


def _fin(code: str, amount, basis: str, message: str) -> dict:
    return {"code": code, "kind": "financial", "amount_poisha": amount,
            "basis": basis, "message": message}


# ---------------------------------------------------------------------------
# Core reconciliation — one shipment (V2.2.1)
# ---------------------------------------------------------------------------

def reconcile_shipment(shipment: dict, tolerance_poisha: int = 0) -> dict:
    """Reconcile one shipment. Input is a plain dict (see README schema).

    Returns a dict with every intermediate value, the findings list, and
    three separate statuses. All money is int poisha or None.
    """
    s = dict(shipment)
    findings: list = []

    # --- fixture / rule (configurable, never hardcoded real rates) -------
    fixture_name = s.get("courier_fixture") or "Fixture Alpha"
    fixture = FIXTURES.get(fixture_name, {})
    cod_fee_base = s.get("cod_fee_base") or fixture.get("cod_fee_base") or "full_cod"
    # M1 port fix: capture the raw rate BEFORE the cleaning loop. The rate
    # itself is resolved after cleaning (below), so a non-integer rate is
    # cleaned to None + DQ_INVALID_VALUE instead of raising TypeError, and
    # the fixture fallback applies only when no rate was supplied at all.
    _raw_rate_bp = s.get("cod_fee_rate_bp")
    _unsupported_base = cod_fee_base not in COD_FEE_BASES
    if _unsupported_base:
        findings.append(_dq("DQ_UNSUPPORTED_BASE",
                            f"Unsupported COD fee base: {cod_fee_base!r}."))
        cod_fee_base = "full_cod"  # only for labelling; fees stay None below

    # --- expected collectible E ------------------------------------------
    # Clean numeric fields first: a stray string (CSV invalid value) is
    # NULL + DQ_INVALID_VALUE, never a silent zero.
    for _f in ("amount_to_collect_poisha", "documented_partial_poisha",
               "reported_collected_poisha", "expected_delivery_fee_poisha",
               "reported_delivery_fee_poisha", "contractual_delivery_fee_poisha",
               "other_fees_poisha", "return_fee_poisha", "excluded_tax_poisha",
               "reported_excluded_tax_poisha", "reported_deductions_total_poisha",
               "reported_payout_poisha", "cod_fee_rate_bp", "vat_rate_bp"):
        if _f in s:
            s[_f] = _clean_int(s[_f], _f, findings)
    rate_bp = s.get("cod_fee_rate_bp")
    if rate_bp is None and _raw_rate_bp is None:
        # No rate supplied at all: fall back to the fixture's rate.
        rate_bp = fixture.get("cod_fee_rate_bp", 100)
    if _unsupported_base:
        rate_bp = None
    E = expected_collectible(s, findings)
    R = s.get("reported_collected_poisha")
    D_e = s.get("expected_delivery_fee_poisha")
    D_r = s.get("reported_delivery_fee_poisha")
    D_c = s.get("contractual_delivery_fee_poisha")
    # D_c confirmation is explicit-only (P0 patch, issue 2): a D_c value
    # alone is NOT evidence of merchant confirmation. The flag must be
    # explicitly True (bool True, or "yes"/"true"/"1"/"y" from CSV) AND a
    # non-empty evidence reference must be supplied. Missing flag, a false
    # flag, or missing evidence all mean D_c is not contractual.
    _flag = s.get("contractual_delivery_fee_confirmed")
    if isinstance(_flag, bool):
        d_c_flag = _flag
    elif isinstance(_flag, str):
        d_c_flag = _flag.strip().lower() in ("yes", "true", "1", "y")
    else:
        d_c_flag = False
    _ev = s.get("contractual_delivery_fee_evidence")
    d_c_evidence = str(_ev).strip() if _ev is not None else ""
    d_c_confirmed = bool(d_c_flag and d_c_evidence)
    O = s.get("other_fees_poisha")
    T_e = s.get("excluded_tax_poisha")
    T_r = s.get("reported_excluded_tax_poisha", T_e)
    P_r = s.get("reported_payout_poisha")

    # Return-fee handling: a return fee is part of other expected fees
    # only when explicitly provided; it never implies payment occurred.
    return_fee = s.get("return_fee_poisha")
    if return_fee is not None and O is not None:
        O = O + return_fee
    elif return_fee is not None and O is None:
        O = return_fee

    # --- fee lines (V2.2.1 §3) ------------------------------------------
    fee_lines = []
    line_values = {}
    line_blocked = False
    for line in s.get("fee_lines") or []:
        parsed, state, raw_text = parse_money(
            line.get("raw"), bool(line.get("column_present", True)))
        fee_type = line.get("fee_type") or "unknown"
        fee_lines.append({"fee_type": fee_type, "state": state,
                          "parsed_poisha": parsed, "raw_text": raw_text})
        line_values[fee_type] = parsed
        if state in ("missing_column", "blank_value", "invalid_value"):
            line_blocked = True
            code = {"missing_column": "DQ_MISSING_COLUMN",
                    "blank_value": "DQ_BLANK_VALUE",
                    "invalid_value": "DQ_INVALID_VALUE"}[state]
            findings.append(_dq(code,
                                f"Fee line {fee_type!r} is {state.replace('_', ' ')}; "
                                "its value is NULL, never verified zero."
                                + (f" Raw text preserved: {raw_text!r}."
                                   if state == "invalid_value" else "")))
        if fee_type == "delivery" and parsed is not None and D_r is None:
            D_r = parsed

    # --- X_r: reported deductions ----------------------------------------
    x_r_total = s.get("reported_deductions_total_poisha")
    if fee_lines and not line_blocked:
        x_r_lines = sum(v for v in line_values.values())
    else:
        x_r_lines = None
    if x_r_total is not None:
        X_r = x_r_total
        if x_r_lines is not None and x_r_lines != x_r_total:
            findings.append(_fin("STATEMENT", x_r_lines - x_r_total, "line_vs_total",
                                 "Sum of reported fee lines differs from the stated "
                                 "total deductions; both figures are preserved."))
    elif fee_lines:
        X_r = x_r_lines  # None when any required line is unparsed
    else:
        X_r = None

    # --- VAT / tax mode ----------------------------------------------------
    vat_mode = s.get("vat_mode") or "unknown"
    vat_breakdown = None
    vat_blocked = False
    if vat_mode == "included":
        vat_breakdown = included_tax_breakdown(
            D_e, s.get("vat_formula"), s.get("vat_rate_bp"))
        # Included-mode / unknown-rate exception: breakdown None, but the
        # gross fee still counts once — do not block the payout.
    elif vat_mode == "unknown":
        tax_in_play = (T_e not in (None, 0)) or (T_r not in (None, 0))
        if tax_in_play:
            vat_blocked = True
            findings.append(_dq("DQ_UNKNOWN_VAT",
                                "VAT mode is unknown and a non-zero tax amount is "
                                "in play, so an exact payout cannot be calculated."))

    # --- COD fees (V2.2.1 §1) --------------------------------------------
    F_e = F_R = None
    d_c_missing = False
    if rate_bp is None:
        F_e = F_R = None
    elif cod_fee_base == "full_cod":
        F_e = cod_fee(E, rate_bp)
        F_R = cod_fee(R, rate_bp)
    elif cod_fee_base == "reported_collected":
        # Same arithmetic as full_cod, but F_e/P_e are scenario-only.
        F_e = cod_fee(E, rate_bp)
        F_R = cod_fee(R, rate_bp)
    elif cod_fee_base == "cod_minus_delivery_fee":
        if D_c is None or not d_c_confirmed:
            d_c_missing = True
            if D_c is None:
                _reason = "no D_c value was supplied"
            elif not d_c_flag:
                _reason = ("the confirmation flag is missing or false — "
                           "a D_c value alone is not merchant confirmation")
            else:
                _reason = ("the confirmation flag is set but no confirmation "
                           "evidence was supplied")
            findings.append(_dq("DQ_MISSING_CONTRACT",
                                "cod_minus_delivery_fee needs a merchant-confirmed "
                                "contractual delivery fee (D_c) with evidence. "
                                f"{_reason[0].upper() + _reason[1:]}, so no COD fee "
                                "is calculated — no fallback to the expected or "
                                "reported delivery fee."))
            F_e = F_R = None
        else:
            F_e = (round_half_up_fee(max(E - D_c, 0), rate_bp)
                   if E is not None else None)
            F_R = (round_half_up_fee(max(R - D_c, 0), rate_bp)
                   if R is not None else None)

    # --- Expected / conditional payouts ------------------------------------
    def _sum(*vals):
        return None if any(v is None for v in vals) else sum(vals)

    X_e = None if vat_blocked else _sum(D_e, F_e, O, T_e)
    X_R = None if vat_blocked else _sum(D_e, F_R, O, T_r)
    P_e = None if (E is None or X_e is None) else E - X_e
    P_cond = None if (R is None or X_R is None) else R - X_R

    # --- Variances ----------------------------------------------------------
    C = None if (R is None or E is None) else R - E
    delta = None if (X_R is None or X_e is None) else X_R - X_e
    V_R = None if (X_r is None or X_R is None) else X_r - X_R
    S = None if (P_r is None or R is None or X_r is None) else P_r - (R - X_r)
    V_total = None if (P_r is None or P_e is None) else P_r - P_e
    V_cond = None if (P_r is None or P_cond is None) else P_r - P_cond

    # Identity validation (asserted in code and in tests whenever defined).
    if None not in (V_total, C, delta, V_R, S):
        assert V_total == C - delta - V_R + S, "V_total identity failed"
    if None not in (V_cond, S, V_R):
        assert V_cond == S - V_R, "V_cond identity failed"

    is_scenario = cod_fee_base == "reported_collected"

    # --- Financial findings (list; never netted, none hides another) -------
    if C is not None and C != 0:
        findings.append(_fin("COLLECTION", C, "reported_vs_expected",
                             "Under-collection" if C < 0 else "Over-collection"))
    if V_R is not None and V_R != 0:
        findings.append(_fin("FEE_TAX", V_R, "reported_basis",
                             "Reported deductions differ from recomputed deductions "
                             "on the reported basis."))
    if S is not None and S != 0:
        findings.append(_fin("STATEMENT", S, "statement_arithmetic",
                             "The statement's own numbers do not add up; this is a "
                             "statement inconsistency, not a confirmed loss."))
    if V_cond is not None and V_cond != 0:
        findings.append(_fin("PAYOUT_COND", V_cond, "contractual",
                             "Reported payout differs from the payout conditional "
                             "on reported collection."))
    if V_total is not None and V_total != 0:
        findings.append(_fin("SCENARIO", V_total,
                             "scenario" if is_scenario else "expectation",
                             "Scenario-only shortfall vs the expected-collection "
                             "scenario." if is_scenario else
                             "Total shortfall vs expected payout."))

    # --- Three separate statuses (V2.2.1 §4) ------------------------------
    has_dq = any(f["kind"] == "data_quality" for f in findings)
    required_none = any(v is None for v in (C, V_R, S, V_cond, V_total))
    # A required NULL is a data-quality gap even without an explicit DQ code.
    if has_dq or required_none:
        data_quality_status = "needs_review"
    else:
        data_quality_status = "complete"

    if V_cond is None:
        contractual_status = "unknown"
    elif V_cond == 0:
        contractual_status = "matched"
    else:
        contractual_status = "mismatch"

    # --- Overall status model (V2.2.1 §4, clarified in the P0 patch) -----
    # Five distinct overall values; the label never hides a finding.
    #   needs_review: any data-quality finding, or any required value NULL.
    #   mismatch: a FEE_TAX, STATEMENT or PAYOUT_COND finding is non-zero
    #     outside tolerance. This is a fee / statement / contractual
    #     payout failure — a collection-only case is NOT a mismatch.
    #   open: the only non-zero financial findings outside tolerance are
    #     COLLECTION and/or SCENARIO. Contractual payout stays matched,
    #     fee and statement stay consistent, but collection is open.
    #   matched_with_tolerance: non-zero findings exist but all are within
    #     tolerance (each finding is still listed, gross amount shown).
    #   matched: no non-zero financial finding, no DQ finding, no NULL.
    # Precedence for the label only: needs_review > mismatch > open >
    # matched_with_tolerance > matched. Findings are always preserved.
    financial = [f for f in findings if f["kind"] == "financial"]
    nonzero = [f for f in financial if f["amount_poisha"] not in (None, 0)]
    _mismatch_codes = {"FEE_TAX", "STATEMENT", "PAYOUT_COND"}
    _collection_codes = {"COLLECTION", "SCENARIO"}
    if has_dq or required_none:
        overall = "needs_review"
    elif any(f["code"] in _mismatch_codes
             and abs(f["amount_poisha"]) > tolerance_poisha for f in nonzero):
        overall = "mismatch"
    elif any(f["code"] in _collection_codes
             and abs(f["amount_poisha"]) > tolerance_poisha for f in nonzero):
        overall = "open"
    elif nonzero:
        overall = "matched_with_tolerance"
    else:
        overall = "matched"

    # Returned shipments: per-line payout is NULL; the expected outcome
    # (which may be negative) is an amount owed / to be offset at batch
    # level, not a bank settlement. No bank variance exists in P0.
    outcome = (s.get("outcome") or "").strip() or None
    expected_outcome = P_e  # may be negative for a confirmed return
    batch_amount_owed = -expected_outcome if (
        outcome in ("returned", "cancelled", "superseded")
        and expected_outcome is not None and expected_outcome < 0) else None

    return {
        "merchant_order_id": s.get("merchant_order_id"),
        "tracking_id": s.get("tracking_id"),
        "courier_fixture": fixture_name,
        "cod_fee_base": cod_fee_base,
        "cod_fee_rate_bp": rate_bp,
        "is_scenario": is_scenario,
        "outcome": outcome,
        "amount_to_collect_poisha": s.get("amount_to_collect_poisha"),
        "E": E, "R": R,
        "D_e": D_e, "D_r": D_r, "D_c": D_c if not d_c_missing else None,
        "D_c_confirmed": bool(d_c_confirmed) and not d_c_missing,
        "D_c_evidence": d_c_evidence if d_c_evidence else None,
        "F_e": F_e, "F_R": F_R,
        "O": O, "T_e": T_e, "T_r": T_r,
        "X_e": X_e, "X_R": X_R, "X_r": X_r,
        "X_r_total": x_r_total, "X_r_lines": x_r_lines,
        "P_e": P_e, "P_cond": P_cond, "P_r": P_r,
        "C": C, "delta": delta, "V_R": V_R, "S": S,
        "V_total": V_total, "V_cond": V_cond,
        "vat_mode": vat_mode, "vat_breakdown": vat_breakdown,
        "fee_lines": fee_lines,
        "findings": findings,
        "contractual_payout_status": contractual_status,
        "data_quality_status": data_quality_status,
        "overall_status": overall,
        "expected_outcome_poisha": expected_outcome,
        "batch_amount_owed_poisha": batch_amount_owed,
        "note": s.get("note"),
    }


def reconcile_batch(shipments: list, tolerance_poisha: int = 0) -> dict:
    """Reconcile a batch, flagging duplicate tracking IDs (preserved, never merged)."""
    results = [reconcile_shipment(s, tolerance_poisha) for s in shipments]
    seen: dict = {}
    for r in results:
        tid = r.get("tracking_id")
        if tid in (None, ""):
            continue
        if tid in seen:
            for dup in (seen[tid], r):
                if not any(f["code"] == "DQ_DUPLICATE_TRACKING" for f in dup["findings"]):
                    dup["findings"].append(_dq(
                        "DQ_DUPLICATE_TRACKING",
                        f"Tracking ID {tid!r} appears more than once. Both rows "
                        "are preserved and never merged or summed."))
                    dup["data_quality_status"] = "needs_review"
                    dup["overall_status"] = "needs_review"
        else:
            seen[tid] = r
    return {"results": results, "count": len(results)}
