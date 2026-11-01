# CourierLedger — Infrastructure Test Report (Phase 2)

**Date:** 2026-11-01. **Scope:** Phase 2 — Production Database &
Infrastructure Hardening. All tests below were run on this VM
against real PostgreSQL 16.15, unless a row says otherwise.
**SQLite was never substituted for PostgreSQL-specific
verification.** P0 is read-only and was not modified.

---

## Phase 3 (2026-11-01/02) — Production Operations, Recovery & Deployment Readiness

All Phase 3 tests were run on this VM against real PostgreSQL 16.15
(cluster `16 main`, both working DBs at Alembic head 0007), unless a row
says otherwise. SQLite was never substituted for PostgreSQL-specific
verification. P0 is read-only and was not modified.

### Full suites (Phase 3, coordinator's independent run)

| Suite | Result | Notes |
|---|---|---|
| Backend pytest, full live suite under `courierledger_runtime` (`CL_LIVE_PG_ROLE=courierledger_runtime`, password from gitignored `.env` at runtime) | **439 passed, 4 skipped, 0 failed** in 270.05s (28 warnings, Starlette/httpx deprecation only) | Phase 2 baseline 410 + 33 new tests = 443 total; 439 + 4 = 443, reconciled exactly. The 4 skipped tests are the new maintenance live tests (`test_maintenance_live_pg.py`) — they skip by design in the runtime-role run because the purge needs DELETE on `user_sessions`/`password_reset_tokens`, which no application role holds; run separately with `CL_LIVE_PG_ROLE=courierledger` (owner password from `.env`) they pass: **4 passed**. A second full run confirmed 439/4/0 (357.45s). |
| Frontend build (`npm run build`) | **pass** — built in 2.92s | Pre-existing >500 kB chunk-size warning only |
| Frontend Vitest (`npm test`) | **50 passed, 0 failed** (8 files) | Unchanged from Phase 2 |
| Playwright e2e (`npx playwright test`) | **3 passed** (48.6s, first try) | Unchanged from Phase 2 |
| P0 prototype (`prototype-p0`, full suite) | **47 passed, 0 failed** (1 unrelated Starlette/httpx deprecation warning) | Unchanged; P0 files unmodified |

### New tests added in Phase 3 (all pass)

| File | Tests | Result |
|---|---|---|
| `m1/backend/tests/test_maintenance_live_pg.py` (new) | 4 live (maintenance job: deletes only expired sessions/tokens, idempotent second run, dry-run counts, advisory-lock skip; audit row survives) | **4 passed** (owner-role run; skipped by design in the runtime-role run — see above) |
| `m1/backend/tests/test_migration_phase3_live_pg.py` (new) | 12 live (drift allow-list pin, migrator-role probes, migration A–D + (5)/(6)) | **12 passed** (as part of the 439) |
| `m1/backend/tests/test_production_config.py` (new) | 15 (8 validator unit, 5 subprocess import, 1 DB-down readiness, 1 live production-mode) | **15 passed** (as part of the 439) |
| `m1/backend/tests/test_migration_sql.py` (+2) | 2 new static tests | **2 passed** (as part of the 439) |

