# CourierLedger — Production Configuration Guide

**Status:** Phase 3, Stream C (Production Configuration + Docker +
Observability), 2026-11-02. This is a **checklist for a production
host that does not exist yet**, not a record of a deployed system.
Nothing in this guide is deployed. The project is **NOT
production-ready** — see §16 for the full pre-launch checklist.

## Status vocabulary

Every item in this guide carries one of these labels. Do not read a
lower label as a higher one.

| Label | Meaning |
|---|---|
| **Implemented and verified** | Done in this repo and proven by a live run or a passing test in this session (evidence cited). |
| **Implemented but not verified** | Done in this repo, but no live run has proven it yet (usually: no Docker / no production host). |
| **Documented design** | A decision or procedure written down; nothing built or run. |
| **Blocked by environment** | Cannot be done or proven on this VM. |
| **Requires founder approval** | Only Naim can decide it (host, money, provider, legal). |

---

## 1. PostgreSQL server (16.x)

| Setting | Value | Status |
|---|---|---|
| Version | PostgreSQL 16.x (16.15 locally) | Implemented and verified (local cluster `16 main`, online on 5432) |
| Encoding | UTF8, locale C.UTF-8 | Implemented and verified (Phase 2; mandatory — a SQL_ASCII cluster breaks psycopg/Alembic) |
| Timezone | Server Etc/UTC; app stores TIMESTAMPTZ | Implemented and verified (Phase 2) |
| `max_connections` | 100 (local default) | Local default; size for the host — see §5 |
| `shared_buffers` | **2 GB** | Requires founder approval / Documented design (local: 128 MB — do not copy the local value to production; do not apply the 2 GB value to this VM either) |
| `effective_cache_size` | **6 GB** | Documented design (same caveat) |
| `work_mem` | **16 MB** | Documented design (local: 4 MB) |
| `maintenance_work_mem` | **512 MB** | Documented design |
| `statement_timeout` (server) | 0 (unlimited) locally | Per-role timeouts are applied instead — see §2 |
| `log_min_duration_statement` | **1000 ms** | Documented design — currently **-1 (disabled)**, verified live 2026-11-02. Needs a config change + reload (no restart). See §14. |
| `pg_stat_statements` | Install + `CREATE EXTENSION` | Blocked by environment — see §14 for the verdict and the exact steps. **Not installed** (verified live 2026-11-02: no extension row; `shared_preload_libraries` is empty). |
| `wal_level` | replica | Implemented and verified (live: `replica`) |
| `archive_mode` | **on** (with an archive command) | Documented design — currently **off** (verified live 2026-11-02); required for PITR (see `DATABASE_BACKUP_RECOVERY.md`) |
| SSL | on, real certificate on a real host | Documented design (locally on with a snakeoil cert — verified live) |
| `listen_addresses` | localhost locally | Implemented and verified locally (`localhost`); production: loopback or private network only, never public |

Databases on the local cluster (verified live 2026-11-02):
`courierledger_app` (11 MB) and `courierledger_dev` (9943 kB), **both
at Alembic 0006**. A scratch database `courierledger_live_test` is
created and dropped by the live test harness; it is not a working
database.

## 2. Per-role timeouts — Implemented and verified (re-verified live 2026-11-02)

`ALTER ROLE ... SET` — applied live and recorded in
`m1/backend/app/db/roles.sql`. No restart needed. Re-verified this
session by connecting as the runtime role and reading
`pg_roles.rolconfig` as superuser:

| Role | statement_timeout | idle_in_transaction_session_timeout | lock_timeout |
|---|---|---|---|
| `courierledger_runtime` | 30s | 60s | 5s |
| `courierledger_maintenance` | 10min | 10min | 30s |
| `courierledger` (owner) | (none) | (none) | (none) |
| `courierledger_migrator` | (none) | (none) | (none) |

If a legitimate operation ever exceeds 30s, raise the timeout for
that path only (`SET LOCAL` in that transaction), never globally.

## 3. Connection pooling — Implemented and verified (Phase 2; code re-read 2026-11-02, unchanged)

