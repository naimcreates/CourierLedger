"""M1 Milestone 3 — port of the P0 engine tests (AT-1..AT-12 + P0 patch tests).

Ported from prototype-p0/tests/test_engine.py (READ-ONLY source). Imports
adapted to the M1 domain module path (app.domain.engine). Worked examples
keep their exact P0 numbers; citations refer to the financial authority
(V2.2.1 Ex1-Ex6, V2.2 Ex A-D decomposition family).

Changes vs the P0 test file (deliberate, documented):
- test_at12_runs_are_immutable (P0) used app.store, the run store. M1
  Milestone 3 has no run store yet (the /api/reconcile endpoint returns
  computed results only and persists nothing), so that test is NOT ported.
  It is replaced by test_m1_input_dict_not_mutated and
  test_patch_deterministic_output_for_identical_inputs below.
- test_m1_invalid_cod_fee_rate_is_null_not_a_crash covers the genuine P0
  bug fixed in this port: a non-int cod_fee_rate_bp raised TypeError
  instead of becoming NULL + DQ_INVALID_VALUE (see the engine docstring).

All data is synthetic. No test is claimed to pass without execution.
"""
from app.domain.engine import (
    FIXTURES,
    cod_fee,
    included_tax_breakdown,
    parse_money,
    reconcile_batch,
    reconcile_shipment,
    round_half_up_fee,
)


def base(**over):
    """A clean matched delivered shipment (Fixture Alpha, full_cod, 100bp)."""
    s = {
        "merchant_order_id": "ORD-T",
        "tracking_id": "TRK-T",
        "courier_fixture": "Fixture Alpha",
        "outcome": "delivered",
        "amount_to_collect_poisha": 100000,
        "reported_collected_poisha": 100000,
        "expected_delivery_fee_poisha": 7000,
        "reported_delivery_fee_poisha": 7000,
        "cod_fee_base": "full_cod",
        "cod_fee_rate_bp": 100,
        "other_fees_poisha": 0,
        "excluded_tax_poisha": 0,
        "reported_deductions_total_poisha": 8000,
        "reported_payout_poisha": 92000,
    }
    s.update(over)
    return s


def codes(result):
    return [f["code"] for f in result["findings"]]


def assert_identities(r):
    """V2.2 identities, asserted whenever all terms are defined."""
    if None not in (r["V_total"], r["C"], r["delta"], r["V_R"], r["S"]):
        assert r["V_total"] == r["C"] - r["delta"] - r["V_R"] + r["S"]
    if None not in (r["V_cond"], r["S"], r["V_R"]):
        assert r["V_cond"] == r["S"] - r["V_R"]


# --- AT-1: V2.2.1 Ex1, D_e vs D_r differ, D_c confirmed --------------------
def test_at1_cod_minus_delivery_fee():
    r = reconcile_shipment(base(
        courier_fixture="Fixture Beta", cod_fee_base="cod_minus_delivery_fee",
        reported_delivery_fee_poisha=9000,
        contractual_delivery_fee_poisha=7000,
        contractual_delivery_fee_confirmed=True,
        contractual_delivery_fee_evidence="fixture-rate-card-2026-10-31",
        reported_deductions_total_poisha=9930,
        reported_payout_poisha=90070))
    assert r["F_e"] == 930 and r["F_R"] == 930
    assert r["V_R"] == 2000 and r["V_total"] == -2000 and r["V_cond"] == -2000
    assert r["C"] == 0 and r["S"] == 0
    assert r["overall_status"] == "mismatch"
    assert_identities(r)


# --- AT-2: V2.2.1 Ex5, D_c unknown -----------------------------------------
def test_at2_dc_unknown_no_fallback():
    r = reconcile_shipment(base(
        courier_fixture="Fixture Beta", cod_fee_base="cod_minus_delivery_fee",
        reported_collected_poisha=90000,
        contractual_delivery_fee_poisha=None,
        contractual_delivery_fee_confirmed=False,
        reported_deductions_total_poisha=8000,
        reported_payout_poisha=82000))
    assert r["F_e"] is None and r["F_R"] is None
    assert r["P_e"] is None and r["P_cond"] is None
    assert r["V_total"] is None and r["V_cond"] is None
    assert r["C"] == -10000 and r["S"] == 0
    assert "DQ_MISSING_CONTRACT" in codes(r)
    assert r["overall_status"] == "needs_review"
    # And with D_c present but explicitly unconfirmed, still no fallback.
    r2 = reconcile_shipment(base(
        courier_fixture="Fixture Beta", cod_fee_base="cod_minus_delivery_fee",
        reported_collected_poisha=90000,
        contractual_delivery_fee_poisha=7000,
        contractual_delivery_fee_confirmed=False,
        reported_deductions_total_poisha=8000,
        reported_payout_poisha=82000))
    assert r2["F_R"] is None and r2["overall_status"] == "needs_review"


