# CourierLedger M1 Backend — Milestones 0 (foundation) + 1 (auth)

FastAPI + sync SQLAlchemy 2.x + psycopg (v3), Pydantic v2, Argon2id
sessions. PostgreSQL 16 is the target database. **No PostgreSQL
server was available in this environment at build time** (see
"Environment status" below), so the app is PostgreSQL-only in code
with a clearly-labelled temporary SQLite fallback used ONLY for the
health smoke test and the pytest suite. The M1 data model is NOT
built on SQLite.

## Environment status (2026-10-31, verified at build time)

- Python 3.12.3, `pip install` works (venv below).
- `psql` NOT installed; `docker` NOT installed; the coordinator
  session was running `apt-get install -y postgresql postgresql-contrib`
  while this was built — it had not finished, and no local PostgreSQL
  server was reachable. `~/.postgres-ready` exists but its meaning is
  unestablished; it is NOT treated as a PostgreSQL environment.
- Consequence: `/api/health` reports `"database": "sqlite-test-only"`
  in the smoke test and `"unavailable"` against the default
  PostgreSQL URL until a server exists. It never claims PostgreSQL
  is ok without a real `SELECT 1` round-trip.

## Setup (exact commands run, 2026-10-31)

```bash
cd m1/backend
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Installed versions (pinned in `requirements.txt`):

| Package | Version |
|---|---|
| fastapi | 0.162.0 |
| uvicorn | 0.54.0 |
| SQLAlchemy | 2.1.6 |
| psycopg / psycopg-binary | 3.3.6 |
| pydantic / pydantic-settings | 2.14.2 / 2.16.2 |
| argon2-cffi | 25.1.0 |
| python-multipart | 0.0.33 |
| email-validator | 2.3.0 |
| httpx | 0.28.1 |
| alembic | 1.20.1 |
| pytest | 9.2.0 |

## Configuration

Copy `../.env.example` to `.env` (at `m1/` for compose, or
`backend/.env` for local runs) and replace every `CHANGE_ME`.
Defaults (no `.env`): PostgreSQL URL
`postgresql+psycopg://courierledger_runtime:CHANGE_ME@127.0.0.1:5432/courierledger`
(the app connects as the RUNTIME role -- B-4 fix; migrations use
`MIGRATION_DATABASE_URL` with the owner role, see `alembic/env.py`),
frontend origin `http://127.0.0.1:5173`, 7-day sessions.
No secrets are hardcoded anywhere in this codebase.

## Run

PostgreSQL (once a server exists):

```bash
cd m1/backend
# with DATABASE_URL pointing at PostgreSQL, and Alembic migrations applied
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Temporary smoke test (SQLite fallback, dev only — NOT production):

```bash
cd m1/backend
DATABASE_URL="sqlite:////tmp/cl_m1_smoke.db" CREATE_TABLES_ON_STARTUP=true \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`CREATE_TABLES_ON_STARTUP=true` creates the auth tables via
`Base.metadata.create_all` — dev/test convenience only. Production
schema is owned by Alembic migrations (`backend/alembic/`, owned by
the DB-models agent). Default is `false`. Note: under the runtime role
(B-4) `CREATE_TABLES_ON_STARTUP=true` fails (the runtime role has no
CREATE privilege) -- that is intended; schema changes go through
Alembic as the owner role.

## Verified live smoke test (2026-10-31, SQLite fallback)

```bash
curl -s http://127.0.0.1:8000/api/health
# {"status":"ok","service":"courierledger-m1","database":"sqlite-test-only","database_ok":true}

# CSRF first, then register -> login -> /me -> create workspace -> list
curl -s -c cookies.txt http://127.0.0.1:8000/api/auth/csrf
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -d '{"email":"smoke@example.com","password":"correct-horse-battery-1","display_name":"Smoke Test"}' http://127.0.0.1:8000/api/auth/register
# -> 201 {"id":"35849f96-...","email":"smoke@example.com","display_name":"Smoke Test","status":"active"}
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -d '{"email":"smoke@example.com","password":"correct-horse-battery-1"}' http://127.0.0.1:8000/api/auth/login
# -> 200 (same user object; session cookie set)
curl -s -b cookies.txt http://127.0.0.1:8000/api/auth/me          # -> 200
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -d '{"name":"Smoke Shop"}' http://127.0.0.1:8000/api/workspaces
# -> 201 {"id":"dff64750-...","name":"Smoke Shop","status":"active","role":"owner",...}
curl -s -b cookies.txt http://127.0.0.1:8000/api/workspaces       # -> 200, list with 1 workspace

# Negative checks (second run, port 8001): register without CSRF -> 403,
# login wrong password -> 401, logout -> 200, /me after logout -> 401.
```

