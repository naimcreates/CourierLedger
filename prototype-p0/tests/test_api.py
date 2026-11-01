"""API tests (synthetic data only) using FastAPI TestClient."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _shipment(**over):
    s = {
        "merchant_order_id": "ORD-API", "tracking_id": "TRK-API",
        "courier_fixture": "Fixture Alpha", "outcome": "delivered",
        "amount_to_collect_poisha": 100000,
        "reported_collected_poisha": 100000,
        "expected_delivery_fee_poisha": 7000,
        "cod_fee_base": "full_cod", "cod_fee_rate_bp": 100,
        "other_fees_poisha": 0, "excluded_tax_poisha": 0,
        "reported_deductions_total_poisha": 8000,
        "reported_payout_poisha": 92000,
    }
    s.update(over)
    return s


def test_health_and_rules():
    assert client.get("/api/health").json()["status"] == "ok"
    rules = client.get("/api/rules").json()
    assert len(rules["fixtures"]) == 3
    assert all("SYNTHETIC" in f["label"] for f in rules["fixtures"])


def test_demo_endpoint():
    resp = client.get("/api/demo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 14 and body["run_id"]
    assert any(r["tracking_id"] == "TRK-0004" and r["V_total"] == -12000
               for r in body["results"])


def test_reconcile_json_and_run_retrieval():
    resp = client.post("/api/reconcile", json={"shipments": [_shipment()]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["overall_status"] == "matched"
    run = client.get(f"/api/runs/{body['run_id']}")
    assert run.status_code == 200
    assert run.json()["results"][0]["tracking_id"] == "TRK-API"
    # Re-running creates a new run; the first is unchanged.
    resp2 = client.post("/api/reconcile", json={"shipments": [_shipment()]})
    assert resp2.json()["run_id"] != body["run_id"]
    assert client.get(f"/api/runs/{body['run_id']}").json()["results"][0][
        "overall_status"] == "matched"


def test_reconcile_csv_upload():
    csv = ("tracking_id,outcome,amount_to_collect_poisha,reported_collected_poisha,"
           "expected_delivery_fee_poisha,other_fees_poisha,excluded_tax_poisha,"
           "reported_deductions_total_poisha,"
           "reported_payout_poisha\n"
           "TRK-CSV,delivered,100000,100000,7000,0,0,8000,92000\n")
    resp = client.post("/api/reconcile-csv",
                       files={"file": ("up.csv", csv, "text/csv")})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["overall_status"] == "matched"


def test_reconcile_rejects_empty():
    assert client.post("/api/reconcile", json={"shipments": []}).status_code == 400


def test_dashboard_loads():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "CourierLedger" in resp.text
    assert "Load synthetic demo dataset" in resp.text


# --- P0 patch: immutable runs, determinism, no edit/delete endpoints --------
def test_patch_no_edit_or_delete_run_endpoints():
    # There is no API for editing or deleting a historical run.
    # PUT / PATCH / DELETE on a run must not succeed (405/404, never 200).
    run = client.post("/api/reconcile", json={"shipments": [_shipment()]}).json()
    rid = run["run_id"]
    for method in ("put", "patch", "delete"):
        resp = getattr(client, method)(f"/api/runs/{rid}")
        assert resp.status_code in (404, 405), (method, resp.status_code)
    # The run is still there, unchanged, afterwards.
    assert client.get(f"/api/runs/{rid}").json()["results"][0][
        "overall_status"] == "matched"


def test_patch_earlier_run_unchanged_after_later_runs():
    first = client.post("/api/reconcile", json={"shipments": [_shipment()]}).json()
    first_snapshot = client.get(f"/api/runs/{first['run_id']}").json()
    # Later runs, including a different shipment, must not touch run 1.
    client.post("/api/reconcile", json={"shipments": [
        _shipment(tracking_id="TRK-LATER", reported_collected_poisha=90000,
                  reported_deductions_total_poisha=7950,
                  reported_payout_poisha=82050)]})
    client.get("/api/demo")
    again = client.get(f"/api/runs/{first['run_id']}").json()
    assert again == first_snapshot
    assert again["results"][0]["tracking_id"] == "TRK-API"
    assert again["results"][0]["overall_status"] == "matched"


def test_patch_deterministic_reconcile_identical_inputs():
    a = client.post("/api/reconcile", json={"shipments": [_shipment()]}).json()
    b = client.post("/api/reconcile", json={"shipments": [_shipment()]}).json()
    assert a["run_id"] != b["run_id"]  # new immutable run each time
    assert a["results"] == b["results"]  # identical results
    assert a["count"] == b["count"] == 1


def test_patch_demo_collection_only_is_open():
    body = client.get("/api/demo").json()
    by_id = {r["tracking_id"]: r for r in body["results"]}
    r2 = by_id["TRK-0002"]
    assert r2["contractual_payout_status"] == "matched"
    assert r2["overall_status"] == "open"
    assert r2["C"] == -5000 and r2["V_total"] == -4950