# --- AT-3: V2.2.1 Ex2, reported_collected scenario-only ---------------------
def test_at3_scenario_never_triggers_contractual_mismatch():
    r = reconcile_shipment(base(
        courier_fixture="Fixture Gamma", cod_fee_base="reported_collected",
        reported_collected_poisha=95000,
        reported_deductions_total_poisha=7950,
        reported_payout_poisha=87050))
    assert r["V_total"] == -4950 and r["is_scenario"] is True
    assert r["V_cond"] == 0
    assert r["contractual_payout_status"] == "matched"
    assert r["C"] == -5000
    assert "SCENARIO" in codes(r)
    # P0 patch: collection-only is `open`, never a contractual mismatch.
    assert r["overall_status"] == "open"
    assert_identities(r)


# --- AT-4: V_R only -> V_cond = -V_R (V2.2 Ex A-D decomposition family) -----
def test_at4_fee_variance_only():
    r = reconcile_shipment(base(
        amount_to_collect_poisha=200000, reported_collected_poisha=200000,
        expected_delivery_fee_poisha=6000, reported_delivery_fee_poisha=8000,
        reported_deductions_total_poisha=10000, reported_payout_poisha=190000))
    assert r["V_R"] == 2000 and r["S"] == 0
    assert r["V_cond"] == -2000  # == S - V_R
    assert_identities(r)


# --- AT-5: S only -> V_cond = S (V2.2 Ex A-D decomposition family) ----------
def test_at5_statement_only():
    r = reconcile_shipment(base(
        amount_to_collect_poisha=150000, reported_collected_poisha=150000,
        expected_delivery_fee_poisha=6000,
        reported_deductions_total_poisha=7500, reported_payout_poisha=140000))
    assert r["V_R"] == 0 and r["S"] == -2500
    assert r["V_cond"] == -2500  # == S - V_R
    assert_identities(r)


# --- AT-6: blank fee line ---------------------------------------------------
def test_at6_blank_line_totals_matched_but_needs_review():
    r = reconcile_shipment(base(
        amount_to_collect_poisha=150000, reported_collected_poisha=150000,
        expected_delivery_fee_poisha=6000,
        reported_deductions_total_poisha=7500, reported_payout_poisha=142500,
        fee_lines=[
            {"fee_type": "delivery", "raw": 6000, "column_present": True},
            {"fee_type": "cod_fee", "raw": "", "column_present": True},
            {"fee_type": "other", "raw": 0, "column_present": True},
        ]))
    assert r["V_R"] == 0 and r["S"] == 0 and r["V_cond"] == 0
    assert "DQ_BLANK_VALUE" in codes(r)
    assert r["overall_status"] == "needs_review"


# --- AT-7: absent column + absent total --------------------------------------
def test_at7_absent_column_and_total():
    r = reconcile_shipment(base(
        amount_to_collect_poisha=150000, reported_collected_poisha=150000,
        expected_delivery_fee_poisha=6000,
        reported_deductions_total_poisha=None, reported_payout_poisha=142500,
        fee_lines=[
            {"fee_type": "delivery", "raw": None, "column_present": False},
            {"fee_type": "cod_fee", "raw": None, "column_present": False},
        ]))
    assert r["X_r"] is None and r["V_R"] is None and r["S"] is None
    assert r["V_cond"] == 0  # still calculable from P_r and P_cond
    assert r["contractual_payout_status"] == "matched"
    assert "DQ_MISSING_COLUMN" in codes(r)
    assert r["overall_status"] == "needs_review"


# --- AT-8: invalid value ------------------------------------------------------
def test_at8_invalid_raw_preserved_never_zero():
    r = reconcile_shipment(base(
        fee_lines=[
            {"fee_type": "delivery", "raw": 6000, "column_present": True},
            {"fee_type": "cod_fee", "raw": "1,5x0", "column_present": True},
        ]))
    line = next(f for f in r["fee_lines"] if f["fee_type"] == "cod_fee")
    assert line["state"] == "invalid_value"
    assert line["parsed_poisha"] is None and line["raw_text"] == "1,5x0"
    assert "DQ_INVALID_VALUE" in codes(r)
    assert r["overall_status"] == "needs_review"