Server was stopped afterwards; no uvicorn process remains.

## Tests

```bash
cd m1/backend
.venv/bin/python -m pytest tests/test_health.py tests/test_auth.py tests/test_password_reset.py -q
# 23 passed (2026-10-31)
.venv/bin/python -m pytest tests/ -q
# 61 passed — 23 M0/M1 tests above + 38 M3 engine/reconcile tests
# owned by the parallel Milestone 3 agent (tests/test_engine.py,
# tests/test_api_reconcile.py, app/domain/engine.py).
```

### Skipped tests (Milestone 11 breakdown, 2026-11-01)

Full suite without live credentials: **246 passed, 70 skipped** (316 collected; `pytest --collect-only` reconciles exactly). All 70 skips come from exactly two module-level `pytestmark = pytest.mark.skipif(...)` gates in the 11 `*_live_pg.py` modules — there is no other skip mechanism in the suite. **Group A — 65 tests, 10 files:** live-PostgreSQL gate on `CL_LIVE_PG_PASSWORD` only (scratch DB `courierledger_live_test`, Alembic, `rls.sql`/`triggers.sql`/`roles.sql`/`seed_couriers.sql`, uvicorn :8001). **Group B — 5 tests, `test_runtime_role_live_pg.py`:** stricter gate, also requires `CL_LIVE_PG_ROLE=courierledger_runtime`. Both groups are **intentional** (SQLite cannot exercise RLS, deferred triggers, idempotency leases, or the runtime role) and **should run in CI** — the workflow's live step sets both variables, so the expected CI signal there is 0 skips, 316 run. Run them locally with:

```bash
CL_LIVE_PG_PASSWORD='<password>' CL_LIVE_PG_ROLE=courierledger_runtime \
  .venv/bin/python -m pytest tests/ -q
# 316 passed, 0 failed (2026-11-01, runtime role)
```

The M0/M1 tests use a temporary SQLite file (`/tmp/cl_m1_test.db`,
set in `tests/conftest.py` before the app is imported). This is a
**test-only fallback**, not the app data model. Two SQLite-only
accommodations exist and are labelled in code: `length()` instead of
`char_length()` in one CHECK constraint, and `set_tenant_context()`
skips the PostgreSQL-only `set_config()` call on non-PostgreSQL
dialects (UUID validation still runs). Both are no-ops on PostgreSQL.

## Layout

```
app/
  main.py            FastAPI app, CORS (Vite origin only, credentials),
                     JSON logging, global {"detail": ...} error handler
  config.py          pydantic-settings, env-based, no hardcoded secrets
  logging_config.py  structured JSON logging (one object per line)
  security.py        Argon2id (t=3, m=65536 KiB, p=4), opaque session
                     tokens, SHA-256 hash storage, constant-time compare
  db/
    session.py       engine + SessionLocal + get_db + set_tenant_context
                     (SELECT set_config('app.current_merchant_id',...))
    auth_models.py   TEMPORARY home for users/merchants/memberships/
                     user_sessions/password_reset_tokens — see below
    # models.py and alembic/ are OWNED BY THE DB-MODELS AGENT — not created here
  api/
    health.py        GET /api/health (real connection check only)
    auth.py          register/login/logout/me/revoke-all/csrf/password-reset
    workspaces.py    create/list/get workspaces (roles owner/admin/member/viewer)
    deps.py          get_current_user, require_csrf (double-submit)
  services/
    auth_service.py  registration, sessions, password reset
    workspace_service.py  workspace CRUD + membership checks
    rate_limit.py    in-memory login rate limit (5 failures / 15 min / IP+email)
tests/
  conftest.py, test_health.py, test_auth.py, test_password_reset.py
```

## Auth design (M1)

- **Passwords:** Argon2id, time_cost=3, memory_cost=65536 KiB,
  parallelism=4. Minimum 12 characters (422 below that).
- **Sessions:** server-managed. The cookie holds an opaque random
  token (`secrets.token_urlsafe(32)`); only its SHA-256 hash is
  stored in `user_sessions`. Cookie: HttpOnly, SameSite=Lax,
  `Secure=false` in dev (set `SESSION_COOKIE_SECURE=true` behind
  HTTPS), Path=/, 7-day max-age. Logout deletes the row;
  `/revoke-all` deletes all of the user's sessions. Password reset
  also revokes all sessions.
- **CSRF:** double-submit cookie + `X-CSRF-Token` header, required
  on every state-changing endpoint. Missing/mismatched -> 403.
  Flow: `GET /api/auth/csrf` first, then send the returned token
  in the header (the cookie is set automatically).
