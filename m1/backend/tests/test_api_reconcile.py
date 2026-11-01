"""Smoke tests for the thin /api/reconcile adapter (M1 Milestone 3).

The endpoint returns the computed result only and persists nothing — there
is no run store yet. These tests check the HTTP shape and that the engine
result flows through unchanged.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.reconcile import router


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _shipment(**over):
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


def test_post_reconcile_matched_shipment():
    r = _client().post(
        "/api/reconcile",
        json={"shipments": [_shipment()], "tolerance_poisha": 0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    result = body["results"][0]
    assert result["overall_status"] == "matched"
    assert result["V_total"] == 0
    # No run id / persistence marker: the endpoint returns the computed
    # result only in Milestone 3.
    assert "run_id" not in body and "run_id" not in result


def test_post_reconcile_open_collection_only():
    r = _client().post(
        "/api/reconcile",
        json={"shipments": [_shipment(
            reported_collected_poisha=95000,
            reported_deductions_total_poisha=7950,
            reported_payout_poisha=87050)]},
    )
    assert r.status_code == 200
    assert r.json()["results"][0]["overall_status"] == "open"


def test_post_reconcile_empty_batch():
    r = _client().post("/api/reconcile", json={"shipments": []})
    assert r.status_code == 200
    assert r.json() == {"results": [], "count": 0}


def test_get_rules_returns_synthetic_fixtures():
    r = _client().get("/api/rules")
    assert r.status_code == 200
    fixtures = r.json()["fixtures"]
    assert {f["courier_fixture"] for f in fixtures} == {
        "Fixture Alpha", "Fixture Beta", "Fixture Gamma"}
    for f in fixtures:
        assert "SYNTHETIC FIXTURE" in f["label"]
        assert f["cod_fee_rate_bp"] == 100
