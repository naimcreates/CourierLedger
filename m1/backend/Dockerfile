# CourierLedger M1 -- backend API image (FastAPI + SQLAlchemy + psycopg).
# Local development only. Build from m1/: `docker compose build backend`
# (context is ./backend). The image does NOT run migrations and does NOT
# create the schema: provision the database first (see m1/README.md --
# alembic as the owner role, then rls.sql / triggers.sql / roles.sql),
# then run the container with DATABASE_URL for the RUNTIME role
# (courierledger_runtime, B-4). No secrets are baked into the image;
# SECRET_KEY / SESSION_SECRET / DATABASE_URL come from the environment.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# psycopg[binary] + argon2-cffi ship wheels; no build tools needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
COPY pytest.ini .
COPY tests ./tests

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Phase 3 (Stream C): readiness, not liveness. /api/health returns 200
# even when the database is down (the process is alive), so a
# healthcheck on /health would report a database-less container as
# healthy. /api/ready returns 200 only when the database round-trip
# succeeds, so Docker restarts/replaces the container until the
# database is actually reachable.
HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
