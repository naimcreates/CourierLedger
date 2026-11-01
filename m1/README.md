# CourierLedger M1 — Local Development Application

**Status: Milestones 0–10 built, M11 git groundwork done, M12 CI-independent development done (2026-11-01). M10 (production-readiness hardening and M1 close-out) is done and verified live against real PostgreSQL 16.15: the Playwright settle steps now drive the real batch-detail UI forms, owner transfer is implemented with the last-owner guard (OD-11 closed), the password-reset decision is recorded (log-only for local dev). The project is pushed to GitHub (`naimcreates/CourierLedger`, branch `main`); canonical GitHub Actions is blocked by an account-level billing restriction (run 41286668638, attempts 1–2: every job "not started because your account is locked due to a billing issue", zero tests run) and a self-hosted runner attempt is parked (see §5.14). M12 verified the same three suites locally instead — backend 316 passed with live PostgreSQL, frontend build + Vitest 50 passed, Playwright 3 passed (see §4 and `m1/LOCAL_VERIFICATION.md`). Blockers B-1 (workspace RLS), B-4 (runtime-role connection) and B-3 (lease clock) are fixed and verified live. B-2 (Docker) is done as far as this VM allows — Dockerfiles written and linted, compose config valid — but image builds and `docker compose up` are unverified because containers cannot start in this VM (see §5). Milestone 4 (CSV import) and Milestone 5 (dashboard) are built and verified live. Verdict: M1 is NOT production-ready — see §7 and `IMPLEMENTATION_CHECKLIST.md` ("M10 — production-readiness review").**

Local development only. Synthetic/demo data only, clearly labelled. No production deployment, no real merchant data, no invented courier rates, CSV templates, or fee semantics.

## 0. Source-of-truth precedence (per kickoff brief)

1. **V2.2.1 + P0 patch** controls finance (see `~/MEMORY.md` and `prototype-p0/`).
2. **M1 database design v0.1 + amendments v0.2–v0.5 (CL-01–CL-26)** controls the database model.
3. **The kickoff brief's stack** controls architecture/technology choices.

**Gap — no "M1 Architecture Decision Record 1.0" exists.** No file by that name (or any ADR 1.0 content) was found anywhere in the workspace when this build started (searched 2026-10-31). The stack in this README and in `IMPLEMENTATION_CHECKLIST.md` is therefore taken from the kickoff brief only. No ADR content has been invented. If an ADR 1.0 is later supplied, this README must be re-checked against it.

**P0 prototype is read-only.** `../prototype-p0/` was not modified. Its engine was ported (with attribution, see `backend/app/domain/README.md`) into `backend/app/domain/engine.py`; its SQLite store and static UI were **not** copied as the M1 data model.

## 1. Architecture (modular monolith, no microservices)

```
m1/
  README.md                    this file
  IMPLEMENTATION_CHECKLIST.md  milestone status, verification log, blockers, next task
  docker-compose.yml           PostgreSQL 16 service + backend service (frontend commented — see §5)
  .env.example                 placeholders only, no secrets (DATABASE_URL = runtime role;
                               MIGRATION_DATABASE_URL = owner role, for Alembic)
  backend/Dockerfile           python:3.12-slim, non-root appuser, HEALTHCHECK /api/health
  frontend/Dockerfile          node:24-alpine, npm ci + build, vite preview on 5173
  frontend/                    React + TypeScript + Vite + Tailwind v3 + React Router
                               + TanStack Query + React Hook Form + Zod
                               (shadcn-style hand-written ui/ components)
  backend/
    requirements.txt           pinned: FastAPI 0.162.0, SQLAlchemy 2.1.6,
                               psycopg[binary] 3.3.6, pydantic 2.14.2,
                               argon2-cffi 25.1.0, alembic 1.20.1, pytest 9.2.0
    app/
      main.py                  FastAPI app; mounts health/auth/workspaces/
                               courier-accounts/imports/reconcile
      config.py                pydantic-settings, env-based, no hardcoded secrets;
                               default DATABASE_URL is the runtime role (B-4)
      logging_config.py        structured JSON logging
      security.py              Argon2id, opaque session tokens (SHA-256 stored)
      api/                     health.py, auth.py, workspaces.py, deps.py,
                               courier_accounts.py, imports.py,
                               reconcile.py (thin adapter over the domain engine)
      services/                auth_service.py, workspace_service.py, rate_limit.py,
                               idempotency_service.py (B-3: clock_timestamp() leases),
                               csv_import.py (M4: mapping, parsing, matching)
      db/
        session.py             engine, get_db, set_tenant_context (CL-06/CL-11),
                               set_user_context (B-1: app.user_id)
        auth_models.py         users, merchants, merchant_memberships,
                               user_sessions, password_reset_tokens
        models.py              remaining 27 tables (32 tables total — see §5)
        rls.sql                RLS policies (membership-based merchants SELECT,
                               NO FORCE on merchants + merchant_memberships,
                               SECURITY DEFINER create_workspace(), B-1)
        triggers.sql           append-only + CL-01/CL-13/CL-16 deferred triggers
        roles.sql              migrator / runtime / maintenance roles + grants
        seed_couriers.sql      the 5 courier codes only (M4; no rates/templates)
    alembic/                   env.py (prefers MIGRATION_DATABASE_URL) +
                               versions/0001_initial_m1.py (all 32 tables)
    tests/                     pytest suite (146 tests incl. live-PostgreSQL modules)
    var/imports/               local-dev stand-in for object storage (gitignored)
```