# --- AT-9: explicit zero (V2.2.1 Ex6) and blank variant ----------------------
def test_at9_explicit_zero_return():
    r = reconcile_shipment(base(
        outcome="returned", amount_to_collect_poisha=80000,
        reported_collected_poisha=0, expected_delivery_fee_poisha=7000,
        other_fees_poisha=0, return_fee_poisha=0,
        reported_deductions_total_poisha=7000, reported_payout_poisha=None))
    assert r["E"] == 0
    assert r["expected_outcome_poisha"] == -7000
    assert r["batch_amount_owed_poisha"] == 7000
    assert r["P_r"] is None  # per-line payout is NULL for a return
    # Blank return fee instead of explicit zero -> outcome cannot be fixed.
    r2 = reconcile_shipment(base(
        outcome="returned", amount_to_collect_poisha=80000,
        reported_collected_poisha=0, expected_delivery_fee_poisha=7000,
        other_fees_poisha=None, return_fee_poisha=None,
        reported_deductions_total_poisha=7000, reported_payout_poisha=None))
    assert r2["X_e"] is None and r2["expected_outcome_poisha"] is None


# --- AT-10: V2.2.1 Ex4 multi-discrepancy decomposition -----------------------
def test_at10_multi_discrepancy_decomposition():
    r = reconcile_shipment(base(
        amount_to_collect_poisha=200000, reported_collected_poisha=190000,
        expected_delivery_fee_poisha=6000, reported_delivery_fee_poisha=8000,
        reported_deductions_total_poisha=9900, reported_payout_poisha=180000))
    assert r["C"] == -10000 and r["V_R"] == 2000 and r["S"] == -100
    assert r["V_cond"] == -2100 and r["V_total"] == -12000
    assert r["V_total"] == r["C"] - r["delta"] - r["V_R"] + r["S"]
    assert r["V_cond"] == r["S"] - r["V_R"]
    for code in ("COLLECTION", "FEE_TAX", "STATEMENT", "PAYOUT_COND", "SCENARIO"):
        assert code in codes(r)
    assert r["overall_status"] == "mismatch"
    assert_identities(r)


# --- AT-11: tolerance -----------------------------------------------------------
def test_at11_tolerance_lists_finding_and_never_matches_none():
    s = base(amount_to_collect_poisha=200000, reported_collected_poisha=190000,
             expected_delivery_fee_poisha=6000, reported_delivery_fee_poisha=8000,
             reported_deductions_total_poisha=9900, reported_payout_poisha=180000)
    assert reconcile_shipment(s, tolerance_poisha=0)["overall_status"] == "mismatch"
    within = reconcile_shipment(s, tolerance_poisha=20000)
    assert within["overall_status"] == "matched_with_tolerance"
    assert within["V_total"] == -12000  # gross amount still shown
    assert "PAYOUT_COND" in codes(within)
    unknown = reconcile_shipment(base(
        courier_fixture="Fixture Beta", cod_fee_base="cod_minus_delivery_fee",
        contractual_delivery_fee_poisha=None,
        contractual_delivery_fee_confirmed=False), tolerance_poisha=10**9)
    assert unknown["overall_status"] == "needs_review"  # NULL never matched


# --- AT-12: rounding + (adapted) immutability ----------------------------------
def test_at12_rounding_half_up():
    assert round_half_up_fee(1050, 100) == 11
    assert cod_fee(1050, 100) == 11
    assert round_half_up_fee(100, 100) == 1
    assert round_half_up_fee(0, 100) == 0


def test_m1_input_dict_not_mutated():
    """M1 replacement for the P0 run-store immutability test (no store yet).

    reconcile_shipment must not mutate the caller's shipment dict, and
    reconcile_batch must not mutate the caller's shipment dicts.
    """
    s = base()
    snapshot = dict(s)
    reconcile_shipment(s)
    assert s == snapshot
    batch_in = [base(), base(tracking_id="TRK-2")]
    batch_snapshot = [dict(x) for x in batch_in]
    reconcile_batch(batch_in)
    assert batch_in == batch_snapshot