No external pooler. SQLAlchemy explicit settings in
`app/db/session.py`: `pool_size=5`, `max_overflow=10`,
`pool_timeout=30`, `pool_recycle=1800`, `pool_pre_ping=True` —
at most 15 connections per backend process. If a pooler is ever
added it **must be PgBouncer in transaction mode** (v0.1 §E.5);
session-mode pooling is forbidden. Tenant/user context is
transaction-local only; a pooled connection cannot carry a previous
tenant's context (proven live in Phase 2:
`test_pooled_connection_context_does_not_leak`).

## 4. Roles and privileges — Implemented and verified (Phase 2; not re-probed in this stream)

See `DATABASE_OPERATIONS.md` §3 for the full table and negative
probes. Essentials:

- Runtime role `courierledger_runtime`: no table ownership,
  `rolbypassrls = false`, 0 DELETE grants, column UPDATE only on
  `reconciliation_runs(status, completed_at)`.
- Migration privileges are **not** separated live: the migrator role
  exists with 0 grants; migrations run as `courierledger`. **Open
  decision:** put the migrator into service (REASSIGN OWNED) or
  document `courierledger` as the live migrator. (Another Phase 3
  stream is working on roles/migrations concurrently — re-check
  `DATABASE_OPERATIONS.md` before acting on this.)
- Maintenance role: SELECT all tables, DELETE only on
  `idempotency_keys`, narrow UPDATEs, no financial-history mutation.
- Default privileges: `pg_default_acl` is empty (fail-closed) — every
  new table needs an explicit grant in `roles.sql` in the same change.
- No application superuser credentials. No credentials in git (the
  only real secret found in the repo was the gitignored
  `m1/backend/.env`; the historical local-dev password in git
  history is invalid for both roles — see
  `PRODUCTION_SECRETS_GUIDE.md`).

## 5. Sizing guidance — Documented design

The local cluster (128 MB shared_buffers, 4 MB work_mem, 100
connections) is a development default, not a production target.
Size from the host's memory: `shared_buffers` ≈ 25% of RAM,
`effective_cache_size` ≈ 75% of RAM, `work_mem` from the expected
concurrent-sort count, `max_connections` from (backend processes ×
15 pool) + maintenance headroom. Re-verify with the actual workload
after the first production-like load test — no load test has been run.

## 6. Environment variables and production startup validation

### 6.1 Templates — Implemented and verified (templates exist; values are placeholders only)

- `m1/.env.example` — dev/compose template (development).
- `m1/backend/.env.example` — backend dev/test template.
- `m1/.env.production.example` — **new in Phase 3, Stream C**:
  production template for `docker-compose.production.yml`.
  Placeholders only (`CHANGE_ME_*`); the app and compose both refuse
  to start on them.

Rules:

- The app connects as the **runtime** role (`DATABASE_URL`); Alembic
  and admin SQL connect as the **owner** role
  (`MIGRATION_DATABASE_URL`). Never the reverse.
- `SECRET_KEY` / `SESSION_SECRET`: long random values
  (`secrets.token_urlsafe(48)`); never committed; never the local-dev
  values in production.
- `SESSION_COOKIE_SECURE=true` behind HTTPS; `ENVIRONMENT=production`
  disables API docs and enforces the secure-cookie guard.
- `CREATE_TABLES_ON_STARTUP=false` in production — schema is owned
  by Alembic.
- `FRONTEND_URL` is the single CORS origin.
- **Never** put a database password in an image layer, a compose
  file, or a workflow file. The compose files are fail-closed on an
  empty password. Passwords are injected at runtime from the secret
  store (local dev: the gitignored `.env`).

### 6.2 Production startup validation — Implemented and verified (Phase 3, Stream C)

`app/config.py: validate_production_settings()` runs at import time
in `app/main.py` whenever `ENVIRONMENT=production`, before the app
is built. It fails fast with a `RuntimeError` that names the
variable. The checks are **value-based**: a value may come from the
process environment or from a `.env` file (as `config.py` has always
allowed) — what matters is that it is not a development default or a
placeholder. Verified live 2026-11-02 (subprocess import probes) and
covered by `tests/test_production_config.py` (15 passed — 8 validator
unit tests, 5 subprocess import tests, 1 DB-down readiness test, 1
live production-mode test — see §17):