## 2. Environment verified in this session (2026-10-31)

- Python 3.12.3, Node v24.22.1, npm 10.9.4, git 2.43.0.
- **PostgreSQL 16.15 installed via apt** (`apt-get install -y postgresql postgresql-contrib`) and running: cluster `16 main`, port 5432, online (`pg_ctlcluster 16 main start`; systemd start is denied by `policy-rc.d` in this VM, so the cluster must be started with `pg_ctlcluster`). `psql (PostgreSQL) 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)`.
- **Warning — environment instability observed:** midway through this session the container was replaced and the PostgreSQL install vanished (no `postgres` user, no cluster, port 5432 closed). PostgreSQL was reinstalled from scratch and **every live-PostgreSQL verification below was re-run in the current container** — the numbers in §4 are from the second, current install, not carried over. If the container is replaced again, reinstall PostgreSQL and re-run §4 before treating the database as verified. `~/.postgres-ready` was never treated as evidence of anything.
- **Docker installed 2026-10-31:** Docker 29.1.3 (`apt-get install -y docker.io`) + Compose v2.39.0 (downloaded from GitHub releases; `docker-compose-plugin` is not in Ubuntu's apt repos). The daemon runs only with `--iptables=false --bridge=none --storage-driver=vfs` (the VM lacks NET_ADMIN; overlayfs fails). Base images `python:3.12-slim`, `postgres:16`, `node:24-alpine` are loaded (via `skopeo` + `docker load`, because the daemon's own pulls fail with TLS handshake timeouts through the egress proxy while shell curl reaches the registry). **Containers cannot start in this VM** (`runc create failed: ... setns: operation not permitted`), so `docker build` fails at the first RUN step and `docker compose up` has never run — see §5. Both Dockerfiles pass hadolint with no findings; build contexts are 1.3 MB (backend) and 269 kB (frontend) after `.dockerignore`; `docker compose config` exits 0.

## 3. Setup — exact commands run (2026-10-31)

PostgreSQL (apt route; use `docker compose up -d db` instead if Docker is available):

```bash
apt-get update
apt-get install -y postgresql postgresql-contrib
pg_ctlcluster 16 main start
su postgres -c "psql -c \"CREATE ROLE courierledger LOGIN PASSWORD '<dev-password>';\" \
  -c 'CREATE DATABASE courierledger_dev OWNER courierledger;' \
  -c 'CREATE DATABASE courierledger_app OWNER courierledger;'"
# NOTE (Phase 1, 2026-11-01): the local dev DB password is a fresh
# random value, generated with `python3 -c "import secrets;
# print(secrets.token_urlsafe(32))"` and stored only in gitignored
# `backend/.env` (DATABASE_URL) and the `CI_POSTGRES_PASSWORD` repo
# secret — never write the value into any doc. Older revisions of
# this file named a documented placeholder password; that value was
# retired in Phase 1 and no longer authenticates either role.
```

**Encoding trap (found 2026-11-01, M10):** if the container was replaced, the recreated cluster may default to SQL_ASCII encoding — psycopg then returns `bytes` instead of `str` for text and Alembic fails with `TypeError: cannot use a string pattern on a bytes-like object` before creating any table. Create the cluster explicitly as UTF8: `pg_dropcluster 16 main` (if a bad cluster exists), then `pg_createcluster 16 main --start -- --encoding=UTF8 --locale=C.utf8`, and verify with `psql -tAc "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname='postgres';"` → `UTF8`. (The `postgres` OS user may also need creating first if the package postinst did not — `groupadd -r postgres && useradd -r -g postgres -d /var/lib/postgresql -s /bin/bash postgres` — and missing deps such as `libpq5`, `libllvm17t64`, `ssl-cert` can be installed from the cached `.deb`s in `/var/cache/apt/archives/`.)

Backend:

```bash
cd m1/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# migrate + apply SQL (Alembic as the DB owner via MIGRATION_DATABASE_URL;
# roles.sql needs a superuser because it creates roles — copy it somewhere
# the postgres user can read first, e.g. /tmp/roles.sql, because the
# workspace files are root-owned 640):
DATABASE_URL='postgresql+psycopg://courierledger_runtime:<password>@127.0.0.1:5432/courierledger_app' \
MIGRATION_DATABASE_URL='postgresql+psycopg://courierledger:<password>@127.0.0.1:5432/courierledger_app' \
  .venv/bin/alembic upgrade head
