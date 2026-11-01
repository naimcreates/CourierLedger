# CourierLedger — Security Test Report (Phase 1, 2026-11-01)

Phase 1 of production-readiness: production security & repository hardening. All counts below
are actual executed results, not claims. Baselines (verified by Ren 2026-11-01, before Phase 1):
backend 316 passed live / 246 passed + 70 skipped SQLite-only, Vitest 50, Playwright 3, P0 47.

# Phase 1.1 (2026-11-01) — Security Closure & Authorization Hardening

Phase 1.1 closed B-F7 (role-scoped RLS), B-F10 (audit trail), B-F11 (session caps /
step-up), B-F15 (revoked-member sessions) and B-F19 (cleanup procedure). All counts
below are actual executed results from the final combined tree, run alone (the full
backend suite must be run alone — concurrent pytest processes against the shared
SQLite test file `/tmp/cl_m1_test.db` produce spurious `no such table` failures,
proven in M8 and re-observed in Phase 1.1). Baselines for this phase (Phase 1
verified results): backend 356 passed live / 275 passed + 81 skipped SQLite-only,
Vitest 50, Playwright 3, P0 47.

## Results (Phase 1.1)

| Suite | Command / conditions | Result |
|---|---|---|
| Backend, SQLite-only | `m1/backend/.venv/bin/python -m pytest tests/ -q` (no `CL_LIVE_PG_PASSWORD`) | **288 passed, 98 skipped, 0 failed** (28 warnings — all pre-existing deprecation warnings, incl. `HTTP_422_UNPROCESSABLE_ENTITY`) |
| Backend, full live, runtime role | `CL_LIVE_PG_PASSWORD=<.env value> CL_LIVE_PG_ROLE=courierledger_runtime .venv/bin/python -m pytest tests/ -q` | **386 passed, 0 failed, 0 skipped** (28 warnings) |
| Backend, new RLS live file only | Same env, `tests/test_rls_roles_live_pg.py` | **11 passed, 0 failed** |
| Backend, new Phase 1.1 live file only | Same env, `tests/test_security_phase11_live_pg.py` | **6 passed, 0 failed** |
| Backend, new Phase 1.1 SQLite file only | `tests/test_security_phase11.py` | **13 passed, 0 failed** |
| Frontend build | `m1/frontend: npm run build` | **Passed** (built in 2.07s; one pre-existing chunk-size warning >500 kB) |
| Frontend unit tests | `m1/frontend: npx vitest run` | **50 passed (8 files), 0 failed** |
| Playwright e2e | `m1/frontend: npx playwright test` (Chromium, 1 worker) | **3 passed, 0 failed** (31.4s) |
| P0 prototype (read-only check) | `prototype-p0/.venv/bin/python -m pytest prototype-p0/tests -q` | **47 passed, 0 failed** — P0 untouched |

### Count reconciliation (Phase 1.1)
- SQLite-only: 275 Phase 1 passed + 13 new Phase 1.1 SQLite tests = 288 passed.
  Skipped: 81 Phase 1 live-PG skips + 17 new Phase 1.1 live tests (11 RLS + 6
  session/audit live) = 98.
- Full live (runtime role): 356 Phase 1 + 13 new SQLite tests (which also run live)
  + 17 new live tests = 386 passed. 288 + 98 = 386 — the two runs agree exactly.
- Two pre-existing live tests were updated for deliberate Phase 1.1 changes (the
  revoke tests now expect a single-workspace member's session to be 401 after revoke
  and re-log-in to continue; the alembic test expects head 0005) and pass. No test
  was removed, skipped, or weakened.
- Vitest 50, Playwright 3, P0 47 are unchanged from Phase 1 — no regressions.
- Both dev DBs verified after: alembic 0005, 33 tables, 46 policies (unchanged
  count — role-scoping changed policies in place), 5 couriers, runtime EXECUTE on
  `app_current_user_role` / `app_current_user_role_effective` = true, PUBLIC = false.

## What the new tests prove (Phase 1.1)