| Check | Failure message names |
|---|---|
| `DATABASE_URL` is a `postgresql://` URL with no `CHANGE_ME` placeholder password (an unset `DATABASE_URL` falls back to the compiled-in default, which contains `CHANGE_ME`, so "not set" and "placeholder" fail the same check) | `DATABASE_URL` |
| `SECRET_KEY` / `SESSION_SECRET` are not the placeholder | `SECRET_KEY` / `SESSION_SECRET` |
| `FRONTEND_URL` is not the development default (`http://127.0.0.1:5173`) | `FRONTEND_URL` |
| `SESSION_COOKIE_SECURE=true` | `SESSION_COOKIE_SECURE` |
| `CREATE_TABLES_ON_STARTUP=false` | `CREATE_TABLES_ON_STARTUP` |

What this closed: before this stream, starting with
`ENVIRONMENT=production` and no `DATABASE_URL` in the environment
**succeeded silently** (exit 0) on the compiled-in placeholder URL —
the app would then fail every database call at request time. That
probe now exits 1 naming `DATABASE_URL`. The two Phase 1 checks
(placeholder secrets, insecure cookie) are preserved inside the new
validator.

One deliberate consequence, recorded honestly: production now
**refuses a SQLite `DATABASE_URL` and the development-default
`FRONTEND_URL`**. The Phase 1 production-mode probe tests
(`tests/test_security_phase1.py`,
`test_production_mode_docs_404_and_hsts` /
`test_production_mode_refuses_insecure_cookie`) used a SQLite URL
and relied on the repo `.env` file's dev-default `FRONTEND_URL` to
probe docs/HSTS without a database; their fixture was changed in
this stream to a `postgresql://` URL pointing at a dead port
(127.0.0.1:5999) and an explicit `https://app.example.invalid`
origin — the probes touch no database and their assertions are
unchanged, so the fixture is now *more* production-realistic, not
less. No test was removed, skipped or weakened.

## 7. Secure cookies and CSRF — Implemented and verified (Phase 3, Stream C re-verified live)

Code (`app/api/auth.py`): the session cookie is `HttpOnly`,
`SameSite=lax`, `Secure` exactly when `settings.session_cookie_secure`
is true; the CSRF cookie is the same except deliberately **not**
`HttpOnly` (frontend JS must read it to send the `X-CSRF-Token`
header). Both cookies take their `Secure` flag from the same setting
the production validator enforces.

Verified live 2026-11-02 against a real production-mode server on
the local cluster (runtime role, `courierledger_app`):

- `GET /api/auth/csrf` returned `Set-Cookie: cl_csrf=...; Max-Age=604800; Path=/; SameSite=lax; Secure` — `Secure` present, `HttpOnly` absent (correct for the CSRF cookie).
- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
  present on responses in production mode (HSTS only in production —
  correct, local dev/test serve plain HTTP).
- Starting in production mode with `SESSION_COOKIE_SECURE=false`
  exits 1 naming the variable (§6.2).

## 8. Proxy / HTTPS assumptions — Documented design (code facts verified 2026-11-02)

The app, as written, assumes exactly this deployment shape. None of
it is optional; the production compose file (§13) encodes it.

- **A TLS-terminating reverse proxy sits in front of the backend.**
  The app itself never terminates TLS (uvicorn serves plain HTTP on
  127.0.0.1:8000 inside the container). `SESSION_COOKIE_SECURE=true`
  and HSTS assume the browser reaches the app over HTTPS through
  that proxy. **Requires founder approval:** no proxy product and no
  host has been chosen, so no proxy exists yet.
- **The proxy is the only component that may set forwarded headers,
  and uvicorn trusts them only from the proxy's address.**
  The code has no `TrustedHostMiddleware` and reads no
  `X-Forwarded-*` header itself. The dev compose file and both
  Dockerfiles run uvicorn **without** `--proxy-headers`, so behind a
  proxy the app would see the proxy as the client. The production
  compose file runs uvicorn with `--proxy-headers
  --forwarded-allow-ips=${TRUSTED_PROXY_IPS:?}` — the variable is
  required and must be the proxy's address(es) on the compose
  network, never `*`.
