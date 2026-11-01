# CourierLedger M1 — Local Verification Procedure

**Status: current as of 2026-11-01 (Milestone 12, CI-independent
development).** GitHub Actions is blocked by an account-level billing
restriction (canonical run 41286668638, attempts 1–2: every job "not
started because your account is locked due to a billing issue", zero
tests run). This document is the reproducible substitute: the exact
commands that prove the same three suites the canonical CI workflow
(`.github/workflows/ci.yml`) runs, executed on this machine instead of
a GitHub-hosted runner. **It is not a CI pass and not a production
readiness claim** — M1 remains a local development build (see
`m1/README.md` §7).

**Container-restart note (2026-11-01, ~16:0x Dhaka):** after the M12
verification below, a service restart replaced the container and
PostgreSQL 16 vanished entirely (no `/usr/lib/postgresql`, no
`/var/lib/postgresql`, no `/etc/postgresql`, nothing on port 5432).
It was restored per §1a (recovery actually used, below) and the
Playwright suite was re-run and passed first try (**3 passed, 26.5s**,
see §5). The backend 316/0/0, Vitest 50 and P0 47 figures in §0 are
Ren's verification from before that restart; the e2e figure is the
post-restore re-verification. The other suites were not re-run after
the restore — treat them as verified in the previous container, not
in the current one, until re-run.

## 0. What this procedure proves — and what it does not

Proves, on this machine, against real PostgreSQL 16:

| Suite | Command | Expected result (2026-11-01) |
|---|---|---|
| Backend pytest, SQLite suite (live modules skip) | `m1/backend`: `.venv/bin/python -m pytest tests/ -q` (no live env) | **246 passed, 70 skipped, 0 failed** |
| Backend pytest, full suite incl. live PostgreSQL (runtime role) | `m1/backend`: `CL_LIVE_PG_PASSWORD=<password> CL_LIVE_PG_ROLE=courierledger_runtime .venv/bin/python -m pytest tests/ -q` | **316 passed, 0 skipped, 0 failed** |
| Frontend production build | `m1/frontend`: `npm run build` | pass (198 modules; pre-existing >500 kB chunk warning only) |
| Frontend Vitest | `m1/frontend`: `npm test` | **50 passed, 0 failed** (8 files) |
| Playwright e2e (real backend + PostgreSQL) | `m1/frontend`: `npx playwright test` | **3 passed, 0 failed** |
| P0 disposable prototype (read-only) | `prototype-p0`: `.venv/bin/python -m pytest tests/ -q` | **47 passed, 0 failed** |

Does NOT prove: Docker image builds or `docker compose up` (containers
cannot start in this VM — see §5); anything on a GitHub-hosted runner
(the canonical workflow has never run); production readiness.

**Rules that are never relaxed for this procedure:**

- No SQLite substitution for PostgreSQL-specific tests. The 70 live
  tests (11 `*_live_pg.py` modules) only run against real PostgreSQL;
  without `CL_LIVE_PG_PASSWORD` they skip, and the skip count is
  reported, never hidden.
- No skipped test is removed, no assertion weakened, no test deleted
  to make a count look better. The full suite is 316 tests
  (`pytest --collect-only`); 246 are SQLite-runnable, 70 are live-only
  (65 gated on `CL_LIVE_PG_PASSWORD` alone, 5 in
  `test_runtime_role_live_pg.py` additionally gated on
  `CL_LIVE_PG_ROLE=courierledger_runtime`).
- Synthetic data only. No real merchant data, no invented courier
  rates or evidence, no V2.2.1 financial-rule change.
- Run the full backend suite alone. Concurrent pytest processes
  against the shared SQLite test file `/tmp/cl_m1_test.db` produce
  spurious failures (`no such table`, `table already exists`) — proven
  in M8, not a defect.

## 1. Environment and dependencies

Verified versions on this machine (2026-11-01):

