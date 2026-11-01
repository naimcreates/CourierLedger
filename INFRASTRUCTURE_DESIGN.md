# CourierLedger — Infrastructure Design

**Status:** Phase 2 (2026-11-01). Local development build; **NOT
production-ready**. This document records the infrastructure as it
exists, the decisions made in Phase 2, and the decisions that are
still open. No production host, provider, or paid service has been
selected.

---

## 1. Architecture (unchanged by Phase 2)

Modular monolith, no microservices. React + TypeScript + Vite
frontend; FastAPI backend; PostgreSQL 16; SQLAlchemy 2.x; Alembic;
PostgreSQL RLS multi-tenancy; append-only financial and audit
history; integer poisha money; UUID ids; TIMESTAMPTZ. One database,
one schema, tenant isolation by RLS, never by separate databases.

## 2. The environment, honestly

- **This VM:** a single Linux container/VM. PostgreSQL 16.15 via
  apt, cluster `16 main`, port 5432, UTF8/C.UTF-8, Etc/UTC.
  The VM has been **replaced at least five times**, wiping
  PostgreSQL each time; it is rebuilt from apt packages with the
  documented UTF8-cluster procedure (see
  `m1/LOCAL_VERIFICATION.md` §0). Nothing on this VM is durable
  infrastructure — it is a development machine.
- **Docker: CANNOT RUN HERE.** The `docker` binary is currently not
  even present (removed by a container replacement); before that it
  was 29.1.3 and containers could not start —
  `runc create failed: setns: operation not permitted` — so `docker
  compose up` has **never run** in this environment. This is blocker
  B-2, unchanged since M0. Phase 2 did not reinstall Docker and did
  not attempt to run it, because the blocker is the VM's runtime,
  not the installation.
- **GitHub Actions: billing-locked.** Zero tests have ever run there
  (every run fails in ~3 s with no runner, no steps, no logs). The
  canonical CI workflows exist and are YAML-valid, but they have
  never executed. Local verification is the only real CI signal.
- **No production host.** No Render, VPS, managed PostgreSQL, or
  any other host has been selected or provisioned.

## 3. Docker / Compose (Phase 2 review)

### 3.1 What was already good

- `m1/docker-compose.yml`: named volume `courierledger_pgdata`;
  `pg_isready` healthcheck on the DB; `depends_on:
  service_healthy`; fail-closed secrets (empty password fails
  startup); loopback-only DB port; no ARG/ENV secrets in either
  Dockerfile; `.dockerignore` excludes `.env`.
- `m1/backend/Dockerfile`: multi-stage, `uvicorn`, healthcheck via
  `/api/health`; `m1/frontend/Dockerfile`: multi-stage node →
  nginx.

### 3.2 What Phase 2 changed

| Change | Why |
|---|---|
| Backend port `8000:8000` → `127.0.0.1:8000:8000` | Loopback-only, matching the DB port — the app is never exposed on the host's network interfaces in dev |
| Frontend Dockerfile: `USER node` + `HEALTHCHECK` via Node 24's global `fetch()` | The container no longer runs the build as root, and the image now has a real health probe |
| Compose header: documents the Docker blocker and points to the native-PostgreSQL fallback | So a future reader does not mistake the compose file for a verified setup |

### 3.3 What Phase 2 verified

- Both Dockerfiles and `docker-compose.yml` parse and were checked
  by structural assertions only (Python YAML parse). No `docker
  compose config`, `build`, or `up` ran — **none can run here**.
- `.dockerignore` exists in both images' contexts and excludes
  `.env`.
- **No production compose file exists.** Dev/test/prod are not
  separated; that is an open decision for a real host.

### 3.4 Data durability

PostgreSQL data lives in the named volume `courierledger_pgdata`
— so a container replacement does not lose the database *in the
Docker model*. But: this VM has never run Docker, so this is an
untested claim here. The **native** path (apt PostgreSQL) has been
re-proven repeatedly and is the only supported local path today. **Do
not use a temporary filesystem as the production database.**

## 4. Configuration — what is applied vs recommended

Full table in `PRODUCTION_CONFIGURATION_GUIDE.md`. Summary:

| Item | Status |
|---|---|
| Per-role timeouts (runtime 30s/60s/5s; maintenance 10min/10min/30s) | **Applied live** + in `roles.sql` |
| Explicit SQLAlchemy pool (5/10/30/1800, pre-ping) | **Applied** in `app/db/session.py` |
| PostgreSQL production `postgresql.conf` (shared_buffers 2 GB, effective_cache_size 6 GB, work_mem 16 MB, maintenance_work_mem 512 MB, log_min_duration_statement 1000, pg_stat_statements) | **Recommendation only** — cannot be verified on a 128 MB shared_buffers dev cluster; enabling `pg_stat_statements` needs a restart + `CREATE EXTENSION` |
| Missing indexes (3 FK indexes) | **Applied** — Alembic 0006 |
| `purge_expired_auth_rows` | App-side Python function, **no scheduler**; mislabelled "procedure" in Phase 1.1 docs — corrected here |
| `alembic check` drift (17–18 ops) | **Unresolved** — representation drift only; decision recorded in `MIGRATION_VERIFICATION.md` (align models vs allow-list vs tolerate) |
| Migrator role | **Open** — `courierledger_migrator` exists with 0 grants; live migrations run as `courierledger` |
| NOLOGIN-role passwords | **Open** — verifiers set on both NOLOGIN roles; document or null |