- **Rate limiting keys on the direct peer** (`request.client.host`;
  see `app/services/rate_limit.py`). Behind a proxy without
  `--proxy-headers`, every client shares the proxy's IP in the
  per-IP buckets (register / reset-request / invite / login). The
  per-account login bucket (email only) still works. With
  `--proxy-headers` + a correct `--forwarded-allow-ips`, uvicorn
  reports the real client IP.
- **The rate limiter is process-local memory.** It does not survive
  a restart and does not share state across workers. Acceptable for
  single-process dev; a multi-worker deployment must replace it with
  a shared store (e.g. Redis) before production — documented in
  `rate_limit.py`, **Requires founder approval** (Redis is a Phase 2
  follow-up item, not built).
- **No forwarded-proto handling is needed beyond the above:** the app
  builds no absolute URLs in responses and the OpenAPI docs are
  disabled in production, so nothing else depends on the request
  scheme. CORS allows exactly one origin (`FRONTEND_URL`).
- **The frontend is same-origin or CORS, not proxied by the app.**
  The Vite dev proxy (`/api` → 127.0.0.1:8000) is a dev-server
  feature; the containerised frontend (`vite preview`) does not
  proxy API calls, so the production frontend must be served by a
  static host or by the reverse proxy — **Requires founder approval**.

## 9. Production API docs disabled — Implemented and verified (live, 2026-11-02)

`app/main.py` sets `docs_url=None, redoc_url=None, openapi_url=None`
when `ENVIRONMENT=production` (Phase 1, C-5.4). Verified live
against a real production-mode server:

- `GET /docs` → 404, `GET /redoc` → 404, `GET /openapi.json` → 404
  (both with the database up and with the database down).
- In development/test mode the docs remain available (unchanged).

## 10. Readiness / liveness — Implemented and verified (live, 2026-11-02)

- `GET /api/health` (liveness): always 200 while the process is up.
  Body reports `"database": "postgresql" | "sqlite-test-only" |
  "unavailable"` and `"database_ok": bool` from a real `SELECT 1`
  round-trip — never from configuration alone.
- `GET /api/ready` (readiness): 200 only when the database round-trip
  succeeds, 503 otherwise. Use `/ready` for load-balancer and
  container health checks.

Verified live this session (real uvicorn, production mode):

| Condition | `/api/health` | `/api/ready` |
|---|---|---|
| Database up (runtime role, `courierledger_app`) | 200, `database: postgresql`, `database_ok: true` | 200, `status: ready` |
| Database down (DATABASE_URL pointed at a dead port, 127.0.0.1:5999 — the cluster was **not** stopped) | 200, `database: unavailable`, `database_ok: false` | **503**, `status: not_ready` |

How to test `/ready` with the database truly down (documented, not
run here — stopping the cluster would interrupt other work on this
shared VM): stop the cluster (`pg_ctlcluster 16 main stop` as
postgres/root), `curl` `/api/ready` (expect 503) and `/api/health`
(expect 200, `database_ok: false`), then start the cluster again.
The dead-port test above proves the same code path without the
outage, so the stop-the-cluster test is a confirmation, not a
discovery step.

## 11. Graceful shutdown — Implemented and verified (live, 2026-11-02)

Sending SIGTERM to a running uvicorn process produces the full
graceful sequence in the structured log, in ~100 ms:

```
Shutting down
Waiting for application shutdown.
Application shutdown complete.
Finished server process [pid]
```

One uvicorn behaviour to know about when reading exit codes:
uvicorn 0.54's `capture_signals` re-raises the captured signal after
the graceful shutdown, so the process exit status is **-15 (killed
by SIGTERM)**, not 0. That is uvicorn's intended behaviour, not a
crash — the lifespan shutdown ran to completion first. Container
orchestrators report this as exit 143 (128+15), the conventional
clean SIGTERM exit. The test
(`tests/test_production_config.py::test_production_server_db_down_ready_503_health_200_docs_404`)
asserts the log sequence and termination, and accepts exit code 0
or -15.

