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
"""

from fastapi import APIRouter

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