- **Rate limiting:** 5 failed logins per (IP + email) in 15 minutes
  -> 429. In-memory and process-local; a multi-worker deployment
  must replace it with a shared store before production.
- **Tenancy (v0.2 CL-11):** tenant scope comes ONLY from the
  authenticated user's server-side membership. Request bodies do
  not accept `merchant_id` (`extra="forbid"` -> 422); path ids are
  verified against `merchant_memberships` before any data is
  returned, and a non-member gets 404 (existence not leaked).
  `set_tenant_context()` sets transaction-local
  `app.current_merchant_id` from the verified id only; a malformed
  UUID sets NULL and fails closed (v0.2 CL-06) — no exception, no
  default tenant.
- **Password reset:** `POST /api/auth/password-reset/request`
  always returns 200 (no account-enumeration oracle). Tokens are
  stored only as SHA-256 hashes, single-use, 1h expiry. **No email
  is sent in M1** — the reset link is written to the structured log
  as a deliberate stopgap (see "Open items").

## DB-models consolidation note

`app/db/auth_models.py` was created because `app/db/models.py` did
not exist when this backend was built. The DB-models agent has
since created `app/db/models.py` + `backend/alembic/` and chose
**reuse over moving**: `models.py` imports `User`, `Merchant`,
`MerchantMembership`, `UserSession`, `PasswordResetToken` and the
shared `Base` from `auth_models.py` and does NOT redefine them, so
`Base.metadata` holds all tables as the single Alembic target. The
DB-models agent also added the CL-15 tombstone column
`merchants.anonymised_at` (nullable) plus its CHECK to `Merchant`
in `auth_models.py`. Do NOT duplicate these tables in `models.py`
and do NOT delete `auth_models.py` — it is the canonical home of
the identity tables. (Note: `models.py` refers to an
`app/db/README.md` that did not exist at the time of writing;
that file is the DB-models agent's to write.)

## Open items

1. **No PostgreSQL verification yet.** The app is PostgreSQL-only
   in code, but no server was reachable at build time, so the
   PostgreSQL path (engine connect, `set_config`, RLS behaviour)
   is unproven against a live database. Re-run the smoke test and
   the suite against PostgreSQL once the coordinator's install (or
   `docker compose up -d db`) provides one. Blocker, not a code gap.
2. **No email provider for password reset.** Reset links are logged,
   not emailed. Production must send the link by email and must NOT
   log the raw token. The log line is labelled in code.
3. **In-memory login rate limit** is process-local (see above).
4. **Run immutability / RLS / migrations** are the DB-models
   agent's scope (`models.py`, `alembic/0001_initial_m1.py` now
   exist); this backend does not auto-create tables by default
   (`CREATE_TABLES_ON_STARTUP` is opt-in dev/test only).
5. **OD-03 (external identity provider)** is open in the spec:
   `users.password_hash` is nullable for IdP accounts; password
   login for such accounts returns 401.
6. **Session cookie `Secure=false`** is a dev default; set
   `SESSION_COOKIE_SECURE=true` (and real `SECRET_KEY` /
   `SESSION_SECRET`) before any non-local deployment. The app
   refuses to start in `production` mode with placeholder secrets.
7. **Concurrent work noted:** while this was built, the Milestone 3
   agent added `app/domain/engine.py`, `app/api/reconcile.py` (not
   yet mounted in `main.py`), `tests/test_engine.py`,
   `tests/test_api_reconcile.py` and `backend/pytest.ini`, and the
   frontend agent scaffolded `m1/frontend/`. Those files were not
   touched here; the full suite (61 passed) covers both agents'
   tests.

---

## Milestone 6 — reconciliation run store (2026-10-31)

Persisted runs, results, fee lines and findings for one M4 import
batch (`app/services/run_service.py`, `app/api/runs.py`,
`tests/test_run_store.py`, `tests/test_runs_live_pg.py`).

- **Creation:** `POST /api/workspaces/{id}/imports/{batch_id}/runs`
  (201 created; 200 on idempotency replay; 422 when the batch cannot
  produce a run; 409 on idempotency conflict; 404 when the batch is
  not in this workspace). Body: `courier_fixture` (default
  `"Fixture Alpha"`) or `rule_version_id` (exactly one), optional
  `tolerance_poisha` (else the workspace setting, 0 when no settings
  row exists), optional `idempotency_key` (or the `Idempotency-Key`
  header). Only `strong_matched` lines with a shipment are eligible;
  everything else is recorded in the run's `input_snapshot.excluded`
  with a reason (`duplicate tracking in batch`, `ambiguous match —
  needs human confirmation`, `no shipment matched`,
  `row failed to parse / skipped`). Zero eligible lines → 422 with
  per-reason counts and **nothing persisted** (eligibility is checked
  before the idempotency claim).