## 5. Persistent storage (Phase 2)

- PostgreSQL data: named volume `courierledger_pgdata` in Compose;
  apt data directory in the native path.
- CSV import files: `m1/backend/var/imports/<merchant-uuid>/<sha256>.csv`.
  Never served over HTTP (no download endpoint, no public URLs).
  Phase 1 caps verified present: 10 MB abort, 50k rows, 100 cols,
  1 MB/field, formula-like values flagged (not executed).
- `file_objects.purged_at` exists but no purge job or retention
  policy uses it — **open**.
- **Object storage:** a `StorageBackend` protocol +
  `LocalFilesystemBackend` abstraction now exists
  (`app/services/storage.py`; `get_storage_backend()` is the single
  swap point; `csv_import.py` stores via it). Labelled honestly: the
  local filesystem is **not** production object storage. No provider
  (S3/R2/Spaces) is selected; no paid service without approval.
  Requirements for the real one: private-by-default objects,
  tenant-aware access checks, no public URLs, size limits, content
  validation, safe download authorization, retention/deletion
  lifecycle hooks, no secrets in metadata.

## 6. Observability (Phase 2)

**Implemented and tested:**

- **Request IDs:** middleware in `main.py` — UUID4 per request;
  a client-supplied `X-Request-ID` is echoed only if ≤128 chars and
  matching `[A-Za-z0-9._-]+`, otherwise replaced; the header is
  returned on every response; a `contextvar` is reset after each
  request. `X-Request-ID` is present on `/api/health` and
  `/api/ready` (verified live).
- **Structured logs:** JSON logging already existed; the
  `request_id` is now injected into **every** JSON log line by
  `JsonFormatter`, and one structured `request_completed` line is
  emitted per request.
- **Readiness vs liveness:** `/api/health` (liveness — always 200
  when the process is up) vs `/api/ready` (readiness — **200** with
  the DB up, **503** with the DB down). A load balancer must use
  `/ready`; Kubernetes-style probes must not confuse the two.
- **No secrets in logs:** grep-verified — reset-token logging
  records only `user_id`; no passwords, tokens, credentials, or
  merchant financial data in logs (see `INFRASTRUCTURE_TEST_REPORT.md`).

**Deliberately NOT built:** hosted error reporting (founder
decision), a metrics endpoint, distributed tracing. Those are
operations-runbook items, not code.

**Not yet enabled:** `pg_stat_statements` (needs restart +
`CREATE EXTENSION`); slow-query visibility is therefore
documentation-only. `log_min_duration_statement = 1000` is the
production recommendation.

## 7. Decisions still open (founder / production-readiness)

1. **Production host** — Render / VPS / managed PostgreSQL: not
   chosen. (A free Render account was offered to Naim on 2026-10-31
   for a phone-openable preview; he has not confirmed creating one.)
2. **Docker host for B-2** — a container-capable machine is needed
   before Docker can ever be verified.
3. **Backup schedule + off-host copy + WAL archiving/PITR** — the
   procedure is proven, the schedule and destination are not.
4. **Object-storage provider** — S3-compatible, when production
   storage is needed.
5. **Email provider** — password reset is log-only (never ship that
   to production).
6. **Migrator role** — Option 1 (put into service) vs Option 2
   (document `courierledger` as live migrator).
7. **`alembic check` drift** — align models vs allow-list
   RLS-layer objects vs tolerate.
8. **GitHub billing lock** — Naim must resolve it on his GitHub
   account before CI can ever run.
9. **Stale self-hosted CI runs / `ci-selfhosted.yml`** — queued runs
   interfere with concurrency groups; decision pending (cancel them
   or remove the workflow).
10. **Retention / purge policy** for imported files and
    `file_objects.purged_at`.
11. **Hosted error reporting** — founder decision.

## 8. What Phase 2 did not do (and why)

- No application rebuild, no new features, no V2.2.1 change.
- No microservices, no Redis, no external pooler.
- No production deployment, no paid infrastructure, no history
  rewrite, no force-push, no credentials committed.
- No claim of production readiness — the project remains **not
  production-ready** (OD-12/OD-16/OD-17 open, B-2 open, ADR 1.0
  missing, billing lock, log-only password reset, market-validation
  gates not run, no merchant-confirmed courier evidence).