There is no in-flight-request drain configuration beyond uvicorn's
default: on SIGTERM uvicorn stops accepting new connections and
waits for the lifespan shutdown; long-running requests are not given
a configured grace period. If the production host needs one, set it
on the orchestrator (e.g. a stop timeout longer than the 30s
statement timeout) — Documented design, not configured anywhere yet.

## 12. Migrations separated from application startup — Implemented and verified (live, 2026-11-02)

- The app contains **no Alembic call**: `grep` over `app/main.py`
  and `app/db/session.py` finds no `alembic` import or invocation,
  and the lifespan handler only runs `create_all` when
  `CREATE_TABLES_ON_STARTUP=true` — which production validation
  forbids (§6.2).
- Verified live: a production-mode server was started against
  `courierledger_app` and stopped; `alembic_version` was `0006`
  before and `0006` after.
- Schema changes run only through the one-shot `migrate` service in
  `docker-compose.production.yml` (`alembic upgrade head`, owner
  role, explicit `run --rm migrate`) — see §13. The test suite
  asserts the revision is unchanged across a production-mode app
  start (`tests/test_production_config.py`, live test).

## 13. Docker and deployment verification

### 13.1 Status — Blocked by environment (Docker absent)

- `which docker` → not found; `docker version` → `command not
  found`. The binary was removed by a container replacement; before
  that, 29.1.3 with containers unable to start (`runc create failed:
  setns: operation not permitted`). **`docker compose up` has never
  run on this VM** — for the dev file or the new production file.
- Nothing in this section is claimed from YAML parsing alone. The
  production compose file was checked only for YAML validity
  (`yaml.safe_load` parses; services `backend`/`db`/`migrate`,
  volume `courierledger_pgdata_prod`, no published DB port).

### 13.2 What exists — Implemented but not verified

- `m1/docker-compose.yml` (dev): Postgres 16 + backend, loopback-only
  ports, fail-closed on unset secrets. Frontend service stays
  commented out (containerised frontend cannot proxy `/api`).
- `m1/docker-compose.production.yml` (**new in Phase 3, Stream C**,
  standalone — run with `-f` alone, not as an override):
  `db` (no published port, named volume
  `courierledger_pgdata_prod`), one-shot `migrate` service
  (`alembic upgrade head`, owner role, `profiles: ["migrate"]`),
  `backend` (`ENVIRONMENT=production`, loopback-only 8000,
  `--proxy-headers --forwarded-allow-ips=${TRUSTED_PROXY_IPS:?}`,
  healthcheck on `/api/ready`). No frontend service — see §8.
- `m1/backend/Dockerfile`: non-root `appuser`, no secrets baked in,
  no migrations on start. **Changed in this stream:** the
  HEALTHCHECK now probes `/api/ready` instead of `/api/health`,
  because `/health` returns 200 even with the database down and
  would report a database-less container as healthy.
- `m1/frontend/Dockerfile`: builds the production bundle, serves
  with `vite preview` on 5173, runs as `node`. Unchanged — the
  preview proxy gap is documented, not fixed, in this stream.

### 13.3 Exact commands for a container-capable host — Documented design (not run)

Run these on the future production host (or any container-capable
machine with the repo checked out), in order. Stop at the first
failure and record it; a green run closes B-2.