- **Rule context:** a merchant-confirmed `CourierRuleVersion`
  (base `expected_collectible` → engine `full_cod`; VAT
  `included_rate_on_net/gross` → engine `included` + formula;
  `other_fees_poisha` / `excluded_tax_poisha` from `rule_definition`,
  absent → NULL, never 0) or a labelled synthetic fixture
  (`Fixture Alpha/Beta/Gamma`). The full resolved context is stored
  as `rule_snapshot` and hashed into the request hash (idempotency)
  and the run hash (CL-05: an existing run with the same hash is
  returned as a replay instead of inserting a duplicate).
- **Storage:** the engine's values and statuses are stored exactly
  as returned, never recomputed. `X_R` → `x_upper_r_poisha`;
  contractual `unknown` → `not_calculable`; data-quality `complete`
  → `ok`. Fee lines carry all three bases with CL-08 states
  (`contract_missing` for COD bases when `D_c` is unconfirmed);
  `included_tax_poisha` sits on the delivery line only, as a
  breakdown. Findings are never netted; the finding code is
  `code + ":" + basis` (which disambiguates the two STATEMENT
  findings) — see the module docstring for the two documented schema
  insufficiencies (repeated same-code DQ findings get a deterministic
  `:2`/`:3` occurrence suffix; the result identity CHECKs were relaxed
  by M6-A1 — see below — so they no longer reject engine-legitimate
  partial identities).
- **Reads:** `GET /api/workspaces/{id}/runs` (newest first, result
  counts by overall status), `.../runs/{run_id}` (full detail:
  results with fee lines + findings), `.../runs/{run_id}/results`
  and `.../findings` (paginated), `.../imports/{batch_id}/runs`
  (runs for a batch, newest first). Non-members get 404.
- **Immutability:** the only post-creation update is the sanctioned
  narrow Core UPDATE of `status`/`completed_at` (`running` →
  `completed`, exactly one row, guarded by
  `fn_reconciliation_run_guard`). There are no PUT/PATCH/DELETE
  routes for runs, results, fee lines or findings.
- **Compute-only unchanged:** `POST /api/reconcile` persists
  nothing. A persisted run ALWAYS requires a real `import_batch_id`
  (schema NOT NULL); no synthetic import batch may be fabricated.
- **Tests (2026-10-31):** SQLite suite
  `tests/test_run_store.py` — **23 passed** (incl. 5 API tests);
  with `tests/test_api_reconcile.py`, **27 passed**. Full backend
  suite: **147 passed, 41 skipped** (live modules skip without
  `CL_LIVE_PG_PASSWORD`). Live-PostgreSQL suite
  `tests/test_runs_live_pg.py` — see `m1/IMPLEMENTATION_CHECKLIST.md`
  for the live result.

## M6-A1 — relaxed result identity CHECKs (design correction, 2026-11-01)

The approved schema (migration `0001`) required `v_cond_poisha` /
`v_total_poisha` to be NULL whenever any identity term was NULL. The
V2.2.1 engine legitimately emits a non-NULL dependent from a partial
identity, because `V_total = P_r - P_e` is defined whenever `P_r` and
`P_e` are known, and `V_cond = P_r - P_cond` whenever `P_r` and
`P_cond` are known — even when the decomposition terms (`C` / `delta`
/ `V_R` / `S`) are NULL. Two engine-verified examples (both correct
`needs_review` results, asserted from a real `reconcile_shipment`
call in `tests/test_m6a1_live_pg.py`):

- (a) delivered shipment, blank collected, payout 90000, expected
  payout 92000 → `V_total` −2000 with `C`/`delta`/`V_R`/`S` all NULL;
- (b) blank COD-fee line, collected 95000, payout 87050 → `V_cond` 0
  with `S`/`V_R` NULL.

Migration `0002_m6a1_relax_result_identity_checks.py` (revision
`0002`, down_revision `0001`) drops the NULL-propagation arm of both
CHECKs and re-adds them under the same names, so the identity is
enforced only when all terms are non-NULL and nothing is imposed on
the dependent when any term is NULL. The engine is unchanged, and a
genuinely violated identity (all terms non-NULL, dependent ≠ formula)
is still rejected — proven live in `tests/test_m6a1_live_pg.py`
((i)/(ii) store, (iii) rejected) and in SQLite through the real run
store path in `tests/test_run_store.py`. Note for reviewers: the old
CHECK text as written in `0001` also had an AND/OR precedence quirk
in its second arm (`... OR (s IS NULL OR v_r IS NULL AND v_cond IS
NULL)` parses as `s IS NULL OR (v_r IS NULL AND v_cond IS NULL)`),
so the NULL-propagation it intended was never fully enforced; the
relaxed CHECKs state the enforced rule plainly and lose no real
protection — the only rows the old text rejected beyond the new text
are shapes the engine cannot produce (they would need a term to be
both NULL and non-NULL at once).