- Python 3.12.3, Node v24.22.1, npm 10.9.4, git 2.43.0.
- PostgreSQL 16.15 (apt-installed, cluster `16 main`, port 5432).
- Playwright 1.64.1 + Chromium headless shell
  (`~/.cache/ms-playwright/`).
- Docker 29.1.3 + Compose v2.39.0 installed, **daemon not used**:
  containers cannot start in this VM (see §5).

Check before you start:

```bash
python3 --version          # 3.12.x
node --version             # v24.x
pg_lsclusters              # 16 main, port 5432, online
psql --version             # psql (PostgreSQL) 16.15
git -C ~/workspace/goals/courierledger-planning-and-market-validation status --short  # clean, or only intended changes
```

### 1a. If PostgreSQL is gone (container replaced)

This VM's container has been replaced several times; each replacement
loses the PostgreSQL install (no `postgres` user, no cluster, port
5432 closed). Reinstall from cached apt archives and re-provision —
do not treat any earlier live result as still valid until the suite
has been re-run in the current container.

```bash
apt-get install -y postgresql postgresql-contrib
# ENCODING TRAP (found 2026-11-01, M10): a recreated cluster may
# default to SQL_ASCII, which makes psycopg return bytes instead of
# str and breaks Alembic before any table exists. Create it as UTF8:
pg_dropcluster 16 main            # only if a bad cluster exists
pg_createcluster 16 main --start -- --encoding=UTF8 --locale=C.utf8
psql -tAc "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname='postgres';"  # must print UTF8
pg_ctlcluster 16 main start
```

Then provision roles and databases exactly as in `m1/README.md` §3
(§2 below), and re-run the full suite before treating the database
as verified.

