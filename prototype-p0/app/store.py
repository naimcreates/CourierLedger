"""Immutable reconciliation-run store (SQLite, local file only).

Each reconciliation creates a NEW run row. There is no update or delete
path: historical runs are immutable by design (V2 invariant).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "runs.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    shipment_count INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    results_json TEXT NOT NULL
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def save_run(source: str, shipments, results) -> dict:
    run_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO runs (id, created_at, source, shipment_count,"
            " payload_json, results_json) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, created_at, source, len(shipments),
             json.dumps(shipments, default=str),
             json.dumps(results, default=str)),
        )
        conn.commit()
    finally:
        conn.close()
    return {"run_id": run_id, "created_at": created_at,
            "source": source, "shipment_count": len(shipments)}


def get_run(run_id: str):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, created_at, source, shipment_count, results_json"
            " FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"run_id": row[0], "created_at": row[1], "source": row[2],
            "shipment_count": row[3], "results": json.loads(row[4])}


def list_runs(limit: int = 20):
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, created_at, source, shipment_count FROM runs"
            " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [{"run_id": r[0], "created_at": r[1], "source": r[2],
             "shipment_count": r[3]} for r in rows]