- `m1/backend/tests/test_rls_roles_live_pg.py` (runtime role, 11 tests): helper
  values (NULL no-context/invited/outsider, owner/viewer, PUBLIC cannot EXECUTE /
  runtime can); cross-tenant UPDATE blocked (rowcount 0) + cross-tenant INSERT
  rejected + B row unchanged; viewer/member INSERT rejected, admin INSERT works;
  member/viewer self-promote blocked (rowcount 0); admin role-change works but
  owner-grant raises; owner role changes + transfer-style swap (both orders);
  demotion-first swap with a second owner present; admin cannot manufacture an
  owner snapshot; invited-user smuggle raises, accept-shaped status-only UPDATE
  works, role unchanged; HTTP accept end-to-end; admin revoke + reactivate via
  direct SQL. The Phase 1 B-F7 probe test was rewritten to assert blocking
  (`test_direct_sql_self_promotion_blocked_by_rls_b_f7`).
- `m1/backend/tests/test_security_phase11.py` (SQLite, 13 tests): session cap
  (11th login evicts oldest; old token 401, new works, exactly 10 live); step-up
  transfer flow (aged session → 403; wrong password → 401, still 403; correct →
  200, transfer works); fresh-login + stale step-up (expired verification → 403,
  re-verify → 200); revoke single-workspace (401 everywhere incl. `/auth/me`);
  revoke multi-workspace (session survives for other workspace, revoked one 404s);
  force-revoke endpoint (manager-only, 403/404 cases, target's 2 sessions 401,
  others unaffected); reset-confirm single commit (both prior sessions 401, token
  consumed, new password works); reset-confirm rate limit (50× 400 then 429);
  logout clears CSRF cookie (Max-Age=0 on both cookies); audit identity + member
  events; audit business events (import/run/settlements/settings incl. variance
  −40000, tolerance 0→500, no-change PATCH writes no row); audit detail never
  contains secrets; purge (expired session + expired/used tokens deleted,
  live/revoked-unexpired rows kept).