## Addendum — coordinator integration verification, 2026-10-31

The "no PostgreSQL verification yet" open item above is now closed. PostgreSQL 16.15 was installed via apt and verified live in this session (cluster 16 main, port 5432): `alembic upgrade head` created 33 tables (32 + `alembic_version`) on `courierledger_dev` and `courierledger_app`; `rls.sql` (45+ policies), `triggers.sql` (21 triggers) and `roles.sql` (49 grants) all applied with exit 0; RLS tenant isolation and the append-only trigger were verified live (see `m1/IMPLEMENTATION_CHECKLIST.md` for exact commands and outputs). The live app against PostgreSQL returns `{"status":"ok","service":"courierledger-m1","database":"postgresql","database_ok":true}` and register/login/me work. **Update 2026-10-31:** Blockers B-1 (workspace RLS), B-4 (runtime-role connection) and B-3 (lease clock, `clock_timestamp()`) are fixed and verified live; Milestone 4 (CSV import) is built and verified live; the full backend suite is **146 passed** (runtime role, live modules included). The SQLite fallback remains test-only. The coordinator also wired `app/api/reconcile.py` into `app/main.py` (no prefix — the router declares full `/api` paths) and added `from app.db import models` so startup table creation registers all tables.

## Milestone 8 (part 2) — courier-statement endpoints, reports endpoint, cross-batch live test (2026-11-01)

**Files:** `app/api/statements.py`, `app/api/reports.py`,
`tests/test_statements.py`, `tests/test_reports.py`,
`tests/test_settlement_live_pg.py` (one new live test). No new
tables/columns, no migration edits, no model changes; `app/main.py`
untouched (the coordinator wires the two new routers); `prototype-p0/`
untouched. All data synthetic.

- **Statements — read-only.** `GET /api/workspaces/{id}/statements`
  (list, newest first; `?import_batch_id=&match_status=&page=&page_size=`,
  page 1-based default 1, page_size default 20 max 100) and
  `GET /api/workspaces/{id}/statements/{line_id}` (detail). Each item
  shows exactly what the M4 import stored: raw CSV values verbatim
  from the import row, parse/match status, the six reported amounts
  with their DQ states, the linked shipment where matched (tracking,
  outcome, expected collectible COD), the import batch reference and
  the courier account (courier code, display label). Unknown
  `import_batch_id` → 404; invalid `match_status` → 422; non-member
  → 404; unauthenticated → 401. No POST/PUT/DELETE routes (405).
- **Reports — read-only, real rows only.**
  `GET /api/workspaces/{id}/reports/summary` returns run counts by
  status, result counts by overall status, the five variances
  (C, V_R, S, V_cond, V_total) as SEPARATE totals over stored
  results — each total sums only that variance's non-NULL values,
  with the NULL count beside it; the five totals are never summed
  together — settlement batch counts by allocation status and by
  bank status, bank receipt counts (complete/incomplete), and import
  batch counts by status. Every known status key is present with 0
  where there is no data; an empty workspace returns honest zeros.
- **Cross-batch live test** (`test_cross_batch_allocation_does_not_fully_allocate_live`
  in `tests/test_settlement_live_pg.py`): batch A is fully allocated
  and calculated (variance 0, `ok`), then gains a receipt of its own
  that stays unallocated; batch B's receipt is allocated in full.
  A stays NOT fully_allocated (`partially_allocated`) with current
  variance NULL / `not_fully_allocated`, while B is fully_allocated
  with current variance 0 / `ok` — B's allocation does not leak
  into A. Note: a batch with NO calculation at all reports
  `no_calculation` (the documented reason order in
  `settlement_variance.py` checks that first), so the test records
  A's calculation before adding the unallocated receipt; that is
  the only way to reach the `not_fully_allocated` reason.
- **Wiring the coordinator must do:** in `app/main.py`,
  `app.include_router(statements.router, prefix="/api")` and
  `app.include_router(reports.router, prefix="/api")` (both routers
  declare the `/workspaces/{workspace_id}/...` prefix, following
  the existing imports/runs/settlements convention). The SQLite
  tests mount the routers themselves (guarded) so they run before
  that wiring lands.