PGPASSWORD='<password>' psql -h 127.0.0.1 -U courierledger -d courierledger_app \
  -v ON_ERROR_STOP=1 -f app/db/rls.sql
PGPASSWORD='<password>' psql -h 127.0.0.1 -U courierledger -d courierledger_app \
  -v ON_ERROR_STOP=1 -f app/db/triggers.sql
su postgres -c "psql -d courierledger_app -v ON_ERROR_STOP=1 -f /tmp/roles.sql"
# seed the 5 courier codes (idempotent; no rates or templates):
PGPASSWORD='<password>' psql -h 127.0.0.1 -U courierledger -d courierledger_app \
  -v ON_ERROR_STOP=1 -f app/db/seed_couriers.sql
.venv/bin/python -m pytest tests/ -q
# live-PostgreSQL modules (skipped without the password):
CL_LIVE_PG_PASSWORD='<password>' CL_LIVE_PG_ROLE=courierledger_runtime \
  .venv/bin/python -m pytest tests/ -q
```

Frontend:

```bash
cd m1/frontend
npm install
npm run build
npm test
npm run dev -- --port 5173 --strictPort   # Vite proxies /api -> 127.0.0.1:8000
```

Run the backend (from `m1/backend`, with a real PostgreSQL `DATABASE_URL` — the runtime role, B-4):

```bash
DATABASE_URL='postgresql+psycopg://courierledger_runtime:<password>@127.0.0.1:5432/courierledger_app' \
ENVIRONMENT=development CREATE_TABLES_ON_STARTUP=false \
SECRET_KEY='<long-random>' SESSION_SECRET='<long-random>' \
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# then open http://localhost:5173/ (Vite) — Dashboard shows live /api/health.
# NOTE: CREATE_TABLES_ON_STARTUP=true fails under the runtime role (no
# CREATE privilege) by design — schema changes go through Alembic as the
# owner role (MIGRATION_DATABASE_URL).
```

Stop both servers when done (`fuser -k 8000/tcp 5173/tcp`). Do not leave them running.

## 4. Verification — real results only (2026-10-31, plus the 2026-11-01 container-restart re-verification in the M12 row below)

| Check | Result |
|---|---|
| Backend pytest (`m1/backend`, full suite incl. live modules, `CL_LIVE_PG_ROLE=courierledger_runtime`) | **316 passed, 0 failed** (172.63s, 25 warnings — Starlette/httpx deprecation only) — was 307 before M10; +9: owner-transfer tests (7 SQLite + 2 live). Live smoke: owner transfer 200 (old owner → admin, new owner → owner), ex-owner transfer again → 403, revoke sole owner → 422. |
| Backend pytest, owner role live modules | **11 passed** (imports 5 + workspaces 6) |
| Frontend build (`npm run build`) | **pass** — `tsc -b && vite build`, 198 modules, built in ~1.8s, JS 577.96 kB (pre-existing >500 kB chunk-size warning only) |
| Frontend Vitest (`npm test`) | **50 passed, 0 failed** (8 files — incl. 4 new owner-transfer Team tests) |
| P0 prototype untouched (`prototype-p0`, full suite) | **47 passed, 0 failed** — P0 files unmodified |
| Alembic live (`alembic upgrade head` on `courierledger_dev` and `courierledger_app`) | **pass** — 33 tables each (32 + `alembic_version`); RLS ENABLE + FORCE on 26 tenant tables |
| `rls.sql` / `triggers.sql` / `roles.sql` applied live | **pass** — 45+ policies, 21 CREATE TRIGGER, 49 GRANTs per database, all exit 0 |
| B-1 live workspace tests (`test_workspaces_live_pg.py`, 6 tests) | **6 passed under the owner role; 6 passed under `courierledger_runtime`** — create 201 with owner membership, list shows it for A and `[]` for B, GET 404 for B / 200 for A, unauthenticated 401, second workspace lists both, revoked membership hides the workspace; SQL-level: A sees 1 merchant, B sees 0, no user sees 0, direct INSERT into `merchants` denied |
| B-4 live runtime-role tests (`test_runtime_role_live_pg.py`, 5 tests) | **5 passed** — app session is `courierledger_runtime` with `rolbypassrls = false`; runtime role cannot UPDATE `audit_logs`, DELETE `idempotency_keys`, or UPDATE `merchants`; sanctioned narrow UPDATE on `reconciliation_runs` passes; pooled-connection context does not leak (pool_size=1, both settings read `''` after commit); auth/workspace/reconcile smoke under the runtime role (Ex A exact) |
| B-3 live lease tests (`test_idempotency_lease_live_pg.py`, 6 tests) | **6 passed** — completion before expiry succeeds, after expiry affects zero rows; **decisive clock test**: 2-second lease + `pg_sleep(3)` in the same transaction → completion fails (proves `clock_timestamp()`, not `now()`); reclaim race has exactly one winner; renew/fail obey the ownership predicate; hash mismatch conflicts; completed key replays. SQLite unit tests: **9 passed** |
| M4 CSV import tests (`test_csv_import.py`, 24 tests, SQLite) | **24 passed** — fixture end-to-end (12 rows, 10 parsed, 7 error rows), amount-only rejection, weak fallback needs human confirmation, duplicate tracking suspends matching, ambiguous repeated tracking, DQ states (absent/blank/invalid/explicit zero), mapping snapshot immutability + file reuse, idempotent replay, hash-mismatch conflict, mapping validation, no-membership refusal, money parsing units |
| M4 live import tests (`test_imports_live_pg.py`, 5 tests) | **5 passed under `courierledger_runtime`; 5 passed under the owner role** — HTTP upload 201 (12 rows / 10 parsed / 6 error rows with no shipments seeded), detail shows duplicate/amount-only/DQ rows, same-key re-upload replays the same batch, second user gets 404 on list/detail/upload, unauthenticated upload 401, bad mapping 422, scratch DB has the 5 seeded courier codes |
| Manual live smoke, persistent dev DB (`courierledger_app`, runtime role, 2026-10-31 ~16:52 UTC) | health 200; register 201; login 200; workspace create 201; courier account create 201 (steadfast); **CSV upload 201** — batch `parsed`, 12 rows / 10 parsed / 6 error rows, mapping snapshot stored; list shows the batch. Server stopped afterwards |
| RLS live, runtime role (`courierledger_runtime`) | **verified**: tenant A sees only ORD-A, tenant B only ORD-B, no tenant sees 0 rows, cross-tenant INSERT rejected (`new row violates row-level security policy for table "orders"`), malformed tenant UUID returns 0 rows with no error (CL-06 fail-closed) |
| Append-only live (superuser UPDATE/DELETE on `audit_logs`) | **verified**: both raise `append-only table audit_logs: UPDATE/DELETE is not permitted`, row intact (`test.insert`) |
| Live app vs PostgreSQL (`courierledger_app`, uvicorn, runtime role) | health `{"status":"ok","service":"courierledger-m1","database":"postgresql","database_ok":true}`; register 201, login 200, `/me` 200; **workspace create 201, list shows it** (B-1 fixed); reconcile V2.2 Ex A exact: C −5000, V_total −4950, V_cond 0, contractual `matched`, overall `open` |
| Live app vs SQLite test fallback (labelled `sqlite-test-only`) | register 201, login 200, `/me` 200, workspace create 201 (role owner), list 200, wrong password 401, no-CSRF register 403, logout then `/me` 401; reconcile Ex A same values as above |
| Frontend ↔ backend live (Vite dev on 5173, backend on 8000) | **verified** (M5, 2026-10-31): app shell serves (`<title>CourierLedger — COD Courier Reconciliation</title>`), `GET /api/health` through the Vite proxy returns the live PostgreSQL health JSON; fresh synthetic user `m5smoke@example.com` registered/logged in via the proxy, workspace "M5 Smoke Shop" + Steadfast account created, fixture CSV uploaded (batch `659f7bb7…`, 12 rows / 10 parsed / 6 error rows), list + detail return the real import; demo reconcile through the proxy returns all five statuses exactly (matched / open C −5000 / needs_review / mismatch V_cond −2000 / matched_with_tolerance at tolerance 500). Servers stopped afterwards |
| Playwright e2e (`npx playwright test`) | **3 passed, 0 failed** (28.4s) — full happy path register → workspace → import → run → settle against real backend + PostgreSQL; settle steps now drive the real batch-detail UI forms (receipt, complete, allocate, calculate variance −40000, reverse → `not_fully_allocated`). See `frontend/e2e/README.md`. |
| Owner transfer (`POST /api/workspaces/{id}/members/transfer-owner`) | **verified live** — `test_members.py` 24 passed, `test_members_live_pg.py` 9 passed under the runtime role and 9 under the owner role; Team page transfer control with two-step confirm. OD-11 closed (see §7). |
| GitHub Actions CI (`.github/workflows/ci.yml`) | **written, YAML-validated, never run** — 3 jobs (backend pytest incl. live PostgreSQL, frontend build + Vitest, Playwright e2e), postgres:16 service, provisioning mirrors §3. No git repository exists here, so no GitHub run has happened; the live-PostgreSQL `su postgres` shim is the riskiest unproven part. |
| M12 local verification (`m1/LOCAL_VERIFICATION.md`, 2026-11-01) | **all green, run by the coordinator on this VM** — backend pytest **316 passed, 0 failed, 0 skipped** with live PostgreSQL under the runtime role (176.55s; SQLite-only run 246 passed / 70 skipped); frontend build pass, Vitest **50 passed**; Playwright **3 passed, 0 failed** (37.2s, first try, no retry); P0 **47 passed**. **Container-restart re-verification (2026-11-01 ~16:0x Dhaka):** a service restart replaced the container and PostgreSQL 16 vanished entirely; restored from cached apt archives (see `m1/LOCAL_VERIFICATION.md` §1a — `apt-get install` could not locate the package, deps `libpq5`/`libjson-perl`/`ssl-cert`/`libllvm17t64` installed from `/var/cache/apt/archives/`, UTF8 cluster recreated, both DBs re-provisioned: 33 tables, alembic 0002, 46 policies, 5 couriers, both roles authenticate) and Playwright re-run **first try: 3 passed, 0 failed, 26.5s** after a one-line e2e selector fix (`getByRole('heading', { name: 'Settlement batch', exact: true })` — the loose name matched two headings, strict-mode violation; the page itself had loaded fine). Backend/Vitest/P0 figures are the pre-restart verification; e2e is the post-restore verification. Canonical GitHub Actions still blocked (account billing lock, run 41286668638 attempts 1–2, zero tests run); self-hosted runner parked (see §5.13–14). Full procedure in `m1/LOCAL_VERIFICATION.md`. |
| Docker / `docker compose` | **partially verified, see §5** — Dockerfiles written + hadolint-clean, `docker compose config` exits 0, base images loaded; **image builds and `docker compose up` NOT run** (containers cannot start in this VM) |

Engine port note: one genuine P0 bug was found and fixed in the port — a non-integer `cod_fee_rate_bp` raised `TypeError` in P0 instead of becoming NULL + `DQ_INVALID_VALUE`; M1 reads the rate after the cleaning loop (see `backend/app/domain/README.md` and `IMPLEMENTATION_CHECKLIST.md`). No formula or worked-example value changed.

## 5. Known blockers and discrepancies (do not silently work around)

1. **BLOCKER — Docker builds and `docker compose up` cannot run in this VM (B-2, environment limit, not an app defect).** Evidence, 2026-10-31: Docker 29.1.3 + Compose v2.39.0 installed; daemon runs only with `--iptables=false --bridge=none --storage-driver=vfs`; base images loaded via skopeo; but `docker run --rm python:3.12-slim python --version` fails at container creation with `runc create failed: ... error spawning mount remapping thread: mount source thread: join container mntns: setns: operation not permitted`, and `docker build` fails at the first RUN step with the same error. The daemon's own image pulls fail with TLS handshake timeouts through the egress proxy (shell curl reaches the registry in <1s; skopeo works). Verified instead: both Dockerfiles pass hadolint with no findings; `docker compose config` exits 0 and resolves services `backend` + `db`; build contexts are 1.3 MB (backend) and 269 kB (frontend) after `.dockerignore`. **Not verified and not claimed:** image builds, `docker compose up -d db`, containerised DB provisioning, containerised backend health. To finish B-2, run on a machine that can start containers: `docker build -t courierledger-backend:dev ./backend && docker build -t courierledger-frontend:dev ./frontend && docker compose up -d db` (provision the containerised DB with Alembic as the owner role, then `rls.sql` / `triggers.sql` / `roles.sql` and the runtime-role password), then `docker compose up backend` and the health + workspace + reconcile smoke through the container. Note: the frontend service stays commented in `docker-compose.yml` because `vite preview` does not apply the dev-server `/api` proxy (`frontend/vite.config.ts` targets `http://127.0.0.1:8000`, dev-server-only), so the containerised frontend cannot reach the backend yet — `npm run dev` remains the supported local flow.
2. **Table count: 32, not 31.** The brief said "31 total" but its own list names 32 tables (it includes `password_reset_tokens`, absent from v0.1 §C). All 32 are implemented; nothing was dropped to force the numbers to agree. The "31" figure in v0.3–v0.5 counts tables without the reset-token table.
3. **Tenant setting names:** v0.1 E.2 names `app.merchant_id`; the build uses `app.current_merchant_id` everywhere (session.py, rls.sql, triggers.sql), following the kickoff brief. `app.user_id` (v0.1 E.2 name) IS now set by the app (B-1: `set_user_context` in `app/db/session.py`, called from `api/deps.py`) — deliberately asymmetric with `app.current_merchant_id`; do not silently rename either setting.
4. **Resolved — B-1 workspace RLS (fixed 2026-10-31, verified live).** Was: `POST /api/workspaces` → 500 `new row violates row-level security policy for table "merchants"`, list → `[]`. Root cause was the chicken-and-egg in `rls.sql` (tenant-context policy on `merchants`, no context for a new merchant). Fix (v0.1 §E.3): membership-based `merchants` SELECT policy keyed on `app.user_id`; `merchant_memberships` split into SELECT/INSERT/UPDATE policies; `NO FORCE` RLS on `merchants` + `merchant_memberships` only; SECURITY DEFINER `public.create_workspace()` (owned by the table-owner role `courierledger` in this environment — literal migrator ownership would break the NO FORCE bypass because that role does not own the tables; CL-26 hardening applied: `SET search_path`, schema-qualified objects, no dynamic SQL, EXECUTE revoked from PUBLIC and granted to the runtime role); app wiring in `workspace_service`/`deps`; 6 live tests pass under both roles. Probe findings recorded in `IMPLEMENTATION_CHECKLIST.md` (notably: `SET row_security TO off` inside a SECURITY DEFINER function does NOT bypass RLS — it errors).
5. **Resolved — B-4 runtime-role connection (fixed 2026-10-31, verified live).** The app now connects as `courierledger_runtime` by default (`backend/app/config.py`, `m1/.env.example`); Alembic prefers `MIGRATION_DATABASE_URL` (owner role). 5 live runtime-role tests pass, including the append-only privilege denials and the pooled-connection context-isolation check.
6. **Resolved — B-3 lease clock (fixed 2026-10-31, verified live).** `app/services/idempotency_service.py` is the only module that updates `idempotency_keys`; all lease comparisons use PostgreSQL `clock_timestamp()` (statement-execution wall clock), never `now()` (transaction-start time) — stated in the module docstring and the SQL it builds. 6 live tests pass, including the decisive `pg_sleep(3)` clock test; 9 SQLite unit tests pass.
7. **M4 import grants (by design, not a bug).** `roles.sql` grants the runtime role SELECT+INSERT only on `import_rows`, `statement_lines`, `import_errors` and `shipment_match_candidates` (UPDATE only on `import_batches`). The import service therefore parses and decides the match first and inserts each row/line once with its final `parse_status`/`match_status` — it never updates a row or line after insert. A tenant context must be set before the courier-account lookup in `run_import` (the account SELECT policy keys on it — found live as a 404), and the batch summary re-asserts the tenant context after commit before refresh (commit clears transaction-local settings — the B-1 pattern).
8. **Auth deviations from v0.1 §C** (recorded, not silently "fixed"): session column is `token_hash`, not `refresh_token_hash`; membership default is `'active'`, not `'invited'`; identity tables use app-side UTC defaults, not server `now()`. See `backend/app/db/README.md`.
9. **Other contradictions** (8 recorded in `backend/app/db/README.md`, isolated not resolved): idempotency operations (v0.1 lists 4 dotted names vs CL-04's 2 — build follows CL-04); `workspace_settings.tolerance_poisha` DEFAULT 0 is the single asserted exception to the no-money-default rule; CL-06 malformed-UUID NULL (implemented) vs v0.1 E.2's raising cast (superseded by CL-06).
10. **Password reset: DECIDED 2026-11-01 — keep log-only for local dev.** No email provider chosen, no vendor integrated, no mailer abstraction added (see `backend/README.md`, "Milestone 10 — password-reset email decision"). Switching on needs Naim's provider choice, config keys, a sender interface at the one call site, removal of the raw token from logs, request rate-limiting, and the frontend `/reset-password` page. **Never ship log-only to production.**
11. **Rate limiting is process-local** (in-memory, 5 failed logins / 15 min per IP+email); not shared across processes.
12. **Run store exists (M6).** `POST /api/reconcile` computes and returns only; persisted runs are created from imports via `POST /api/workspaces/{id}/imports/{batch_id}/runs` (see `IMPLEMENTATION_CHECKLIST.md`). The P0 AT-12 run-immutability test was therefore not ported in that form.
13. **DB role-password drift (recurring, found again 2026-11-01, M12).** The `courierledger` / `courierledger_runtime` role passwords have drifted from the documented placeholder after container replacements (M6/M7/M8) and after the self-hosted runner's provisioning (M12 — it set both roles to the workflow's password value, which at the time came from a redacted copy, so local `.env` authentication broke). A live-suite failure with `FATAL: password authentication failed for user "courierledger"` is first a drift symptom, not an app defect: verify both roles authenticate with the `.env` password, reset them as the postgres superuser if not, then re-run. Full diagnosis + reset procedure in `m1/LOCAL_VERIFICATION.md` §2. The repo secret `CI_POSTGRES_PASSWORD` holds the same fresh random value as the local roles (Phase 1 rotation, 2026-11-01); self-hosted runs authenticate with it and never reset the shared role passwords.
14. **Self-hosted GitHub runner — parked (M12).** The 2026-11-01 attempt registered runner `courierledger-vm`, ran three runs, then went offline (process gone); registration removed 2026-11-01. Results: runs 41288531890 / 41290116524 failed at provisioning (shell syntax error in the heredoc — a redacted copy had been pasted; fixed in commit `3e82c81`); run 41292188799 frontend success, e2e failed (30-minute job timeout in "Set up Node 24" — Node download/cache-restore stalled through the egress proxy), backend job never started. **A real password was committed** in the first version of `.github/workflows/ci-selfhosted.yml` (`CI_PG_PASSWORD` as a literal); removed 2026-11-01, and in Phase 1 (2026-11-01) the literal fallback was removed entirely — both workflows now fail closed when the `CI_POSTGRES_PASSWORD` secret is unset. The value remains in git history (commits `6c6c03b`–`3e82c81`); it only ever guarded synthetic-data databases on this VM and both roles have been reset since — no history rewrite is done. The workflow file stays as the tested record of how CI runs on this VM. Canonical CI is still blocked by the account billing lock; see `m1/LOCAL_VERIFICATION.md` §7.