- `m1/backend/tests/test_security_phase11_live_pg.py` (runtime role, 6 tests):
  revoke single/multi-workspace live; transfer + step-up live (fresh works,
  16-min-old session → 403 via direct-SQL ageing, wrong step-up 401, correct
  step-up → transfer works, last-owner revoke still 422); audit immutability
  (UPDATE and DELETE both raise `InsufficientPrivilege`, row unchanged); audit
  tenant isolation (A's rows visible under A's context, zero under B's); session
  cap live (11 HTTP logins, oldest revoked, 10 live / 1 revoked in SQL).
- Service-layer probe (DB stream, real `member_service` via SQLAlchemy as the
  runtime role): create/invite/accept/change_role (owner+admin)/viewer-403/
  admin-to-owner-422/revoke/re-invite/`transfer_owner` both directions/
  demoted-owner-403 — all passed.

## Intermediate failures in Phase 1.1 — what they were, and why the final numbers stand

- **Audit INSERT … RETURNING blocker (found live, fixed app-side).** The first
  live run after the audit call sites landed 500'd on register: `INSERT INTO
  audit_logs` violated RLS because the ORM emits `INSERT ... RETURNING
  created_at` and PostgreSQL applies the SELECT policy to the returned row,
  which a NULL-merchant row can never pass. Reproduced identically against a
  pristine-HEAD `rls.sql` database, so it was pre-existing, not caused by the
  Phase 1.1 SQL changes. Fixed by setting `created_at` explicitly in
  `record_audit` (plain INSERT, no RETURNING). The full live runs quoted above
  are after this fix.
- **Concurrent-run interference.** A full live run executed while the two
  implementation streams were still editing files showed 44 failed / 200
  passed / 151 errors, dominated by the RETURNING blocker above and by two
  pytest processes sharing `/tmp/cl_m1_test.db`. Two SQLite runs with the same
  cause showed spurious `sqlite3.OperationalError: no such table: users`. All
  were discarded; the numbers above are from clean solo runs of the final
  tree (SQLite 288/98, live 386/0), which reconcile exactly.
- **One test bug in the new RLS file, fixed:** a rollback after an expected
  error also undid an earlier in-transaction admin change — fixed by
  committing the admin change before the grant attempt; re-verified passing.
- **One test bug in the app stream's revoke-test update, fixed:** the first
  re-login attempt used the module's random `PASSWORD` constant instead of
  the `live_pg` default password; fixed to log in with the default password.

---

## Results (Phase 1)


| Suite | Command / conditions | Result |
|---|---|---|
| Backend, SQLite-only | `m1/backend/.venv/bin/python -m pytest tests/ -q` (no `CL_LIVE_PG_PASSWORD`) | **275 passed, 81 skipped, 0 failed** (28 warnings — all pre-existing deprecation warnings, incl. `HTTP_422_UNPROCESSABLE_ENTITY`) |
| Backend, full live, runtime role | `CL_LIVE_PG_PASSWORD=<rotated .env value> CL_LIVE_PG_ROLE=courierledger_runtime .venv/bin/python -m pytest tests/ -q` | **356 passed, 0 failed, 0 skipped** (28 warnings) |
| Backend, Phase 1 live file only | Same env, `tests/test_security_phase1_live_pg.py` | **11 passed, 0 failed** |
| Backend, Phase 1 SQLite file + auth/reconcile | `tests/test_security_phase1.py tests/test_auth.py tests/test_password_reset.py tests/test_api_reconcile.py` | **53 passed, 0 failed** |
| Frontend build | `m1/frontend: npm run build` | **Passed** (built in 1.80s; one pre-existing chunk-size warning >500 kB) |
| Frontend unit tests | `m1/frontend: npx vitest run` | **50 passed (8 files), 0 failed** |
| Playwright e2e | `m1/frontend: npx playwright test` (Chromium, 1 worker) | **3 passed, 0 failed** (46.1s) |
| P0 prototype (read-only check) | `prototype-p0/.venv/bin/python -m pytest prototype-p0/tests -q` | **47 passed, 0 failed** — P0 untouched |

### Count reconciliation
- SQLite-only: 246 baseline passed + 29 new Phase 1 tests = 275 passed. Skipped: 70 baseline
  live-PG skips + 11 new Phase 1 live-file tests (skipped without `CL_LIVE_PG_PASSWORD`) = 81.
- Full live (runtime role): 316 baseline + 11 new Phase 1 live tests + 29 new SQLite tests
  (which also run live) = 356 passed. The two pre-existing live tests that failed on the
  first Phase 1 live run were updated for deliberate Phase 1 changes (the reconcile smoke
  test now sends the CSRF header; the alembic test expects head 0004) and pass.
- Vitest 50, Playwright 3, P0 47 are unchanged from baseline — no regressions.

## What the new tests prove (Phase 1)

- `m1/backend/tests/test_security_phase1.py` (SQLite, 29 tests): logout/revoke-all/reset
  revocation by `revoked_at` (not DELETE); viewer 403 matrix (10 endpoints) + viewer reads
  still 200 + non-member 404; reconcile auth+CSRF (anon 401, authed-no-CSRF 403, authed+CSRF
  200) and strict-model rejections; extra-field → 422 (7 endpoints); StrictInt money +
  overflow → 422; password max length 129 → 422; register rate limit (50×201 then 429);
  reset-request rate limit (10×200 then 429); reset re-request invalidates the prior token;
  login per-account bucket; invite rate limit (10×422 then 429); CSRF missing/wrong → 403;
  CSV row cap (50001 rows), column cap (101 cols), mapping header >500; formula flagging
  (2 flagged, `-500` not flagged, raw values verbatim); upload >10 MB → 413 with nothing
  persisted; `parse_failed` → 422 with batch persisted; list-imports pagination and the
  500-error cap with `truncated_errors`; security headers present in test env; production-mode
  subprocess probes (docs 404, openapi 404, HSTS present; import raises `RuntimeError`
  when `SESSION_COOKIE_SECURE` is false in production).
- `m1/backend/tests/test_security_phase1_live_pg.py` (runtime role, 11 tests): logout →
  200 + cookie cleared + `/me` 401; revoke-all from one of two sessions → both 401;
  reset-confirm → 200, all prior sessions 401, new password works; session expiry → 401;
  viewer POSTs → 403 live; 35 cross-tenant HTTP probes (user B non-member of A's workspace
  → 404 on imports/runs/settlements/shipments/statements/reports/courier-accounts/settings/
  members/accept); CSRF missing/wrong → 403 on 16 POST/PATCH endpoints; direct-SQL RLS
  probes (context A vs B, unset/malformed → zero rows, cross-tenant INSERT rejected,
  append-only UPDATE rejected); B-F12 residual probe (direct-SQL `create_workspace` with
  another user's id still succeeds — see below); B-F17 probe (PUBLIC cannot EXECUTE
  `app_current_*`; runtime role can); B-F7 residual probe (direct-SQL self-promotion
  succeeds at the SQL layer; HTTP layer still refuses it with 403).

## Intermediate failure — B-F12 assertion reverted

The first full live run under the runtime role after the B-F12 fix returned **2 failed,
325 passed, 29 errors**. The 29 errors were fixture errors in `test_triggers_live_pg.py`,
`test_m6a1_live_pg.py`, and `test_idempotency_lease_live_pg.py`: their fixtures call
`create_workspace()` directly as the runtime role without setting `app.user_id`, and the
new in-function assertion raised for every call. Per the task rule (a fix that breaks the
suite is reverted, not accommodated by weakening tests), the assertion was removed and
`create_workspace()` restored to trusting `p_user_id`; B-F12 is recorded as remaining with
a residual probe test. The two failures in the same run were stale expectations from
deliberate Phase 1 changes (reconcile smoke test needed a CSRF header; alembic test
expected head 0002, now 0004) and were updated, not weakened. The re-run after both actions
is the 356-passed result above.

## Environment notes

- **Container replacement mid-task:** the VM/container was replaced during this phase —
  PostgreSQL was down, `psql` was gone, and the cluster was a fresh one (no roles, no
  databases). The cluster was re-provisioned from scratch per `m1/LOCAL_VERIFICATION.md`
  §1–2 (roles + databases, Alembic head 0004, `rls.sql`/`triggers.sql`, `roles.sql`, seed).
  Both dev DBs verified after: alembic 0004, 46 policies, 5 couriers, PUBLIC EXECUTE on
  `app_current_*` = false. During that rebuild a helper echoed a command line containing a
  prefix of the then-current rotated password into an agent tool log, so the password was
  rotated a third time (all three stores) and verified — see `PRODUCTION_SECRETS_GUIDE.md`.
- **PostgreSQL 16** cluster `16 main` on 5432; backend venv `m1/backend/.venv/bin/python`;
  system python3 lacks psycopg (use the venv).
- **Docker cannot run in this VM** (B-2 still open): compose verified by YAML validation +
  inspection only.
- **GitHub Actions is billing-locked:** pushes trigger a failed run (no runner, no steps,
  no logs). No CI run was usable in this phase; the canonical CI workflow was not executed.
  This is recorded, not chased.

## What was NOT run and why

- **GitHub Actions CI** (`ci.yml`, `ci-selfhosted.yml`): billing lock on Naim's account —
  every run fails in ~3 s with no runner and no logs. Not a code failure; cannot be run
  until the billing lock is resolved.
- **Docker build / `docker compose up`:** B-2 — containers cannot start in this VM
  (`runc create failed: setns`). Compose was validated by YAML parsing + inspection only.
- **`npm audit`:** blocked by the egress proxy (A-F9). The OSV fallback from Stream A was
  clean (0 vulns in 254 locked packages) — a fallback, not a full audit. Re-run in CI when
  the billing lock is resolved.
- **Live production-mode header check against a deployed instance:** no deployment exists;
  production-mode behaviour was verified with subprocess probes (docs 404, HSTS present).
- **No live 500 was triggered** (the generic 500 handler is code-verified only, as in
  Stream C).