**Recovery actually used, 2026-11-01 ~16:0x Dhaka (container
replacement #4):** `apt-get install -y postgresql` failed with
"Unable to locate package postgresql" (an `apt-get update` was still
running in the background holding the lists lock, and the package
lists were stale). The packages were installed from the cached
`.deb`s in `/var/cache/apt/archives/` instead:
`dpkg -i postgresql-client-common postgresql-common
postgresql-client-16 postgresql-16 postgresql-contrib postgresql`
then the missing dependencies from the same cache — `libpq5`,
`libjson-perl`, `ssl-cert`, `libllvm17t64` — then
`dpkg --configure postgresql-16` (it had stayed unconfigured until
`libllvm17t64` was present; `apt-get check` reported it "not
installable" only because the lists were stale). The cluster was
then created UTF8 as above and verified (`UTF8`, PostgreSQL 16.15,
online on 5432). Provisioning ran from a throwaway script in `/tmp`
that read the password from `m1/backend/.env` at runtime (never
printed, temp password file deleted afterwards): roles
`courierledger` / `courierledger_runtime` created (or passwords
reset to the `.env` value), both DBs created, Alembic 0002 +
`rls.sql` / `triggers.sql` / `roles.sql` / seed on both. Verified:
both DBs 33 tables, alembic `0002`, 46 policies, 5 couriers; both
roles authenticate with the `.env` password. The script was deleted
after use.

## 2. Database provisioning (native PostgreSQL, no Docker)

The canonical CI provisions a `postgres:16` service container; here
the same steps run against the VM's local cluster. `<password>` below
is the local-dev password held in the gitignored `m1/backend/.env`
(`DATABASE_URL`); it is the documented dev placeholder from
`m1/README.md` §3. Never print it, never commit it, never reuse it
outside local dev.

```bash
cd ~/workspace/goals/courierledger-planning-and-market-validation/m1/backend

# Roles + databases (as the postgres superuser). The password is set
# from the .env value without printing it:
su postgres -c "psql -c \"CREATE ROLE courierledger LOGIN PASSWORD '<password>';\" \
  -c 'CREATE DATABASE courierledger_dev OWNER courierledger;' \
  -c 'CREATE DATABASE courierledger_app OWNER courierledger;'"
# If the roles already exist, reset their passwords instead:
#   ALTER ROLE courierledger WITH PASSWORD '<password>';
#   ALTER ROLE courierledger_runtime WITH PASSWORD '<password>';

# Schema (Alembic as the DB owner, both DBs — currently migration 0002):
for db in courierledger_app courierledger_dev; do
  MIGRATION_DATABASE_URL="postgresql+psycopg://courierledger:<password>@127.0.0.1:5432/$db" \
    .venv/bin/alembic upgrade head
done

# RLS + triggers as the owner role, per database:
for db in courierledger_app courierledger_dev; do
  PGPASSWORD='<password>' psql -h 127.0.0.1 -U courierledger -d "$db" \
    -v ON_ERROR_STOP=1 -q -f app/db/rls.sql
  PGPASSWORD='<password>' psql -h 127.0.0.1 -U courierledger -d "$db" \
    -v ON_ERROR_STOP=1 -q -f app/db/triggers.sql
done

# roles.sql as the postgres superuser (it creates roles). The workspace
# copy is root-owned mode 640, so copy it somewhere the postgres user
# can read first, then delete the copy:
cp app/db/roles.sql /tmp/roles.sql && chmod 644 /tmp/roles.sql
for db in courierledger_app courierledger_dev; do
  su postgres -c "psql -d $db -v ON_ERROR_STOP=1 -q -f /tmp/roles.sql"
done
rm -f /tmp/roles.sql

# Seed the 5 courier codes (idempotent; no rates or templates):
for db in courierledger_app courierledger_dev; do
  PGPASSWORD='<password>' psql -h 127.0.0.1 -U courierledger -d "$db" \
    -v ON_ERROR_STOP=1 -q -f app/db/seed_couriers.sql
done
```

Verify the provisioned state (either DB):

```bash
PGPASSWORD='<password>' psql -h 127.0.0.1 -U courierledger -d courierledger_app -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public';"   # 33 (32 tables + alembic_version)
PGPASSWORD='<password>' psql -h 127.0.0.1 -U courierledger -d courierledger_app -tAc \
  "SELECT version FROM alembic_version;"                          # 0002
PGPASSWORD='<password>' psql -h 127.0.0.1 -U courierledger -d courierledger_app -tAc \
  "SELECT count(*) FROM pg_policies;"                             # 46
PGPASSWORD='<password>' psql -h 127.0.0.1 -U courierledger -d courierledger_app -tAc \
  "SELECT count(*) FROM couriers;"                                # 5
```

The `rls.sql` / `triggers.sql` runs print `NOTICE: policy/trigger ...
does not exist, skipping` lines — those are the scripts' idempotent
`DROP IF EXISTS` guards on a fresh database, not errors.

### Password drift — read this before you trust a live-test failure

The DB role passwords have drifted from the documented placeholder
more than once (container replacements in M6/M7/M8, and the
self-hosted runner's provisioning in M12, which set both roles to the
workflow's password value). **A live-suite failure with
`FATAL: password authentication failed for user "courierledger"` is
first a password-drift symptom, not an app defect.** Diagnose before
changing any code:

```bash
# Does the .env password still authenticate for both roles?
python3 -c "
import re, subprocess
env = {}
for line in open('m1/backend/.env'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        env[k] = v.strip().strip(chr(39)).strip(chr(34))
pw = re.search(r'://[^:]+:([^@]+)@', env['DATABASE_URL']).group(1)
for role in ('courierledger', 'courierledger_runtime'):
    url = 'postgresql://' + role + ':' + pw + '@127.0.0.1:5432/courierledger_app'
    r = subprocess.run(['psql', url, '-tAc', 'SELECT current_user'],
                       capture_output=True, text=True)
    print(role, '->', 'OK' if r.returncode == 0 else 'FAILED')
"
```

If either role fails, reset both roles to the `.env` password as the
postgres superuser (the reset reads the password from `.env` at
runtime and never prints it; the procedure used on 2026-11-01 is:
copy `.env` to a 644 temp file, run a small psycopg script as the
postgres OS user with `ALTER ROLE ... WITH PASSWORD` via
`psycopg.sql.Literal` — bind parameters are not accepted by
`ALTER ROLE` — then delete the temp copy and the temp package
directory). After a reset, re-run the live suite; do not edit the
application to work around an authentication failure.

## 3. Backend test suite

```bash
cd ~/workspace/goals/courierledger-planning-and-market-validation/m1/backend

# Install dependencies once (venv, as local dev does):
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Step 1 — SQLite suite; live modules skip (must stay green even if
# live PostgreSQL breaks). Expected: 246 passed, 70 skipped, 0 failed.
.venv/bin/python -m pytest tests/ -q

# Step 2 — full suite with live PostgreSQL under the runtime role.
# <password> is the same local-dev password as in §2 (from .env).
# Expected: 316 passed, 0 skipped, 0 failed.
CL_LIVE_PG_PASSWORD='<password>' CL_LIVE_PG_ROLE=courierledger_runtime \
  .venv/bin/python -m pytest tests/ -q
```

What the numbers mean:

- 316 total (`pytest --collect-only`). 246 run without PostgreSQL
  (engine, CSV import, run store, settlement, members, settings,
  shipments, statements, reports, auth, SQL-content checks).
- 70 live-only tests in 11 `*_live_pg.py` modules, each gated by a
  module-level `pytestmark = pytest.mark.skipif(...)`:
  - 65 gated on `CL_LIVE_PG_PASSWORD` only (scratch DB
    `courierledger_live_test`, Alembic, `rls.sql`/`triggers.sql`/
    `roles.sql`/seed, uvicorn on port 8001 — see
    `backend/tests/live_pg.py`). These prove RLS tenant isolation,
    the CL-01/CL-13/CL-16/CL-10 deferred triggers, the
    `clock_timestamp()` lease clock (including the decisive
    `pg_sleep(3)` test), the runtime role, CSV import, run store,
    settlements, members, settings, shipments, workspaces.
  - 5 in `test_runtime_role_live_pg.py`, additionally gated on
    `CL_LIVE_PG_ROLE=courierledger_runtime`.
- Two tests in `test_m6a1_live_pg.py` are pure-engine checks and pass
  even without a database; they are counted in the 70 but do not need
  PostgreSQL.
- Expected warnings only: Starlette/httpx deprecation warnings
  (~25). Any other warning or failure is real — report it exactly,
  do not dismiss it.

## 4. Frontend build and Vitest

```bash
cd ~/workspace/goals/courierledger-planning-and-market-validation/m1/frontend

npm install          # or npm ci for a clean install
npm run build        # expected: pass, 198 modules, ~2s
npm test             # expected: 50 passed, 0 failed, 8 files
```

Notes: the build prints a pre-existing `Some chunks are larger than
500 kB` warning (JS ~581 kB) — recorded since M5, not a failure.
Vitest is scoped to `src/**/*.test.{ts,tsx}`; the Playwright suite in
`e2e/` is excluded from Vitest by design.

## 5. Playwright e2e (real backend + PostgreSQL)

```bash
cd ~/workspace/goals/courierledger-planning-and-market-validation/m1/frontend

npx playwright test        # expected: 3 passed, 0 failed (~40s)
```

Prerequisites and traps:

- PostgreSQL online with `courierledger_app` provisioned (§2).
- `m1/backend/.env` must exist — `playwright.config.ts` reads it
  and passes it to the backend webServer process (runtime-role
  `DATABASE_URL`, `SECRET_KEY`, `SESSION_SECRET`). It is gitignored;
  if it is missing, the config crashes before any test runs. The
  file's `DATABASE_URL` password must match the current role
  password (see "Password drift", §2).
- The config starts both servers itself (uvicorn on 8000, Vite dev
  on 5173) and stops them afterwards; `reuseExistingServer: true`,
  so if servers are already running they are reused. **Stale
  server trap (M7):** a uvicorn started before a router was added
  serves 404s for the new routes while a fresh server fails to
  bind. If settle/run steps fail with 404s, kill any old server on
  port 8000 by PID first (never `pkill -f uvicorn` — it once matched
  the caller's own shell) and re-run.
- The suite (3 tests, Chromium): full happy path register → workspace
  → order + shipment → Steadfast account → fixture upload (12 rows /
  10 parsed / 6 error rows) → Fixture Alpha run (completed, 1 result
  `needs_review`) → settlement batch → receipt ৳600 → complete →
  allocate in full → calculate variance −40000 → reverse; run-detail
  page test; unauthenticated redirect test. Each run registers a
  fresh timestamped user, so re-runs never collide. It creates real
  rows in `courierledger_app` — that is the local dev database, by
  design.
- Known flakiness (M10/M11): one transient failure then pass on
  retry was observed twice (first Playwright run 1 failure, passed on
  retry; 14 repeat executions, 0 failures; mechanism unproven, best
  hypothesis a transient environment glitch ~1–3 s after
  settlement-batch creation, before the receipt POST). On any
  failure: copy `frontend/test-results/` aside before re-running,
  and report exactly what happened including any retry — do not
  silently re-run until green.
- **Selector fix (2026-11-01, M12):** Ren's first independent e2e run
  failed with a strict-mode violation — the loose heading name
  `'Settlement batch'` matched both `'Settlement Batches'` (h1) and
  `'Create a settlement batch'` (h2); the batch detail page itself
  had loaded correctly. Fixed in
  `frontend/e2e/happy-path.spec.ts` (~line 222) with
  `getByRole('heading', { name: 'Settlement batch', exact: true })`
  plus a comment. After the container-restart PostgreSQL restore
  the suite passed **first try: 3 passed, 0 failed, 26.5s** — the
  selector was the only change; no app code was touched.
- No browser visit is needed to run the suite; Chromium headless
  shell is already installed (`npx playwright install chromium` if
  `~/.cache/ms-playwright/` is ever wiped).

## 6. How this differs from canonical CI

| Aspect | Canonical CI (`.github/workflows/ci.yml`) | This procedure |
|---|---|---|
| Runner | `ubuntu-latest` GitHub-hosted | This VM |
| PostgreSQL | `postgres:16` service container | Local apt cluster, port 5432 (§2) |
| Python / Node setup | `setup-python` / `setup-node` actions | System Python 3.12 / Node 24 already installed |
| Dependencies | Fresh venv + `npm ci` per run | Existing `.venv` / `node_modules`, refresh with the same commands |
| Live-test `su postgres` shim | Created `postgres` OS user + `sudo -E` pytest | Not needed — a real `postgres` OS user and local cluster exist |
| Test commands | Identical pytest / build / Vitest / Playwright invocations | Same commands, same expected counts |
| Secrets | `CI_POSTGRES_PASSWORD` repo secret (fallback literal placeholder) | Local `.env` password; never printed or committed |

The self-hosted variant (`.github/workflows/ci-selfhosted.yml`) is a
mirror of canonical CI for the `courierledger-vm` runner; it is
**parked** (see §7) and is not part of this procedure.

## 7. Self-hosted runner — parked, not deleted

The 2026-11-01 self-hosted attempt is recorded here so it is not
re-tried blindly:

- Runner `courierledger-vm` registered, ran three runs
  (41288531890, 41290116524, 41292188799), then went offline (its
  process is gone); registration removed 2026-11-01 (no runner
  exists now). The workflow file stays as the tested record.
- Results: run 41288531890 and 41290116524 — backend and e2e jobs
  failed at provisioning with a shell syntax error in the
  provisioning heredoc (a redacted copy had been pasted; fixed in
  commit 3e82c81). Run 41292188799 — frontend success; e2e FAILED
  because "Set up Node 24" never completed (30-minute job timeout;
  Node download / npm-cache restore stalled through the egress
  proxy); backend job never started (runner went offline).
- Two defects the attempt exposed, both fixed on 2026-11-01:
  1. **A real password was committed** in `ci-selfhosted.yml`
     (`CI_PG_PASSWORD` as a literal). Removed; the workflow now
     uses `${{ secrets.CI_POSTGRES_PASSWORD || 'ci-only-placeholder' }}`,
     matching canonical CI. The value remains in git history
     (commits 6c6c03b–3e82c81) and cannot be removed without a
     history rewrite, which is not done; it only ever guarded
     synthetic-data databases on this VM, and both roles have been
     reset to the `.env` password since.
  2. **Password drift:** the runner's provisioning reset the shared
     roles' passwords and dropped/recreated `courierledger_app` /
     `courierledger_dev`, breaking local `.env` authentication. The
     repo secret `CI_POSTGRES_PASSWORD` is now set to the documented
     local-dev placeholder value, so any future self-hosted run
     resets the roles to the same password local dev uses.
- To revive a runner: download the actions-runner release for
  linux-x64, get a registration token
  (`POST /repos/naimcreates/CourierLedger/actions/runners/registration-token`),
  `./config.sh --url https://github.com/naimcreates/CourierLedger
  --token <token> --labels self-hosted,linux --unattended`, then
  `./run.sh` with `RUNNER_ALLOW_RUNASROOT=1` (the runner runs as
  root on this VM). Keep the runner directory outside the repo
  (`~/actions-runner` was used). Expect slow setup steps through the
  egress proxy (pip/npm/cache restores time out or retry).

## 8. Docker — status on this VM (B-2, unchanged)

Docker 29.1.3 + Compose v2.39.0 are installed; the daemon starts
only with `--iptables=false --bridge=none --storage-driver=vfs`
(the VM lacks NET_ADMIN; overlayfs fails). Re-verified 2026-11-01:
the daemon starts, but **no images are cached** (the container was
replaced since M10) and the daemon's own pulls fail with a TLS
handshake timeout / unexpected EOF against `registry-1.docker.io`
through the egress proxy, while shell `curl` reaches the registry.
Containers cannot start in this VM
(`runc create failed: ... setns: operation not permitted`, M10
evidence) — so `docker build` and `docker compose up` have never
run here. To finish B-2, use a machine that can run containers:
`docker build` both images, `docker compose up -d db`, provision
the containerised DB (Alembic as owner, `rls.sql` / `triggers.sql` /
`roles.sql`, runtime-role password), `docker compose up backend`,
then the health + workspace + reconcile smoke through the container.
Do not claim any of that ran here.

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `psql: connection refused` / no cluster | Container replaced; PostgreSQL gone | §1a reinstall + §2 provision + full re-run |
| `password authentication failed for user "courierledger"` | Password drift (§2) | Reset roles to the `.env` password; re-run |
| `TypeError: cannot use a string pattern on a bytes-like object` in Alembic | Cluster created with SQL_ASCII encoding | Recreate cluster with `--encoding=UTF8 --locale=C.utf8` (§1a) |
| Backend suite: `no such table` / `table already exists` | Two pytest processes sharing `/tmp/cl_m1_test.db` | Run the suite alone; never run two pytest processes at once |
| Live tests: `InvalidURL` from httpx | Proxy env vars (bracketed IPv6 in `no_proxy`) | Already handled: live clients use `trust_env=False` |
| E2E: 404s on settlement/run routes | Stale uvicorn from before the router existed | Kill the old server by PID, re-run (§5) |
| E2E: single transient failure, passes on retry | Known unproven flakiness (M10/M11) | Preserve `frontend/test-results/`, report the retry honestly |
| `npm ci` / pip install retries or timeouts | Egress proxy slowness | Retry; `npm install` / pip with the existing cache usually succeeds on the second attempt |
| `gh run` shows jobs failing in ~3 s with no logs | GitHub account billing lock (canonical CI only) | Not a code failure; resolve billing on the GitHub account, then re-run. Do not change code to work around it. |