- **Tests (2026-11-01):** SQLite suite
  `tests/test_statements.py` + `tests/test_reports.py` —
  **8 passed** (pagination, both filters, verbatim raw values,
  DQ NULL handling, exact aggregate values incl. the M6-A1 NULL
  shapes, empty-workspace zeros, cross-workspace 404s,
  unauthenticated 401s, read-only 405s). Live suite
  `tests/test_settlement_live_pg.py` — **5 passed, 0 failed**
  under `CL_LIVE_PG_ROLE=courierledger_runtime` (incl. the new
  cross-batch test). NOTE: the documented dev-only placeholder
  password (`dev-only-placeholder`) does NOT authenticate the
  `courierledger` role on this container any more — the live run
  used the password stored in gitignored `backend/.env`
  (mode 600); the placeholder in `m1/README.md` is stale for this
  container and needs updating by whoever owns that doc.
- **Full-suite caveat:** a full `pytest tests/ -q` run during this
  task reported **226 passed, 31 failed, 1 error** — but all 31
  failures were in the parallel shipments worker's
  `tests/test_shipments.py` and the run collided with that worker's
  own concurrent pytest on the shared SQLite file
  (`/tmp/cl_m1_test.db`, dropped/recreated per test by conftest):
  this part's combined runs failed mid-test with
  `sqlite3.OperationalError: no such table: merchant_memberships`
  while another `pytest` process was observed running, and the
  same tests pass when run without a concurrent runner. That
  number is therefore NOT a clean regression signal for this part;
  the coordinator should re-run the full suite with no other
  worker running pytest at the same time.

## Milestone 8 (part 1) — orders + shipments creation API (2026-11-01)

**Files:** `app/api/shipments.py`, `app/services/shipment_service.py`,
`tests/test_shipments.py`, `tests/test_shipments_live_pg.py`. No new
tables/columns, no migration edits, no model changes; `app/main.py`
untouched (the coordinator wires the one router — see below);
`prototype-p0/` untouched. All data synthetic; no secrets.

- **Endpoints** (all under `/api/workspaces/{workspace_id}/...`,
  router prefix `/workspaces/{workspace_id}`, wired with
  `prefix="/api"` like imports/runs/settlements):
  `POST /orders` (201), `GET /orders` (paginated, newest first),
  `GET /orders/{order_id}`, `POST /orders/{order_id}/shipments`
  (201), `GET /shipments` (paginated, newest first; filters
  `?order_id=` and `?tracking_id=` — the tracking filter is
  normalised, so `trk-1001` matches `TRK-1001`),
  `GET /shipments/{shipment_id}`. POSTs require the CSRF
  double-submit check; all routes require authentication (401
  without a session) and workspace membership (404 for
  non-members, never a leak). There are deliberately no PUT / PATCH
  / DELETE routes (405).
- **Boundary validation:** money is `StrictInt` poisha — a float,
  a numeric string or a boolean amount is 422, never coerced.
  Request bodies are `extra="forbid"`: no `merchant_id` (the
  tenant comes only from the workspace path parameter after the
  membership check), no `expected_collectible_cod_poisha`, no
  `source`. `currency` must be `BDT`; `outcome` /
  `outcome_source` must be known values from the schema lists.
- **Expected collectible COD is derived server-side**
  (`derive_expected_collectible` in the service; the stored value
  and the derivation are both returned so they can be checked
  against each other): delivered → the merchant-stated amount
  when given, else the order COD (NULL when neither is known);
  partially_delivered → the documented partial amount ONLY
  (amount + evidence ref both present), else NULL, never the full
  order COD; returned / cancelled / superseded → verified 0;
  in_transit / unknown → NULL. An amount supplied for an outcome
  where it is meaningless (returned / cancelled / in_transit /
  unknown / superseded) is 422, not stored or ignored.
- **Split amounts are quarantined, not assumed:** a delivered
  shipment whose stated amount differs from the order total is
  recorded with the stated amount and flagged
  `needs_review: true` with reason
  `split_amount_differs_from_order_total`. `needs_review` is
  computed from stored fields at read time (there is no
  needs-review column on shipments); the other reasons are
  `outcome_not_confirmed`, `order_cod_unknown` and
  `partial_amount_not_documented`.
- **MVP one COD-bearing shipment per order:** a second active
  COD-bearing shipment for the same order is 422
  (`second_cod_bearing_shipment`), checked in the service and
  enforced again by the partial unique index
  `uq_shipments_one_active_cod_bearing_per_order` (a race that
  trips the index maps back to the same 422). A superseded
  shipment is excluded on both sides: it is recorded AFTER its
  successor exists (the successor must be a shipment of the same
  order), and recording it never blocks or counts as the active
  COD-bearing shipment. Note the successor must exist first
  because there is no update endpoint to mark an existing
  shipment superseded afterwards.