Two pre-existing tests were updated for deliberate Phase 3 changes (head
assertion 0006→0007 in `test_infrastructure_phase2_live_pg.py` and
`test_runtime_role_live_pg.py` — both files say the assertion "must move
with the next migration"; behaviour under test unchanged). One Phase 1
fixture was changed (`test_security_phase1.py` `_prod_subprocess` now uses
a dead-port `postgresql://` URL + explicit
`FRONTEND_URL=https://app.example.invalid` because production validation
refuses SQLite and the dev-default origin; assertions unchanged). No test
removed, skipped, or weakened.

### Migration tests (Phase 3, real PostgreSQL 16.15)

| Test | Result |
|---|---|
| A — empty database → `alembic upgrade head` | **PASS** — fresh DB via `provision_db.sh` → 0007 |
| B — existing DB at 0006 → 0007 | **PASS** — both working DBs upgraded as the migrator |
| C — repeated migration | **PASS** — `upgrade head` re-run on app + dev, exit 0; second `provision_db.sh` run identical counts |
| D — fresh-cluster provisioning | **PASS** — scratch DB head 0007, 33 tables, 46 policies, 15 functions, 22 triggers, 5 couriers |
| (5) — RLS, triggers, indexes, constraints after correction | **PASS** — 33/46/15/22, 133 indexes, 265 constraints, FORCE 24 |
| (6) — no schema change from ordinary app startup | **PASS** — `alembic check` identical before vs after a production-mode run |
| `alembic check` after migration | **Exits 255 by design** — residual is the pinned 13-op allow-list (12 FK representation + 1 RLS-layer index); no differences suppressed |

### Backup and restore tests (Phase 3, real runs)

| Check | Result |
|---|---|
| Encrypted backup (`backup_encrypted.sh`, Alembic 0006, real synthetic rows) | **PASS** — 263,286 bytes encrypted (plaintext 263,101), globals 2,015 bytes, ~2 s; sidecar SHA-256 verified, decrypt + `pg_restore -l` verified |
| Restore drill into a separate DB (`verify_restore.sh`) | **PASS** — ~5 s, exit 0; object counts identical, row counts identical (8 users, 8 reconciliation_results, 4 variance calcs, 56 audit_logs); V_total 0, V_cond 0, variance-calc 0 violations; RLS probe 0 merchants; scratch DB dropped |
| Encrypted backup after rebuild (Alembic 0007, empty DB) | **PASS** — 226,294 bytes; same checks pass (identities vacuous, no business rows) |
| Retention (keep 7 / 30 days) | **PASS** — 10 fake old sets + 1 real run → exactly 7 sets remained; same-minute re-run refused by the collision guard |
| Globals restore | **NOT RUN** — artifact created + checksummed only |

### Maintenance-job tests (Phase 3, live)

Seeded 2 expired + 1 live session and expired + used + live tokens on
`courierledger_dev`: dry-run 2/2, real run completed 2/2, second run 0/0
(idempotent), multi-DB run 0/0 both, unknown DB → JSON error, exit 2;
only live rows remained. Systemd timers enabled (backup daily 02:30 UTC,
maintenance hourly) and proved via `systemctl start` (`Result=success`).

### Test-run notes (Phase 3)

- A container replacement mid-task wiped PostgreSQL; the cluster was
  rebuilt (apt install, UTF8, both DBs re-provisioned at Alembic 0007)
  and every live proof was re-run on the rebuilt cluster. One encrypted
  backup was lost with its passphrase (it lived in `/etc`); the evidence
  chain was re-run after the rebuild with the passphrase under
  `~/.config`.
- Stream A reported `test_security_phase11.py` showing failures in the
  dirty tree vs its clean-baseline run. The coordinator's full-suite run
  is green (439/4/0), so those were artefacts of running test files
  standalone and/or concurrently with other streams' pytest processes:
  two concurrent pytest processes in this backend directory share the
  fixed `/tmp/cl_m1_test.db` SQLite file and one process's `_fresh_db`
  fixture drops tables mid-test in the other (observed 12-failure run
  with `no such table: user_sessions`). Full suites must be run with no
  other pytest process active. A pre-existing flake also stands: running
  two live-PG modules in one pytest invocation errors the first test of
  whichever module runs second at fixture setup (shared scratch DB name
  + port 8001 in `live_pg.py`); each file passes alone, and the full
  suite passes — recorded, not fixed in Phase 3.
- **Not run:** GitHub Actions CI (billing lock); Docker build /
  `docker compose up` (B-2 — no Docker binary on this VM); `npm audit`
  (egress blocked).

---

## 1. Full suites (actual results)

| Suite | Result | Notes |
|---|---|---|
| Backend pytest, full live suite under `courierledger_runtime` (`CL_LIVE_PG_ROLE=courierledger_runtime`, password from gitignored `.env` at runtime) | **410 passed, 0 failed** in 260.68s (28 warnings, Starlette/httpx deprecation only) | Phase 1.1 baseline 386 + 4 new live tests + 20 new unit tests (storage 11 + observability 9) = 410, reconciled exactly. One intermediate run showed 409 passed / 1 failed: the pre-existing `test_alembic_uses_migration_database_url` asserted head `0005`; the test's own comment says the assertion "must move with the next migration" — updated to `0006` (behaviour under test unchanged) and the re-run is green. |
| Frontend build (`npm run build`) | **pass** — built in 4.98s | Pre-existing >500 kB chunk-size warning only |
| Frontend Vitest (`npm test`) | **50 passed, 0 failed** (8 files) | Unchanged from Phase 1.1 |
| Playwright e2e (`npx playwright test`) | **3 passed** (40.2s, first try) | Unchanged from Phase 1.1 |
| P0 prototype (`prototype-p0`, full suite) | **47 passed, 0 failed** (1 unrelated Starlette/httpx deprecation warning) | Unchanged; P0 files unmodified |

## 2. New tests added in Phase 2 (all pass)

| File | Tests | Result |
|---|---|---|
| `m1/backend/tests/test_storage_backend.py` | 11 (protocol, local backend: layout/key format, tenant isolation, path-traversal refusal, 10 MB cap, SHA-256 verification, swap point) | **11 passed** |
| `m1/backend/tests/test_observability.py` | 9 (request-ID middleware: generation, echo validation, header return, contextvar reset; `request_id` in JSON logs; `/ready` 200/503 semantics) | **9 passed** |
| `m1/backend/tests/test_infrastructure_phase2_live_pg.py` | 4 (alembic head is 0006; the three 0006 indexes exist; runtime-role timeout settings in `pg_roles.rolconfig`; `GET /api/ready` returns 200 with `database_ok: true` and an `X-Request-ID` header) | **4 passed** |
| Regression run at the time (72 + 29 + 20 = 121) | — | **121 passed, 0 failed** |

Two test bugs were found and fixed during Phase 2 (not weakened):
the live test initially passed a SQLAlchemy URL
(`postgresql+psycopg://...`) to `psycopg.connect()`, which rejects
it — fixed to keyword connection parameters (which also keeps the
password out of error messages); and the `/ready` test initially
built its own `httpx.Client` instead of using the fixture's yielded
client — fixed to the established pattern. Both fixes were to the
test, not to the application.

## 3. Migration verification (real PostgreSQL)

| Test | Result |
|---|---|
| A — empty database → `alembic upgrade head` | **PASS** — head 0006 in 1.17 s (re-run in Phase 2; Stream B measured 2.25 s at 0005). Alembic-only DB: 33 tables, 0 policies, 0 functions, 0 triggers, 131 indexes, 259 constraints. **Gap:** Alembic alone does not produce a working database (0 policies/functions/triggers/seed rows; runtime role gets permission denied; `create_workspace()` missing) — provisioning needs the full script (see `MIGRATION_VERIFICATION.md`) |
| B — existing DB at 0005 → latest | **PASS** — both working DBs at **0006 (head)** with all 3 new indexes |
| C — repeated migration → safe no-op | **PASS** — clean no-op on scratch and both working DBs |
| D — fresh cluster provisioning in dependency order | **PASS** — `provision_db.sh` on scratch DB `courierledger_phase2_test`: head 0006, counts identical to working DBs; second run a clean no-op; scratch DB dropped afterwards. Wrong orders both fail: `roles.sql` first → `function public.app_current_merchant_id() does not exist` (exit 3); `rls.sql` first → `role "courierledger_maintenance" does not exist` |
| `alembic check` | **FAILS (exit 255, 17–18 ops)** on a correctly migrated DB — representation drift only (6 FKs NO ACTION vs RESTRICT, unique-index vs UniqueConstraint, one missing-but-harmless `uq_memberships_merchant_id`, one RLS-layer index). Decision open; see `MIGRATION_VERIFICATION.md` §3 |
| 0002 downgrade hazard | **Proven at CHECK level** — re-adding 0001's CHECK fails on a row shape (S non-NULL, V_R NULL, V_cond non-NULL). No such rows exist in `courierledger_app` (0 of 4). Production stance: no downgrades; restore from backup |

## 4. Role and privilege verification (live queries)

Full table in `DATABASE_OPERATIONS.md` §3. Headlines:

- `courierledger` owns all 33 tables in both DBs; `courierledger_runtime`
  owns nothing; `courierledger_migrator` 0 grants, 0 objects;
  `courierledger_maintenance` SELECT all 33 tables + DELETE only on
  `idempotency_keys`; only `postgres` is superuser; `pg_default_acl`
  empty (fail-closed).
- Negative probes as the runtime role, all as designed: no context →
  0 rows; tenant A sees only A's 1 order; cross-tenant INSERT →
  42501; UPDATE `audit_logs` / DELETE `idempotency_keys` / UPDATE
  `merchants` → 42501; UPDATE of a non-granted
  `reconciliation_runs` column → 42501; sanctioned status UPDATE
  executes (0 rows matched). Probes ran in a rolled-back transaction;
  row counts re-verified unchanged afterwards.
- Per-role timeouts verified in `pg_roles.rolconfig`: runtime
  30s/60s/5s, maintenance 10min/10min/30s.

## 5. Backup and restore (actually performed)

| Step | Result |
|---|---|
| `backup_db.sh courierledger_app` | `pg_dump -Fc`, 246,370 bytes, SHA-256 `3c8620522fc526f870960e80052ad8a311760d0c2c243c048784cb9732a5eac6`, < 1 s. (Owner-role dump fails under FORCE RLS — backup must bypass RLS; see `DATABASE_BACKUP_RECOVERY.md` §2.) |
| `verify_restore.sh` into a fresh separate DB | **PASS** — object counts identical (33/46/15/22/132/264, RLS 24/26, alembic 0006); per-table row counts `diff` empty (244 rows); V_total violations 0, V_cond violations 0, variance-calc violations 0; runtime-role RLS probe on the restored DB correct (no context → 0 merchants). Restore DB dropped on success. |
| Proof dump files | Deleted after verification (dumps are credential-bearing). `m1/backups/` removed. |

Caveats: 244 rows is tiny — durations prove the procedure, not
production-scale timing. No schedule, off-host copy, WAL archiving,
globals backup, or recurring drill exists yet.

## 6. Docker / Compose (NOT verified — exact blocker)

- `docker` binary is **not present** in this VM (removed by a
  container replacement); before that it was 29.1.3 and containers
  could not start (`runc create failed: setns: operation not
  permitted`). `docker compose up` has **never run** here.
- Phase 2 verified the compose file and both Dockerfiles by Python
  YAML parse + structural assertions only. Edits: backend port
  loopback-bound, frontend Dockerfile runs as `USER node` +
  `HEALTHCHECK`, compose header documents the blocker + the native
  fallback. **No Docker verification is claimed.**
- Native PostgreSQL (apt, cluster `16 main`, online, UTF8/C.UTF-8)
  is the verified local path.

## 7. Storage and observability (new, tested)

- **Storage abstraction:** `StorageBackend` protocol +
  `LocalFilesystemBackend` (`app/services/storage.py`);
  `get_storage_backend()` is the single swap point; `csv_import.py`
  stores via the backend. 11 tests pass. Honest label: local
  filesystem is **not** production object storage.
- **Observability:** request-ID middleware + `request_id` in every
  JSON log line + `request_completed` per request + `/api/ready`
  (200/503) distinct from `/api/health`. 9 tests pass. Log-leak grep
  clean (reset-token logging records only `user_id`).
- **Not built:** hosted error reporting (founder decision), metrics
  endpoint, distributed tracing. **Not enabled:**
  `pg_stat_statements` (needs restart + extension).

## 8. What was NOT tested (stated, not hidden)

- **GitHub Actions CI:** billing-locked; zero tests have ever run
  there. Both workflows were fixed for the provisioning trap
  (NOLOGIN roles pre-created before `rls.sql`) and YAML-validated,
  but **never executed**.
- **Docker containers:** cannot run in this VM (see §6).
- **Production PostgreSQL config values** (shared_buffers 2 GB etc.):
  recommendations only; cannot be verified on the dev cluster.
- **Load/performance testing:** none performed. No slow-query
  measurement (no `pg_stat_statements`).
- **Restore at production scale:** proven at 244 rows only.
- **Downgrade on a populated database:** not tested (by design —
  downgrades are not a production recovery path).

## 9. Tests not removed, skipped, or weakened

No test was removed, skipped, or weakened in Phase 2. Two new test
files' bugs were fixed in the tests themselves (§2). The full live
suite runs with 0 skips under the runtime role when the password is
supplied (the 70-skip figure from M11 was the no-password mode).