```bash
# 0. Prerequisites on the host
docker version                # must print a server version
docker compose version        # must print a version
cp m1/.env.production.example m1/.env.production   # fill from the secret store; never commit

# 1. Build (no secrets are baked into the image — verify by inspection)
docker compose -f m1/docker-compose.production.yml --env-file m1/.env.production build

# 2. Provision the database ONCE (roles + RLS + triggers, then migrations).
#    The migrate service only runs Alembic; it does NOT create roles or
#    apply rls.sql / triggers.sql / roles.sql. Follow the provisioning
#    order in m1/README.md, adapted to the host (the local
#    m1/scripts/provision_db.sh is written for this VM's native cluster).
docker compose -f m1/docker-compose.production.yml --env-file m1/.env.production up -d db
#    ... provisioning steps (roles, rls.sql, triggers.sql) ...

# 3. Migrate (owner role, one-shot; must exit 0 and print the new head)
docker compose -f m1/docker-compose.production.yml --env-file m1/.env.production run --rm migrate
docker compose -f m1/docker-compose.production.yml --env-file m1/.env.production exec db \
  psql -U courierledger -d courierledger -At -c "SELECT version_num FROM alembic_version;"

# 4. Start the backend and check readiness
docker compose -f m1/docker-compose.production.yml --env-file m1/.env.production up -d
curl -s -o /dev/null -w "ready: %{http_code}\n" http://127.0.0.1:8000/api/ready     # expect 200
curl -s http://127.0.0.1:8000/api/health                                             # expect database postgresql
curl -s -o /dev/null -w "docs: %{http_code}\n" http://127.0.0.1:8000/docs            # expect 404
docker compose -f m1/docker-compose.production.yml ps                                # backend (healthy)

# 5. Persistence across restarts (data must survive)
docker compose -f m1/docker-compose.production.yml --env-file m1/.env.production exec db \
  psql -U courierledger -d courierledger -At -c "SELECT count(*) FROM alembic_version;"
docker compose -f m1/docker-compose.production.yml --env-file m1/.env.production restart db backend
curl -s -o /dev/null -w "ready after restart: %{http_code}\n" http://127.0.0.1:8000/api/ready  # expect 200
docker compose -f m1/docker-compose.production.yml --env-file m1/.env.production down
docker compose -f m1/docker-compose.production.yml --env-file m1/.env.production up -d
curl -s -o /dev/null -w "ready after down/up: %{http_code}\n" http://127.0.0.1:8000/api/ready   # expect 200

# 6. Role separation inside the containers (the app must NOT connect as owner)
docker compose -f m1/docker-compose.production.yml --env-file m1/.env.production exec db \
  psql -U courierledger -d courierledger -At -c \
  "SELECT rolname, rolconfig FROM pg_roles WHERE rolname LIKE 'courierledger%' ORDER BY rolname;"
#    expect: runtime 30s/60s/5s; maintenance 10min/10min/30s
docker compose -f m1/docker-compose.production.yml --env-file m1/.env.production exec backend \
  python -c "import os; print('DATABASE_URL user:', os.environ['DATABASE_URL'].split('://')[1].split(':')[0])"
#    expect: courierledger_runtime

# 7. Test suites that must pass on the host (from the repo root)
#    Backend live suite needs CL_LIVE_PG_PASSWORD + CL_LIVE_PG_ROLE=courierledger_runtime
#    (see tests/live_pg.py); the production-config tests are in
#    tests/test_production_config.py.
```

## 14. Observability — Implemented and verified where noted

- Request IDs + structured JSON logs with `request_id` on every line
  — **Implemented and verified** (Phase 2; re-checked live
  2026-11-02: `X-Request-ID` present on `/api/ready` responses,
  client-supplied ids echoed only when they match
  `[A-Za-z0-9._-]{1,128}` — anything else is replaced with a fresh
  UUID4, so an arbitrary header value never reaches the logs
  unfiltered. `request_completed` is logged per request with method,
  path and status code).
- `/api/health` (liveness) vs `/api/ready` (readiness, 200/503) —
  **Implemented and verified** (Phase 2; re-verified live, §10).
- **Logging audit (Phase 3, Stream C, 2026-11-02)** — all 36
  `logger.*` calls in `app/` were read. Result: **no passwords,
  tokens, session identifiers, customer information, or financial
  amounts are logged anywhere.** Log extras carry only ids, counts,
  statuses and row counts (`batch_id`, `merchant_id`, `run_id`,
  `results`, `excluded`, `rows`/`parsed`/`error_rows`). The raw
  password-reset token is never logged (Phase 1, B-F2 — only
  `password_reset_token_issued` with `user_id`). The audit trail
  (`app/services/audit_service.py`) records only ids, old/new role
  or status, and counts. One item to know about: `login_rate_limited`
  logs the **normalised email address** — a personal identifier, but
  the account identifier of a security event (a rate-limited login),
  not a secret. It is the only place a user identifier reaches the
  logs. No `print()` calls exist in `app/`. The formatter
  (`app/logging_config.py`) has no allow-list on extra fields — it
  formats whatever callers pass — so the rule stays a code-review
  rule: never pass request bodies, passwords, tokens or financial
  content as extras.