- **Import → run compatibility:** tracking ids are normalised
  with the same `csv_import.normalise_tracking` the M4 matcher
  uses, so an API-created shipment strong-matches an import row
  with the same tracking id (proven live, below).
- **Wiring the coordinator must do:** in `app/main.py`, add
  `shipments` to the `from app.api import ...` line and add
  `app.include_router(shipments.router, prefix="/api")` after the
  settlements line. The SQLite tests mount the router themselves
  (guarded: skipped when the route already exists, same pattern
  as part 2), so they pass before the wiring lands; the live
  tests run the real app via uvicorn and need the wiring.
- **Tests (2026-11-01):** SQLite suite
  `tests/test_shipments.py` — **20 passed** (derivation matrix
  incl. NULL vs verified 0, strict-money 422s, second-COD 422,
  split quarantine flag, tracking normalisation, filters,
  cross-workspace 404s, unauthenticated 401s, no-CSRF 403s,
  read-only 405s). Live suite `tests/test_shipments_live_pg.py` —
  **4 passed under the owner role and 4 passed under
  `CL_LIVE_PG_ROLE=courierledger_runtime`** (API-created
  order ORD-1001 + shipment TRK-1001 → fixture upload 201, row 1
  `strong_matched` to the API shipment id → run 201 with exactly
  one result, for the API shipment, expected collectible 100000,
  reported collected 100000; second-COD 422 vs second-non-COD
  and superseded-COD allowed; cross-workspace 404s;
  unauthenticated 401s). Full backend suite with the temporary
  wiring line in place: **261 passed, 0 failed** (runtime role)
  and **256 passed, 5 skipped** (owner role; the 5 skips are the
  runtime-role-only modules), run with no other worker running
  pytest at the same time.
- **SQLite limitation (test-only fallback, not a production
  rule):** the partial unique index uses `postgresql_where`,
  which SQLite ignores, so on SQLite it is a plain UNIQUE index
  on `(merchant_id, order_id)` and ANY second shipment for one
  order fails at the database. The cases that need two shipments
  for one order (second non-COD shipment, superseded shipment
  naming a successor) are therefore covered only in the live-PG
  suite, where the partial index applies.
- **Environment note:** this container was replaced again during
  the task (no `postgres` user, no cluster). PostgreSQL 16.15 was
  reinstalled from the cached apt archives (`dpkg -i` of the
  cached `.deb`s; the data directory survived, both DBs already at
  migration `0002`, 33 tables, 46 policies, 21 triggers, 5
  couriers) and the cluster restarted with `pg_ctlcluster 16 main
  start`. All live numbers above were run after that reinstall.
  On this container the documented dev-only placeholder password
  (`dev-only-placeholder`) DOES authenticate both the
  `courierledger` (owner) and `courierledger_runtime` roles
  (proven by the owner-role and runtime-role runs above) — the
  part-2 note saying otherwise does not match the current state.

---

## Milestone 10 — password-reset email decision (2026-11-01)

### Decision

**Keep log-only for local development. No email provider is chosen,
no vendor is integrated, and no mailer abstraction is added in M1.**

This is a deliberate, documented decision — not an oversight left
open by accident. The reset flow works end to end for local
development (request → token stored → link logged → confirm resets
the password and revokes all sessions), and nothing in this decision
changes that flow.

### What exists today (verified 2026-11-01)

- `app/services/auth_service.py::request_password_reset` — always
  returns success from the caller's point of view (no
  account-enumeration oracle). For an active user with a local
  password it stores a one-time token and writes the reset link to
  the structured log (`password_reset_link_issued`, with the full
  `reset_link` including the raw token). **No email is sent.**
- `app/db/auth_models.py::PasswordResetToken` — only the SHA-256
  hash of the token is stored; single-use (`used_at`); 1-hour expiry
  (`settings.password_reset_ttl_seconds`); the link is built from
  `settings.frontend_url` + `/reset-password?token=<raw token>`.
- `app/api/auth.py` — `POST /password-reset/request` always 200;
  `POST /password-reset/confirm` consumes the token, sets the new
  password (minimum 12 characters) and revokes all sessions.
- **No mailer abstraction exists in the codebase.** A search of
  `app/` finds no SMTP client, no transactional-email client, no
  sender interface and no no-op sender — the `logger.info` call in
  `request_password_reset` is the only delivery path. No abstraction
  was added here on purpose: inventing a seam for a provider nobody
  has chosen would be unreviewed design, not a decision.
