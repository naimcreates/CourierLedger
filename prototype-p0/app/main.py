"""CourierLedger P0 disposable prototype — FastAPI app.

Local-only prototype. No auth, no real courier APIs, no bank CSV, no
real merchant data. Synthetic data and fictional fixtures only.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import store
from .csvio import parse_csv
from .engine import FIXTURES, reconcile_batch

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"

app = FastAPI(title="CourierLedger P0 Prototype (disposable)",
              description="Synthetic COD reconciliation only. Fictional fixtures; "
                          "not real courier contracts.")


@app.get("/api/health")
def health():
    return {"status": "ok", "prototype": "P0-disposable", "data": "synthetic-only"}


@app.get("/api/rules")
def rules():
    return {"fixtures": list(FIXTURES.values()),
            "note": "SYNTHETIC FIXTURE — not a real courier contract. "
                    "Rates are configurable test fixtures, never real rates."}


def _run(source: str, shipments, tolerance_poisha: int = 0):
    batch = reconcile_batch(shipments, tolerance_poisha)
    saved = store.save_run(source, shipments, batch["results"])
    return {"run_id": saved["run_id"], "created_at": saved["created_at"],
            "source": source, "count": batch["count"],
            "results": batch["results"]}


@app.get("/api/demo")
def demo():
    path = DATA / "demo.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="demo.csv not found")
    shipments, report = parse_csv(path.read_text(encoding="utf-8"))
    out = _run("demo.csv", shipments)
    out["csv_report"] = report
    return out


@app.post("/api/reconcile")
async def reconcile(payload: dict):
    shipments = payload.get("shipments")
    if not isinstance(shipments, list) or not shipments:
        raise HTTPException(status_code=400,
                            detail="Provide a non-empty 'shipments' list.")
    tolerance = payload.get("tolerance_poisha", 0)
    return _run("json", shipments, int(tolerance))


@app.post("/api/reconcile-csv")
async def reconcile_csv(file: UploadFile = File(...),
                        mapping: str = Form(default="{}"),
                        tolerance_poisha: int = Form(default=0)):
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    try:
        mapping_obj = json.loads(mapping) if mapping else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="mapping must be valid JSON")
    shipments, report = parse_csv(text, mapping_obj or None)
    if report.get("errors"):
        raise HTTPException(status_code=400, detail="; ".join(report["errors"]))
    out = _run(f"upload:{file.filename}", shipments, tolerance_poisha)
    out["csv_report"] = report
    return out


@app.get("/api/runs")
def runs(limit: int = 20):
    return {"runs": store.list_runs(limit)}


@app.get("/api/runs/{run_id}")
def get_one_run(run_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/shipments/{run_id}/{tracking_id}")
def shipment_detail(run_id: str, tracking_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    for r in run["results"]:
        if r.get("tracking_id") == tracking_id:
            return r
    raise HTTPException(status_code=404, detail="Shipment not found in run")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
