"""CSV parsing / mapping tests (synthetic data only)."""
from pathlib import Path

from app.csvio import build_mapping, parse_csv
from app.engine import reconcile_batch

DATA = Path(__file__).resolve().parent.parent / "data"


def test_demo_csv_parses_and_reconciles():
    shipments, report = parse_csv((DATA / "demo.csv").read_text())
    assert report["row_count"] == 14
    assert report["errors"] == []
    batch = reconcile_batch(shipments)
    by_id = {r["tracking_id"]: r for r in batch["results"]}
    assert by_id["TRK-0001"]["overall_status"] == "matched"
    assert by_id["TRK-0002"]["contractual_payout_status"] == "matched"
    assert by_id["TRK-0002"]["V_total"] == -4950
    assert by_id["TRK-0003"]["V_R"] == 2000
    assert by_id["TRK-0004"]["V_total"] == -12000
    assert by_id["TRK-0005"]["overall_status"] == "needs_review"
    assert by_id["TRK-0006"]["expected_outcome_poisha"] == -7000
    assert by_id["TRK-0007"]["P_e"] == 48900
    assert by_id["TRK-0008"]["overall_status"] == "needs_review"
    assert by_id["TRK-0009"]["fee_lines"][1]["state"] == "invalid_value"
    assert by_id["TRK-0010"]["S"] == -2500
    assert by_id["TRK-0013"]["E"] is None
    assert by_id["TRK-0014"]["vat_breakdown"] == {"net": 10000, "tax": 1500}
    dupes = [r for r in batch["results"] if r["tracking_id"] == "TRK-DUP-01"]
    assert len(dupes) == 2
    assert all(any(f["code"] == "DQ_DUPLICATE_TRACKING" for f in r["findings"])
               for r in dupes)


def test_edge_cases_csv():
    shipments, report = parse_csv((DATA / "edge_cases.csv").read_text())
    assert report["row_count"] == 10
    batch = reconcile_batch(shipments)
    by_id = {r["tracking_id"]: r for r in batch["results"]}
    assert by_id["TRK-E01"]["F_R"] == 0
    assert by_id["TRK-E02"]["F_R"] == 0
    assert by_id["TRK-E03"]["overall_status"] == "matched"
    assert by_id["TRK-E04"]["F_R"] == 100_000_000
    assert by_id["TRK-E05"]["E"] is None
    assert by_id["TRK-E06"]["X_e"] is None
    assert by_id["TRK-E07"]["R"] is None
    assert any(f["code"] == "DQ_INVALID_VALUE"
               for f in by_id["TRK-E07"]["findings"])
    assert by_id["TRK-E09"]["F_R"] == 11


def test_all_five_fee_line_states():
    text = (
        "tracking_id,outcome,amount_to_collect_poisha,reported_collected_poisha,"
        "expected_delivery_fee_poisha,reported_delivery_fee_poisha,"
        "reported_cod_fee_poisha,reported_deductions_total_poisha,reported_payout_poisha\n"
        "T1,delivered,100000,100000,7000,7000,1000,8000,92000\n"
        "T2,delivered,100000,100000,7000,7000,,8000,92000\n"
        "T3,delivered,100000,100000,7000,7000,\"1,5x0\",8000,92000\n"
    )
    shipments, _ = parse_csv(text)
    batch = reconcile_batch(shipments)
    states = [r["fee_lines"][1]["state"] for r in batch["results"]]
    assert states == ["valid", "blank_value", "invalid_value"]
    # Absent column: cod fee column dropped entirely.
    text2 = ("tracking_id,outcome,amount_to_collect_poisha,reported_collected_poisha,"
             "expected_delivery_fee_poisha,reported_delivery_fee_poisha,"
             "reported_deductions_total_poisha,reported_payout_poisha\n"
             "T4,delivered,100000,100000,7000,7000,8000,92000\n")
    shipments2, report2 = parse_csv(text2)
    assert "reported_cod_fee_poisha" in report2["missing_fields"]


def test_duplicate_headers_rejected():
    _, report = parse_csv("tracking_id,tracking_id,outcome\nT1,delivered\n")
    assert report["errors"]


def test_column_mapping_renames_headers():
    text = ("order, tracking, outcome, cod, collected, del_fee, total, payout\n"
            "O1,T1,delivered,100000,100000,7000,8000,92000\n")
    mapping = {"order": "merchant_order_id", "tracking": "tracking_id",
               "cod": "amount_to_collect_poisha",
               "collected": "reported_collected_poisha",
               "del_fee": "expected_delivery_fee_poisha",
               "total": "reported_deductions_total_poisha",
               "payout": "reported_payout_poisha"}
    shipments, report = parse_csv(text, mapping)
    assert report["row_count"] == 1
    assert shipments[0]["merchant_order_id"] == "O1"
    assert shipments[0]["amount_to_collect_poisha"] == 100000


def test_build_mapping_reports_unmapped_and_missing():
    _, unmapped, missing = build_mapping(["tracking_id", "weird_col"])
    assert unmapped == ["weird_col"]
    assert "merchant_order_id" in missing


# --- P0 patch: demo TRK-0002 is open; D_c evidence is parsed ------------------
def test_patch_demo_trk0002_is_open_and_trk0003_has_evidence():
    shipments, report = parse_csv((DATA / "demo.csv").read_text())
    assert report["row_count"] == 14
    batch = reconcile_batch(shipments)
    by_id = {r["tracking_id"]: r for r in batch["results"]}
    assert by_id["TRK-0002"]["overall_status"] == "open"
    assert by_id["TRK-0002"]["contractual_payout_status"] == "matched"
    assert by_id["TRK-0003"]["D_c_confirmed"] is True
    assert by_id["TRK-0003"]["D_c_evidence"] == "fixture-rate-card-2026-10-31"
    assert by_id["TRK-0003"]["overall_status"] == "mismatch"


def test_patch_csv_dc_without_evidence_is_needs_review():
    text = (
        "tracking_id,outcome,amount_to_collect_poisha,reported_collected_poisha,"
        "expected_delivery_fee_poisha,contractual_delivery_fee_poisha,"
        "contractual_delivery_fee_confirmed,cod_fee_base,"
        "reported_deductions_total_poisha,reported_payout_poisha\n"
        "T-DC,delivered,100000,90000,7000,7000,yes,cod_minus_delivery_fee,8000,82000\n")
    shipments, _ = parse_csv(text)
    assert shipments[0].get("contractual_delivery_fee_evidence") is None
    batch = reconcile_batch(shipments)
    r = batch["results"][0]
    assert r["F_R"] is None
    assert any(f["code"] == "DQ_MISSING_CONTRACT" for f in r["findings"])
    assert r["overall_status"] == "needs_review"