- Slow-query visibility — **Blocked by environment / Documented design:**
  - `log_min_duration_statement` is **-1 (disabled)** on the local
    cluster (verified live). Recommended production value: **1000
    ms**. Needs a config change + reload only (`SELECT
    pg_reload_conf()` or `pg_ctlcluster reload`) — no restart.
  - `pg_stat_statements`: **NOT installed** (verified live: no
    extension row, `shared_preload_libraries` is empty). The library
    file **is present** on disk
    (`/usr/lib/postgresql/16/lib/pg_stat_statements.so`), and
    `postgresql.conf` is writable by the `postgres` user, so the
    install itself is mechanical. It was **deliberately not enabled
    here**: enabling needs a cluster restart, and restarting the
    shared local cluster would drop every connection and interrupt
    the other Phase 3 streams' live work on this VM. Exact steps for
    a maintenance window (or for the production host's first
    provisioning):
    1. As the `postgres` OS user, add to `postgresql.conf`:
       `shared_preload_libraries = 'pg_stat_statements'` (append if
       the line already lists other libraries) and
       `log_min_duration_statement = 1000`.
    2. Restart the cluster: `pg_ctlcluster 16 main restart` (local)
       or the host's equivalent. Expect a brief outage — every
       connection drops; the app's pool re-connects on the next
       request (`pool_pre_ping=True`).
    3. As a superuser on each database that needs it:
       `CREATE EXTENSION IF NOT EXISTS pg_stat_statements;`
    4. Verify: `SELECT count(*) FROM pg_stat_statements;` returns
       rows after some traffic, and `SHOW shared_preload_libraries;`
       contains `pg_stat_statements`.
    5. Do **not** grant general read access to `pg_stat_statements`
       — it contains query text, which can include literals. Only
       the maintenance/operator role reads it.
- Hosted error reporting — **not built** (founder decision).
- Metrics endpoint / distributed tracing — **not built**.
- Backup failure alerts — **Documented design** (no alerting exists
  yet; the backup/maintenance scripts exit non-zero on failure so a
  scheduler can see it — see `INCIDENT_RESPONSE_RUNBOOK.md`).

## 15. Backups — Implemented but not verified as a schedule (procedure proven; other streams own the scripts)

See `DATABASE_BACKUP_RECOVERY.md`. The procedure (`backup_db.sh` /
`verify_restore.sh`, plus the Phase 3 encrypted backup
`backup_encrypted.sh`) was proven once on this VM. What does not
exist yet: a schedule, an off-host copy, WAL archiving, a globals
backup, a recurring drill, encryption-at-rest verification,
failure alerts. **Proposed targets (not verified):** RPO ≤ 1 h,
RTO ≤ 2 h, 30 daily + 12 monthly dumps. Do not load real merchant
data until a backup schedule and an off-host copy exist.