## 6. What is NOT in this build (Milestone 5 done)

Milestone 4 (CSV import) is done: configurable column mapping, immutable mapping snapshots per import, original files preserved (`file_objects` + SHA-256, bytes under `backend/var/imports/` as a local-dev stand-in), raw values preserved, duplicate detection, ambiguous matches flagged for review, never a match on amount alone, idempotent `import_create` keys, synthetic generic-statement fixtures only. Milestone 5 (dashboard) is done: Dashboard (live health + real workspace/import counts + labelled demo reconcile), Imports list/detail + CSV upload + courier accounts, Shipments and Reconciliation driven by the live engine over labelled synthetic demo input (all five V2.2.1 statuses, full breakdown, C/V_R/S/V_cond/V_total kept separate), honest "backend endpoint pending" pages for Courier Statements / Reports at the time (wired to real endpoints in M8), Team (real workspaces + roles; member endpoints pending), Settings (documented default tolerance ৳0.00, labelled as the default, nothing saved), auth-gated routes (unauthenticated → login). Milestone 6 (reconciliation run store) is done and verified live: persisted immutable runs from imports, run list/detail pages, CL-01/CL-13/CL-16 triggers live-tested. Milestone 7 (settlement & bank receipts) is done and verified live: M6-A1 identity-CHECK amendment via migration `0002`; settlement-batch / bank-receipt / allocation / reversal / variance endpoints; CL-16/CL-21 current-variance derivation as a read-side service with NULL reasons; CL-10 dedicated live test (both layers); Settlement Batches and Bank Receipts pages wired to the endpoints with Bangla labels. Fully allocated means every receipt linked to the batch is allocated to it in full (v0.1 T-17) — so a batch whose receipts total 60000 against an expected 100000 records variance −40000. Milestone 8 (shipments/orders creation, courier statements, reports) is done and verified live: orders + shipments creation API (V2.1 outcome→expected-collectible derivation, one COD-bearing shipment per order, NULL kept distinct from verified zero), read-only statement-line endpoints, reports summary from real aggregates only (five variance totals kept separate), cross-batch fully_allocated live test, and the Shipments / Courier Statements / Reports pages wired to them. Milestone 9 (team & workspace management, e2e) is done and verified live: member invite/accept/role/revoke endpoints per the CL-20 advisory-lock protocol with the last-active-owner guard (OD-11 mechanism settled — locked membership transaction), workspace settings endpoints (tolerance config by Owner/Admin, proven to flow into runs: mismatch at 0 → matched_with_tolerance at 500), Team and Settings pages wired to the real endpoints, Playwright 1.64.1 + Chromium installed with the happy-path suite **3 passed** against the real backend + PostgreSQL, and four frontend/backend contract bugs found by the e2e suite and fixed (register password length, run-detail flat shape, settlement-detail flat shape, settlement/bank-receipt list envelopes + `result_counts`). Milestone 10 (production-readiness hardening and M1 close-out) is done and verified live: the Playwright settle steps now drive the real batch-detail UI forms (no settle mutation uses `page.request` anymore); owner transfer endpoint `POST /api/workspaces/{id}/members/transfer-owner` with the last-owner guard (old owner becomes an admin) — **OD-11 closed**; password-reset email decision recorded (log-only for local dev, see `backend/README.md`); GitHub Actions CI workflow written and YAML-validated (`.github/workflows/ci.yml`: backend pytest incl. live PostgreSQL, frontend build + Vitest, Playwright e2e) but **never run** — there is no git repository here at all; production-readiness review written in `IMPLEMENTATION_CHECKLIST.md` with verdict **NOT production-ready**. Not started: production deployment, real merchant data.

