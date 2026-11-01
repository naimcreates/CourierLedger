"""Health endpoint.

GET /api/health -> {"status": "ok", "service": "courierledger-m1",
"database": "postgresql" | "sqlite-test-only" | "unavailable",
"database_ok": bool}

"database" reports "postgresql" ONLY when a real round-trip
(SELECT 1) to a PostgreSQL database succeeds. When no PostgreSQL
server is reachable it reports "unavailable" (or "sqlite-test-only"
when the temporary test-only SQLite fallback URL is configured and
reachable). The endpoint never claims the database is ok on the
basis of configuration alone.

GET /api/ready (Phase 2, Part 8) is the readiness probe and is
distinct from /health (liveness): it returns 200 only when the
database round-trip succeeds, and 503 when the database is
unreachable — so a load balancer / orchestrator can keep the
process alive (liveness) while refusing traffic until the
database is back. /health always returns 200 because the service
itself is up even when the database is down.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db.session import check_connection, database_kind

router = APIRouter()


@router.get("/health")
def health() -> dict:
    ok = check_connection()
    kind = database_kind()
    database = kind if ok else "unavailable"
    return {
        "status": "ok",
        "service": "courierledger-m1",
        "database": database,
        "database_ok": ok,
    }


@router.get("/ready")
def ready() -> JSONResponse:
    ok = check_connection()
    if ok:
        return JSONResponse(
            status_code=200,
            content={"status": "ready", "service": "courierledger-m1", "database_ok": True},
        )
    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "service": "courierledger-m1", "database_ok": False},
    )