Script behaviour this stream relies on (verified by reading, not by
running — do not edit another stream's files):

- `m1/scripts/backup_db.sh <db> [out-dir] [env-file]` — `set -euo
  pipefail`; writes `<db>-<stamp>.dump` + `.sha256`; non-zero exit
  on any failure. Runs `pg_dump -Fc` as the postgres superuser
  (required — an owner-role dump fails under FORCE RLS on
  `audit_logs`).
- `m1/scripts/backup_encrypted.sh` (Phase 3, Stream A) — encrypted
  variant (`gpg` symmetric AES-256, passphrase from
  `CL_BACKUP_PASSPHRASE_FILE` or `CL_BACKUP_PASSPHRASE`, never a CLI
  argument); also dumps globals. Refuses to run with no passphrase.
- `m1/scripts/verify_restore.sh <src-db> <dump> [restore-db]` —
  restores into a **separate** fresh database, compares object
  counts, per-table row counts, financial identities and the
  runtime-role RLS probe; any mismatch exits non-zero and the
  restore database is **left in place** for inspection.
- `m1/scripts/maintenance.py [--db <name>] [--dry-run]` (Phase 3,
  Stream A) — the only deletion is expired sessions + expired/used
  reset tokens (B-F19); prints exactly one JSON line per run
  (`status: completed | skipped | dry_run`); exit 0 on completed /
  skipped (a held lock is not a failure), non-zero with a JSON error
  line on any error. There is **no scheduler** for it yet — running
  it on a schedule is a founder/operator decision.

## 16. Pre-launch checklist (all must be true before production)

- [ ] Production host provisioned; Docker verified on a
  container-capable machine (B-2 closed) or native PostgreSQL
  documented as the production path — §13.3 commands run green
- [ ] PostgreSQL 16, UTF8/C.UTF-8, `postgresql.conf` per §1,
  `pg_stat_statements` installed, `log_min_duration_statement = 1000`
- [ ] Migrator role decision made and applied (§4)
- [ ] NOLOGIN-role passwords documented or nulled
- [ ] Backup schedule + off-host copy + WAL archiving running;
  restore drill passed and recorded
- [ ] Object-storage provider chosen (or local storage explicitly
  accepted as a temporary measure with its limits recorded)
- [ ] Email provider chosen; password reset no longer log-only
- [ ] OD-12 / OD-16 / OD-17 closed (legal/policy review)
- [ ] ADR 1.0 written
- [ ] GitHub billing lock resolved; CI green on a real run
- [ ] Market-validation gates passed (Day 7 + Day 14)
- [ ] Merchant-confirmed courier evidence (rate cards, redacted
  statement headers) for at least the first courier
- [ ] `alembic check` drift decision made (see
  `MIGRATION_VERIFICATION.md` §3) and applied
- [ ] Reverse proxy chosen and `TRUSTED_PROXY_IPS` set from the real
  proxy address (see §8) — **Requires founder approval**
- [ ] Frontend hosting chosen (static host or reverse proxy) — **Requires founder approval**
- [ ] Rate limiting moved to a shared store if more than one backend
  worker/process will run (see §8) — **Requires founder approval**
- [ ] Backup/maintenance schedules actually running with failure
  alerting (see §15 and `INCIDENT_RESPONSE_RUNBOOK.md`)

**None of these is true today. The project is NOT production-ready.**

## 17. What Phase 3, Stream C changed (2026-11-02)

| File | Change | Status |
|---|---|---|
| `m1/backend/app/config.py` | `validate_production_settings()` — fail-fast production validation naming the variable | Implemented and verified (tests + live probes) |
| `m1/backend/app/main.py` | Calls the validator when `ENVIRONMENT=production`; Phase 1 checks preserved inside it | Implemented and verified |
| `m1/backend/tests/test_production_config.py` | New: validator unit tests, subprocess import tests, DB-down readiness test, live production-mode test | Implemented and verified — 15 passed, 0 failed |
| `m1/backend/tests/test_security_phase1.py` | Fixture only: production-probe helper now uses a dead-port `postgresql://` URL + explicit non-default `FRONTEND_URL` (production validation refuses SQLite and the dev-default origin) — assertions unchanged, no test removed/skipped/weakened | Implemented and verified — both production-mode tests pass |
| `m1/backend/Dockerfile` | HEALTHCHECK now probes `/api/ready` (was `/api/health`) | Implemented but not verified (no Docker) |
| `m1/docker-compose.production.yml` | New standalone production compose (db + one-shot migrate + backend) | Implemented but not verified (no Docker) |
| `m1/.env.production.example` | New production env template, placeholders only | Implemented and verified (template only) |
| `m1/README.md` | One line: Dockerfile healthcheck now `/api/ready` | Implemented and verified |
| `PRODUCTION_CONFIGURATION_GUIDE.md` | This file — full rewrite for Phase 3 | — |
| `INCIDENT_RESPONSE_RUNBOOK.md` | New — six incident procedures | — |

Nothing else was touched by this stream. V2.2.1 financial logic,
RLS, append-only controls and historical Alembic migrations were not
modified; `prototype-p0/` was not touched; nothing was committed or
pushed; nothing was deployed.