# --- Boundary values --------------------------------------------------------------
def test_boundary_zero_cod_zero_rate_and_below_dc():
    zero = reconcile_shipment(base(amount_to_collect_poisha=0,
                                  reported_collected_poisha=0,
                                  expected_delivery_fee_poisha=0,
                                  reported_deductions_total_poisha=0,
                                  reported_payout_poisha=0))
    assert zero["overall_status"] == "matched" and zero["F_R"] == 0
    no_rate = reconcile_shipment(base(cod_fee_rate_bp=0,
                                     reported_deductions_total_poisha=7000,
                                     reported_payout_poisha=93000))
    assert no_rate["F_e"] == 0 and no_rate["overall_status"] == "matched"
    below = reconcile_shipment(base(
        courier_fixture="Fixture Beta", cod_fee_base="cod_minus_delivery_fee",
        reported_collected_poisha=5000,
        contractual_delivery_fee_poisha=7000,
        contractual_delivery_fee_confirmed=True,
        contractual_delivery_fee_evidence="fixture-rate-card-2026-10-31",
        reported_deductions_total_poisha=7000, reported_payout_poisha=-2000))
    assert below["F_R"] == 0  # max(R - D_c, 0)
    assert below["F_e"] == 930


def test_boundary_large_values_integer_only():
    r = reconcile_shipment(base(
        amount_to_collect_poisha=10_000_000_000,
        reported_collected_poisha=10_000_000_000,
        expected_delivery_fee_poisha=700000,
        reported_deductions_total_poisha=100700000,
        reported_payout_poisha=9899300000))
    assert r["F_R"] == 100_000_000
    assert all(isinstance(v, int) for v in
               (r["F_R"], r["X_e"], r["P_e"], r["V_total"]) if v is not None)
    assert r["overall_status"] == "matched"


# --- Duplicates / missing outcome / regression --------------------------------------
def test_duplicate_tracking_preserved_not_merged():
    batch = reconcile_batch([base(tracking_id="TRK-DUP"),
                             base(tracking_id="TRK-DUP",
                                  merchant_order_id="ORD-2",
                                  amount_to_collect_poisha=60000,
                                  reported_collected_poisha=60000,
                                  reported_deductions_total_poisha=5600,
                                  reported_payout_poisha=54400)])
    assert len(batch["results"]) == 2
    assert all("DQ_DUPLICATE_TRACKING" in codes(r) for r in batch["results"])
    assert all(r["overall_status"] == "needs_review" for r in batch["results"])


def test_missing_outcome_means_e_is_none():
    r = reconcile_shipment(base(outcome="", amount_to_collect_poisha=50000))
    assert r["E"] is None
    assert "DQ_MISSING_OUTCOME" in codes(r)
    assert r["overall_status"] == "needs_review"


def test_regression_delivered_returned_partial():
    delivered = reconcile_shipment(base())
    assert delivered["overall_status"] == "matched"
    returned = reconcile_shipment(base(outcome="returned",
                                      amount_to_collect_poisha=80000,
                                      reported_collected_poisha=0,
                                      reported_payout_poisha=None))
    assert returned["E"] == 0 and returned["expected_outcome_poisha"] == -7000
    partial = reconcile_shipment(base(
        outcome="partially_delivered", amount_to_collect_poisha=80000,
        documented_partial_poisha=60000, reported_collected_poisha=60000,
        other_fees_poisha=3500, reported_deductions_total_poisha=11100,
        reported_payout_poisha=48900))
    assert partial["E"] == 60000 and partial["P_e"] == 48900
    assert partial["overall_status"] == "matched"
    partial_unknown = reconcile_shipment(base(
        outcome="partially_delivered", amount_to_collect_poisha=80000,
        documented_partial_poisha=None))
    assert partial_unknown["E"] is None  # never the full order COD


def test_vat_included_breakdown_never_subtracted_twice():
    r = reconcile_shipment(base(
        expected_delivery_fee_poisha=11500,
        reported_deductions_total_poisha=12500, reported_payout_poisha=87500,
        vat_mode="included", vat_formula="rate_on_net", vat_rate_bp=1500))
    assert r["vat_breakdown"] == {"net": 10000, "tax": 1500}
    assert r["X_e"] == 12500 and r["P_e"] == 87500
    assert r["overall_status"] == "matched"


