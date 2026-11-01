"""CourierLedger M1 backend — FastAPI application entrypoint.

Milestones 0 (foundation) + 1 (auth). Run with:
    uvicorn app.main:app --host 127.0.0.1 --port 8000
from m1/backend/ (with the venv active).

Notes:
- CORS allows only the Vite dev origin (config.frontend_url) with
  credentials, so the browser will send the HttpOnly session cookie.
- The global exception handler returns {"detail": ...} and never
  leaks tracebacks or internals to the client; full tracebacks go
  to the structured JSON log.
- In production mode the app refuses to start with the placeholder
  SECRET_KEY / SESSION_SECRET.
- Tables are NOT auto-created by default. Set
  CL_CREATE_TABLES_ON_STARTUP=true (env var create_tables_on_startup)
  only for local dev / the smoke test / pytest; production schema is
  owned by Alembic migrations (backend/alembic/, another agent).
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import auth, courier_accounts, health, imports, members, reconcile, reports, runs, settlements, shipments, statements, workspaces
from app.api import settings as settings_api
from app.config import settings
from app.db import models  # noqa: F401  (registers all 31/32 tables on shared Base)
from app.db.auth_models import Base
from app.db.session import engine
from app.logging_config import configure_logging, get_logger

configure_logging(settings.log_level)
logger = get_logger(__name__)

PLACEHOLDER = "CHANGE_ME_REPLACE_WITH_A_LONG_RANDOM_SECRET"

if settings.environment == "production" and (
    settings.secret_key == PLACEHOLDER or settings.session_secret == PLACEHOLDER
):
    raise RuntimeError(
        "Refusing to start in production with placeholder SECRET_KEY/SESSION_SECRET. "
        "Set SECRET_KEY and SESSION_SECRET environment variables."
    )

@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.create_tables_on_startup:
        # Dev/test convenience ONLY — see module docstring.
        logger.warning("create_tables_on_startup_enabled")
        Base.metadata.create_all(bind=engine)
    logger.info(
        "app_startup",
        extra={"environment": settings.environment, "database_url_scheme": settings.database_url.split("://", 1)[0]},
    )
    yield


app = FastAPI(title="CourierLedger M1", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "unhandled_exception",
        extra={"path": str(request.url.path), "method": request.method, "error": repr(exc)},
        exc_info=exc,
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


@app.get("/")
def root() -> dict:
    return {"service": "courierledger-m1", "status": "ok"}


app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(workspaces.router, prefix="/api")
app.include_router(members.router, prefix="/api")
app.include_router(courier_accounts.router, prefix="/api")
app.include_router(imports.router, prefix="/api")
app.include_router(runs.router, prefix="/api")
app.include_router(settlements.router, prefix="/api")
app.include_router(shipments.router, prefix="/api")
app.include_router(statements.router, prefix="/api")
app.include_router(reports.router, prefix="/api")
app.include_router(settings_api.router, prefix="/api")
# Reconcile router declares its full /api paths itself — no extra prefix.
app.include_router(reconcile.router)