## 7. Production readiness — NOT ready

**M1 is NOT production-ready.** It is a verified local development build. The full item-by-item review is in `IMPLEMENTATION_CHECKLIST.md` ("M10 — production-readiness review"). In short: the engine, triggers (CL-01/CL-13/CL-16/CL-10), auth/RLS, idempotency, import/run/settlement/team flows and OD-11 are verified live and ready; **not ready** are OD-12 (needs redacted merchant statements), OD-16 and OD-17 (need Bangladesh policy/legal review), B-2 Docker (containers cannot start in this VM), the missing ADR 1.0, CI (written, never run, no repo), password reset (log-only by decision), and market validation (Day 7 / Day 14 gates not run, no merchant-confirmed courier evidence). What would have to change: a container-capable machine proving B-2 end to end; a first green GitHub Actions run on a real repository; ADR 1.0 written and checked; OD-12/OD-16/OD-17 closed with the resulting behaviour implemented; password-reset email switched on; validation gates run; and a deployment target with real secrets management, TLS, `Secure` cookies, and a non-local rate limiter. **Do not deploy M1 to production and do not load real merchant financial data into it until then.**

## 8. Deliberately not done

- No production deployment, no paid services, no live courier APIs, no real merchant financial data, no bank CSV, no invented courier rates/templates/fee semantics. All demo data is synthetic and labelled `SYNTHETIC FIXTURE` / `SYNTHETIC DEMO`.
- No secrets are hardcoded anywhere; `.env.example` holds placeholders only. The local dev DB password is a fresh random value (Phase 1 rotation, 2026-11-01) stored only in gitignored `backend/.env` and the `CI_POSTGRES_PASSWORD` repo secret — never write it into any doc.
- The SQLite fallback is test-only (health smoke test + pytest suite). It is not the M1 data model and must never hold production data.