def test_vat_included_rate_on_gross_breakdown():
    r = reconcile_shipment(base(
        expected_delivery_fee_poisha=11500,
        reported_deductions_total_poisha=12500, reported_payout_poisha=87500,
        vat_mode="included", vat_formula="rate_on_gross", vat_rate_bp=1500))
    # tax = round_half_up(11500 * 1500 / 10000) = 1725; net = 11500 - 1725.
    assert r["vat_breakdown"] == {"net": 9775, "tax": 1725}
    assert r["X_e"] == 12500 and r["P_e"] == 87500
    assert r["overall_status"] == "matched"


def test_vat_unknown_mode_with_tax_blocks_exact_payout():
    r = reconcile_shipment(base(excluded_tax_poisha=1500))
    assert "DQ_UNKNOWN_VAT" in codes(r)
    assert r["X_e"] is None and r["P_e"] is None
    assert r["overall_status"] == "needs_review"


def test_fee_line_states_all_five():
    assert parse_money(None, False) == (None, "missing_column", None)
    assert parse_money(None, True) == (None, "blank_value", None)
    assert parse_money("1,5x0", True)[1] == "invalid_value"
    assert parse_money(5000, True) == (5000, "valid", "5000")
    assert parse_money(0, True) == (0, "explicit_zero", "0")
    assert parse_money("", True)[1] == "blank_value"
    assert parse_money(" 1,500 ", True) == (1500, "valid", "1,500")


# --- P0 patch: overall status model — open vs mismatch ----------------------
def test_patch_overall_collection_only_is_open_not_mismatch():
    # Ex2 (AT-3 shape) on full_cod: C != 0, V_R = S = V_cond = 0.
    r = reconcile_shipment(base(
        reported_collected_poisha=95000,
        reported_deductions_total_poisha=7950, reported_payout_poisha=87050))
    # full_cod here: F_R on R, so V_R = 7950 - (7000+950) = 0, V_cond = 0.
    assert r["C"] == -5000 and r["V_cond"] == 0
    assert r["contractual_payout_status"] == "matched"
    assert r["overall_status"] == "open"
    assert "COLLECTION" in codes(r)


def test_patch_overall_fee_only_is_mismatch():
    r = reconcile_shipment(base(
        amount_to_collect_poisha=200000, reported_collected_poisha=200000,
        expected_delivery_fee_poisha=6000, reported_delivery_fee_poisha=8000,
        reported_deductions_total_poisha=10000, reported_payout_poisha=190000))
    assert r["V_R"] == 2000 and r["C"] == 0
    assert r["contractual_payout_status"] == "mismatch"
    assert r["overall_status"] == "mismatch"


def test_patch_overall_statement_only_is_mismatch():
    r = reconcile_shipment(base(
        amount_to_collect_poisha=150000, reported_collected_poisha=150000,
        expected_delivery_fee_poisha=6000,
        reported_deductions_total_poisha=7500, reported_payout_poisha=140000))
    assert r["S"] == -2500 and r["C"] == 0
    assert r["overall_status"] == "mismatch"


def test_patch_overall_collection_plus_fee_is_mismatch_not_open():
    r = reconcile_shipment(base(
        amount_to_collect_poisha=200000, reported_collected_poisha=190000,
        expected_delivery_fee_poisha=6000, reported_delivery_fee_poisha=8000,
        reported_deductions_total_poisha=9900, reported_payout_poisha=180000))
    assert r["C"] == -10000 and r["V_R"] == 2000
    assert r["overall_status"] == "mismatch"
    assert "COLLECTION" in codes(r) and "FEE_TAX" in codes(r)


def test_patch_overall_collection_within_tolerance():
    r = reconcile_shipment(base(
        reported_collected_poisha=95000,
        reported_deductions_total_poisha=7950, reported_payout_poisha=87050),
        tolerance_poisha=6000)
    assert r["overall_status"] == "matched_with_tolerance"
    assert r["C"] == -5000  # gross amount still shown


# --- P0 patch: D_c confirmation must be explicit flag + evidence -----------
def _beta(**over):
    s = base(courier_fixture="Fixture Beta",
             cod_fee_base="cod_minus_delivery_fee",
             reported_collected_poisha=90000,
             contractual_delivery_fee_poisha=7000,
             reported_deductions_total_poisha=8000,
             reported_payout_poisha=82000)
    s.update(over)
    return s