- **The frontend has no reset-password page yet.** No
  `/reset-password` route exists in `m1/frontend/src`, so a link
  emailed today would land on a missing page even if a provider
  were wired up. Building that page is part of switching email on,
  not a separate afterthought.
- Tests: `tests/test_password_reset.py` (6 tests) cover the
  log-only flow. No test fakes an email send, because there is no
  email send to fake.

### Exact reason for staying log-only

1. **M1 is a local development build only.** There is no production
   deployment, no public domain, and no sender identity. A real
   password-reset email needs a verified sending domain with
   SPF/DKIM/DMARC, bounce handling and deliverability monitoring —
   none of which exists, and none of which a local dev build needs.
2. **Choosing a provider is a vendor and cost commitment.**
   It picks who holds users' email addresses, what the free tier
   covers, and how deliverability to Bangladeshi inboxes behaves.
   That choice belongs to Naim, not to a Milestone 10 worker, and
   no provider account exists.
3. **The log-only link is safe only because the logs stay local.**
   The raw reset token is a secret equivalent to a password: anyone
   with the link can reset the account. Logging it is acceptable
   only while the logs live on a developer machine, the data is
   synthetic, and no real user accounts exist. The moment email
   exists, the log line must go (see "Switching on" below).
4. **Nothing is lost by waiting.** The token lifecycle (generate →
   hash → store → single-use → expire → confirm → revoke sessions)
   is provider-independent and is already built and tested. Adding
   a sender later touches exactly one call site; it does not
   require reworking auth.

### What switching on requires (provider choice left open)

All of this is future work. None of it is built here.

1. **Provider decision (Naim's call).** Criteria, not a
   recommendation: a transactional-email API or SMTP service with
   a free tier that covers expected volume; a sender domain Naim
   controls; no retention of message content beyond delivery
   needs; reliable delivery to Bangladeshi mailboxes. SMTP and
   API-based providers are both acceptable shapes — the decision
   is which vendor, not which protocol.
2. **Configuration (proposed key names — not yet in
   `app/config.py`, do not use them until the provider lands):**
   `PASSWORD_RESET_MAILER_ENABLED` (bool, default `false` — the
   provider must be off unless explicitly enabled),
   `MAILER_PROVIDER` (vendor name), `MAILER_API_KEY` (secret —
   via environment / secret store only, never committed),
   `MAILER_FROM_ADDRESS` and `MAILER_FROM_NAME` (a real,
   monitored mailbox on the verified domain), plus
   `MAILER_SMTP_HOST` / `MAILER_SMTP_PORT` / `MAILER_SMTP_USER` /
   `MAILER_SMTP_PASSWORD` only if SMTP is the chosen shape.
3. **Code change — one call site, behind a small interface.**
   Add a minimal sender interface (e.g. a `send_password_reset(to,
   link)` function) with a disabled-by-default implementation and
   the chosen provider behind it; `request_password_reset` calls
   it instead of `logger.info`. The interface must be the only
   place that knows about the provider, so swapping providers
   never touches auth logic.
4. **Token handling is unchanged.** Token generation, SHA-256
   storage, single use, 1-hour expiry, confirm-and-revoke-all-
   sessions all stay exactly as they are. Email delivery changes
   how the link travels, not what the link is.
5. **Remove the raw token from logs.** The
   `password_reset_link_issued` log line must stop including
   `reset_link` the moment email exists — log only that a link
   was issued (user id, timestamp), never the link or token.
   Keeping the log line after adding email would leak every reset
   token into the logs.
6. **Rate-limit the request endpoint.** Login is rate-limited
   (5 failures / 15 min / IP+email); the password-reset request
   endpoint currently is not. An emailed flow can be abused to
   spam a victim's inbox, so a per-IP/per-email limit must land
   with the provider.
7. **Build the frontend reset page.** A `/reset-password` route
   that reads `?token=`, asks for a new password (minimum 12
   characters) and calls `POST /password-reset/confirm`. Until
   it exists, emailed links are dead ends.
8. **Template.** Plain-text (plus HTML if the provider needs it):
   the reset link only, the 1-hour expiry stated in plain
   language, what to do if the user did not request it, and no
   password or account details in the message.
9. **Test it for real.** Add a test with a fake sender proving
   the link is emailed and NOT logged (assert the log has no
   `reset_link` field), plus a test that the provider stays
   disabled by default (`PASSWORD_RESET_MAILER_ENABLED=false`
   → no send attempt, link logged as today).
10. **Never ship log-only to production.** If the provider is
    not ready when a deployment is planned, password reset must
    be disabled or the deployment blocked — a logged reset link
    on a shared server is a live account-takeover path.