def test_patch_dc_missing_flag_is_not_confirmation():
    # D_c value present, flag key absent entirely.
    s = _beta()
    s.pop("contractual_delivery_fee_confirmed", None)
    s["contractual_delivery_fee_evidence"] = "fixture-rate-card-2026-10-31"
    r = reconcile_shipment(s)
    assert r["F_R"] is None and r["F_e"] is None
    assert "DQ_MISSING_CONTRACT" in codes(r)
    assert r["overall_status"] == "needs_review"


def test_patch_dc_false_flag_is_not_confirmation():
    r = reconcile_shipment(_beta(
        contractual_delivery_fee_confirmed=False,
        contractual_delivery_fee_evidence="fixture-rate-card-2026-10-31"))
    assert r["F_R"] is None
    assert "DQ_MISSING_CONTRACT" in codes(r)
    assert r["overall_status"] == "needs_review"


def test_patch_dc_missing_evidence_is_not_confirmation():
    r = reconcile_shipment(_beta(
        contractual_delivery_fee_confirmed=True,
        contractual_delivery_fee_evidence=""))
    assert r["F_R"] is None
    assert "DQ_MISSING_CONTRACT" in codes(r)
    assert r["overall_status"] == "needs_review"
    r2 = reconcile_shipment(_beta(contractual_delivery_fee_confirmed=True))
    assert r2["F_R"] is None  # evidence key absent entirely
    assert "DQ_MISSING_CONTRACT" in codes(r2)


def test_patch_dc_valid_explicit_confirmation():
    r = reconcile_shipment(_beta(
        contractual_delivery_fee_confirmed=True,
        contractual_delivery_fee_evidence="fixture-rate-card-2026-10-31"))
    assert r["F_R"] == 830  # round(max(90000-7000, 0) * 1%)
    assert r["D_c_confirmed"] is True
    assert r["D_c_evidence"] == "fixture-rate-card-2026-10-31"
    assert "DQ_MISSING_CONTRACT" not in codes(r)


# --- P0 patch: determinism ---------------------------------------------------
def test_patch_deterministic_output_for_identical_inputs():
    s = base()
    a = reconcile_shipment(s)
    b = reconcile_shipment(base())
    assert a == b
    batch1 = reconcile_batch([base(), base(tracking_id="TRK-2")])
    batch2 = reconcile_batch([base(), base(tracking_id="TRK-2")])
    assert batch1 == batch2


# --- M1 port fix: invalid COD fee rate is NULL + DQ, never a crash ---------
def test_m1_invalid_cod_fee_rate_is_null_not_a_crash():
    """P0 bug fix (see engine docstring): in P0, cod_fee_rate_bp was read
    into a local before the cleaning loop, so a non-int rate raised
    TypeError. In M1 the rate goes through the same cleaning as every
    other money/rate field: invalid -> None + DQ_INVALID_VALUE, no fee
    calculated, no silent fallback to the fixture rate."""
    for bad_rate in ("100", True, 100.5, "1,5x0"):
        r = reconcile_shipment(base(cod_fee_rate_bp=bad_rate))
        assert r["F_e"] is None and r["F_R"] is None, bad_rate
        assert "DQ_INVALID_VALUE" in codes(r), bad_rate
        assert r["overall_status"] == "needs_review", bad_rate
    # Integral float rate is accepted by the cleaner, like other fields.
    r = reconcile_shipment(base(cod_fee_rate_bp=100.0))
    assert r["F_e"] == 1000 and r["F_R"] == 1000
    assert r["overall_status"] == "matched"
    # No rate supplied at all -> fixture rate still applies (100bp).
    s = base()
    s.pop("cod_fee_rate_bp")
    r = reconcile_shipment(s)
    assert r["cod_fee_rate_bp"] == 100 and r["F_e"] == 1000
    assert r["overall_status"] == "matched"


# --- Fixtures: synthetic only, labelled --------------------------------------
def test_fixtures_are_synthetic_and_labelled():
    assert set(FIXTURES) == {"Fixture Alpha", "Fixture Beta", "Fixture Gamma"}
    assert FIXTURES["Fixture Alpha"]["cod_fee_base"] == "full_cod"
    assert FIXTURES["Fixture Beta"]["cod_fee_base"] == "cod_minus_delivery_fee"
    assert FIXTURES["Fixture Gamma"]["cod_fee_base"] == "reported_collected"
    for name, fx in FIXTURES.items():
        assert fx["cod_fee_rate_bp"] == 100
        assert "SYNTHETIC FIXTURE" in fx["label"]
        assert "not a real courier contract" in fx["label"]
