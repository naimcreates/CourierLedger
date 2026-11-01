# CourierLedger — Security Remediation (Phase 1, 2026-11-01)

Phase 1 of production-readiness: **production security & repository hardening**. The application
was NOT rebuilt: architecture, V2.2.1 financial logic (the engine was not touched), working
features, and verified tests are preserved. Nothing was deployed. The project is **NOT
marked production-ready** — see "Remaining production blockers" below.

Audit specification: `SECURITY_AUDIT.md` (consolidated Stream A / B / C findings; 57 unique
findings + 5 positives). Fix numbers below are the task's fix numbers (1–30).

## What was fixed in this phase

### Fix 1 — B-F1 (Critical): session revocation by UPDATE, not DELETE
- **Change:** `m1/backend/app/services/auth_service.py` — `revoke_session` /
  `revoke_all_sessions` now do `UPDATE user_sessions SET revoked_at = now()` (revoke-all
  only touches unrevoked rows). `get_user_by_session_token` already checks `revoked_at`; that
  check covers all resolution paths (verified by reading `deps.py` — every authenticated
  request resolves through it).
- **Proving tests:** `tests/test_security_phase1.py` (SQLite): logout revokes + cookie cleared
  + `/me` 401 + row remains with `revoked_at` set; revoke-all revokes both sessions;
  reset-confirm revokes all sessions + new password works; expired session → 401.
  `tests/test_security_phase1_live_pg.py` (runtime role): `test_logout_revokes_session_live`,
  `test_revoke_all_revokes_both_sessions_live`,
  `test_reset_confirm_revokes_all_sessions_live`, `test_expired_session_is_401_live`.

### Fix 2 — B-F2 / C-4.4 (High): raw reset token removed from logging
- **Change:** `auth_service.py` — `request_password_reset` no longer builds or logs the reset
  link; it logs only `password_reset_token_issued` with `user_id`. `logging_config.py`
  docstring corrected: the module formats whatever it is given (no allow-list on extra
  fields), so it must never be handed secrets.
- **Proving tests:** `tests/test_password_reset.py` rewritten — `_issued_token()` helper
  generates a token via `generate_password_reset_token()` and stores its hash via
  `hash_token()` directly in the DB, returning the raw token, never logging it.
  `test_reset_token_stored_hashed_only` asserts the endpoint creates exactly 1 row (SHA-256
  hex, `used_at` None) and no log record carries `reset_link` or `token=`.
  **Remaining:** password reset is still log-only for local dev — no email provider is
  chosen. Never ship log-only reset to production.

### Fix 3 — B-F3 (High): viewer read-only
- **Change:** `m1/backend/app/api/deps.py` — added `WRITE_ROLES = ("owner", "admin", "member")`
  and `require_write_role(membership)` (403 for viewers). Applied in 11 mutating endpoints:
  `courier_accounts.py:create_account`, `imports.py:upload_import`, `runs.py:create_run`,
  `shipments.py:create_order` + `create_shipment`, `settlements.py`: `create_batch`,
  `create_receipt`, `complete_receipt`, `create_allocation`, `reverse_allocation`,
  `calculate_variance`. Non-members still get 404 first; viewers get 403. Member
  invite/role/revoke/transfer and settings PATCH already had owner/admin checks (unchanged).
- **Proving tests:** `tests/test_security_phase1.py::test_viewer_posts_403_matrix` (10
  endpoints, viewer 403 + viewer reads still 200 + non-member 404);
  `tests/test_security_phase1_live_pg.py::test_viewer_posts_403_live` (runtime role).

### Fix 4 — B-F4 / C-1.1 / C-1.2 (Critical/High): `/api/reconcile` authenticated + strict model
- **Change:** `m1/backend/app/api/reconcile.py` rewritten. POST `/api/reconcile` now requires
  `get_current_user` + `require_csrf` (auth checked first: an anonymous caller gets 401, an
  authenticated caller without CSRF gets 403); GET `/api/rules` requires `get_current_user`.
  `shipments: list[dict]` replaced by strict Pydantic models: `ReconcileFeeLine` (fee_type
  max 100, raw str|int|float|None, column_present StrictBool), `ReconcileShipment`
  (extra="forbid"; StrictInt money fields ge=0 le=10**15; `cod_fee_rate_bp`/`vat_rate_bp`
  StrictInt ge=0 le=10000; outcome Literal of the 6 engine outcomes; `cod_fee_base` Literal
  of the 3 bases; `vat_formula` Literal rate_on_net/rate_on_gross; `fee_lines` max 100;
  string caps), `ReconcileRequest` (shipments max_length=1000, `tolerance_poisha` StrictInt
  ge=0 le=10**15). The handler passes `model_dump(exclude_none=True)` dicts to
  `reconcile_batch` — **exclude_none matters**: the engine distinguishes an absent key from
  an explicit `None` in one place (`reported_excluded_tax_poisha` falls back to
  `excluded_tax_poisha` only when absent); passing explicit `None`s broke the engine
  (returned `needs_review` instead of `matched`). The engine itself was NOT changed.
- **Proving tests:** `tests/test_api_reconcile.py` (rewritten to use the authed main-app
  client): matched, open, empty, rules; `tests/test_security_phase1.py`:
  `test_reconcile_requires_auth_and_csrf` (anon 401, authed-no-CSRF 403, authed+CSRF 200) and
  `test_reconcile_strict_model_rejections` (string/bool/float money, unknown enum/outcome/
  field, negative tolerance, overflow, >1000 shipments → 422). Frontend Dashboard demo
  still works — Vitest + Playwright e2e pass (see `SECURITY_TEST_REPORT.md`).

### Fix 5 — A-F1 / A-F2 (High/Medium): password literals removed from the tree + rotation
- **Change:** `.github/workflows/ci.yml` and `.github/workflows/ci-selfhosted.yml` — the
  19-char literal fallback `${{ secrets.CI_POSTGRES_PASSWORD || '<literal>' }}` removed from
  both files; `CI_PG_PASSWORD: ${{ secrets.CI_POSTGRES_PASSWORD }}` with a fail-closed step
  (errors + exits 1 when the secret is empty) in the backend and e2e jobs of both files.
  CREATE/ALTER ROLE statements kept, still interpolating `${CI_PG_PASSWORD}` (not literals),
  now guaranteed to be the secret. **Correction to Stream A:** the claimed "10 ALTER ROLE
  literals" were not literals on re-check — the statements interpolate the env var. The
  real exposure was the fallback literal plus the publicly documented placeholder string
  used as the live password.
  - **Rotation (2026-11-01):** one fresh random password (`secrets.token_urlsafe(32)`,
    43 chars, value never printed or written to a file) set in all three stores: GitHub
    repo secret `CI_POSTGRES_PASSWORD` (`gh secret set`, rc 0); both DB roles
    (`courierledger`, `courierledger_runtime`) reset to it as the postgres superuser;
    `m1/backend/.env` `DATABASE_URL` updated to match (gitignored file — contents never
    printed). Verified via psycopg: both roles authenticate against both DBs with the new
    value; the old documented placeholder value now fails auth for both roles (boolean
    False/False). (First attempt: `gh secret set` succeeded but the role-reset step failed
    with a venv permission error and the in-memory value was lost, so a second fresh value
    was generated and all three stores completed with it.)
  - **Re-rotation (2026-11-01, after a container replacement):** the VM/container was
    replaced mid-task and the PostgreSQL cluster was wiped; it was re-provisioned from
    scratch per `m1/LOCAL_VERIFICATION.md` §1–2. During that rebuild a helper echoed a
    command line containing a prefix of the then-current rotated value into an agent tool
    log, so a **third** fresh random password was generated and set in all three stores
    (GitHub repo secret, both DB roles, `.env`), verified: both roles authenticate
    against both DBs (boolean True/True/True/True). The partially exposed value is now
    invalid; no value was printed in the re-rotation step. See
    `PRODUCTION_SECRETS_GUIDE.md`.
  - Docs scrubbed: the historical 10-char literal replaced with `<your-local-db-password>`
    in 3 test files (5 occurrences); the 19-char fallback literal count is now 0 in both
    workflow files, `m1/LOCAL_VERIFICATION.md`, `m1/IMPLEMENTATION_CHECKLIST.md`; the
    documented placeholder string count is now 0 in `m1/README.md`, `m1/backend/README.md`,
    `m1/IMPLEMENTATION_CHECKLIST.md`, `m1/LOCAL_VERIFICATION.md`, both workflow files.
    Remaining "documented dev placeholder" phrases in `m1/IMPLEMENTATION_CHECKLIST.md:85,122,212`
    are historical narrative (no value).
- **Proving tests / verification:** grep boolean counts (0) for both literal values across
  the tree and the final diff (values never printed); live auth verification above; the
  full live suite re-run under the runtime role with the rotated password
  (see `SECURITY_TEST_REPORT.md`).

### Fix 6 — A-F3 (Medium): self-hosted workflow uses CI-only databases
- **Change:** `.github/workflows/ci-selfhosted.yml` — all DB names changed from
  `courierledger_app`/`courierledger_dev` to dedicated CI-only `courierledger_ci_app` /
  `courierledger_ci_dev` (23 ci_app / 11 ci_dev occurrences, 0 `courierledger_app` left);
  all `ALTER ROLE … WITH PASSWORD` statements removed (0 left) — the workflow never resets
  shared role passwords. Header comment rewritten (secret is a fresh random value rotated
  in Phase 1; the secret must equal the current role password for self-hosted runs).
  GitHub-hosted `ci.yml`: service container password comes from the secret (already the
  shape — just literals removed).
- **Proving tests / verification:** grep counts above; YAML validated OK. (CI itself is
  billing-locked and cannot run — see blockers.)

### Fix 7 — C-1.3 (High): `extra="forbid"` on the six missing models
- **Change:** `RegisterRequest`, `LoginRequest`, `PasswordResetRequest`,
  `PasswordResetConfirm` (`auth.py`), `CreateBatchRequest`, `CreateAllocationRequest`
  (`settlements.py`), `CreateRunRequest` (`runs.py`) all got
  `model_config = ConfigDict(extra="forbid")`. (`ReconcileRequest` covered by fix 4.)
- **Proving tests:** `tests/test_security_phase1.py::test_extra_field_rejected_422`
  (7 endpoints → 422 on an unknown field).

### Fix 8 — C-2.1 (High): CSV import limits
- **Change:** `m1/backend/app/services/csv_import.py` — `MAX_IMPORT_ROWS=50_000`,
  `MAX_IMPORT_COLUMNS=100`, `CSV_FIELD_SIZE_LIMIT=1MB`, `MAX_ERRORS_RETURNED=500`,
  `parse_csv_bytes` rewritten to iterate the reader (never `list(reader)`), enforcing
  row/column caps during iteration, explicit `csv.field_size_limit`. `validate_mapping`
  rejects source headers >500 chars.
- **Proving tests:** `tests/test_security_phase1.py` — CSV row cap (50001 rows →
  `ImportFailedError`), column cap (101 cols), mapping header >500 → `MappingError`.
  **Remaining:** per-request timeout / background-job treatment for large imports
  (C-2.1 remediation) is NOT done — a 50k-row import is still a single transaction.

### Fix 9 — C-2.3 (High): formula-like cell flagging at ingest
- **Change:** `csv_import.py` — `FORMULA_LIKE_PREFIXES = ("=", "+", "-", "@", "|", "%")`
  with a numeric-exception regex (plain signed numbers like `-500` are NOT flagged).
  `_parse_record_fields` appends a `FORMULA_LIKE_VALUE` warning per formula-like cell in
  `import_errors` (same pattern as `VALUE_INVALID`); raw values are preserved verbatim
  (design rule). The `import_errors.error_code` CHECK constraint was widened to include
  `FORMULA_LIKE_VALUE` in `app/db/models.py` plus new Alembic migration
  `0004_phase1_formula_like_error_code.py` (revises 0003); the constraint was NOT removed.
- **Proving tests:** `tests/test_security_phase1.py::test_csv_formula_like_cells_flagged_and_raw_preserved`
  (2 flagged: `=cmd|' /C calc'!A0` and `@evil`; `-500` NOT flagged; raw values preserved
  verbatim). **Remaining:** any future export must neutralise such cells — a requirement
  on the export feature, not a Phase 1 fix (no export exists today).

### Fix 10 — C-4.1 / C-5.3 / C-5.4 (High/Medium): security headers + docs disabled in production
- **Change:** `m1/backend/app/main.py` — security-headers middleware on every response:
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`,
  `Permissions-Policy: camera=(), microphone=(), geolocation=()`, basic CSP
  (`default-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`);
  HSTS `max-age=31536000; includeSubDomains` only in production. Docs disabled in
  production (`docs_url=None, redoc_url=None, openapi_url=None` when
  `ENVIRONMENT=production`).
- **Proving tests:** `tests/test_security_phase1.py` — headers present in test env
  (nosniff/DENY/no-referrer/Permissions-Policy/CSP, no HSTS, `/docs` 200); production-mode
  subprocess probes (docs 404, openapi 404, HSTS present with `SESSION_COOKIE_SECURE=true`;
  import raises `RuntimeError` mentioning `SESSION_COOKIE_SECURE` when false).

### Fix 11 — C-1.5 (Medium): StrictInt on every money field + upper bound
- **Change:** `settlements.py` (`expected_batch_payout_poisha`, `amount_poisha` gt=0,
  `allocated_poisha` gt=0 — all StrictInt le=10**15), `runs.py`
  (`tolerance_poisha` StrictInt ge=0 le=10**15), `shipments.py`
  (`order_cod_poisha` / `amount_to_collect_poisha` le=10**15), `settings.py`
  (tolerance le=10**15), `reconcile.py` (fix 4).
- **Proving tests:** `tests/test_security_phase1.py::test_strictint_money_fields`
  (bool/string money → 422, overflow → 422, settings tolerance 10**16 via PATCH → 422).

### Fix 12 — B-F5 (Medium): rate-limit register and password-reset request; invalidate prior reset tokens
- **Change:** `m1/backend/app/services/rate_limit.py` — generic `is_limited`/`record_attempt`
  scope buckets for register / reset-request / invite; `reset_for_tests()` clears all three
  stores. `m1/backend/app/config.py` — `register_max_attempts=50` / `register_window_seconds=3600`,
  `reset_request_max_attempts=10` / `reset_request_window_seconds=3600`,
  `invite_max_attempts=10` / `invite_window_seconds=3600`. `auth.py` — register rate-limited
  per IP (429 after 50/hour); reset-request rate-limited per IP (429 after 10/hour).
  `auth_service.py` — `request_password_reset` invalidates prior unused tokens via
  `UPDATE … SET used_at=now()`.
  - Note: the register limit is 50/hour per IP, not 10 — 10/hour blocked the live test
    suite itself (many users registered from 127.0.0.1 in one module run) and would block
    small-office NATs. 50/hour still stops mass registration.
- **Proving tests:** `tests/test_security_phase1.py::test_register_rate_limit_per_ip`
  (50×201 then 429), `test_reset_request_rate_limit_per_ip` (10×200 then 429),
  `test_reset_re_request_invalidates_prior_token` (helper token → 400 after endpoint
  request).

### Fix 13 — B-F9 (Medium): enumeration
- **Change:** `auth.py` — register with an existing email returns **200 + generic message**
  `{"detail": "If this email is not already registered, an account has been created. Please
  sign in."}` (no user id in body) instead of 400; 201 + `UserOut` when created.
  `auth_service.py` — module-level dummy Argon2 hash `_DUMMY_PASSWORD_HASH` and dummy verify
  on unknown-user / inactive / no-local-password login paths (timing parity).
  `members.py` — invite rate-limited per workspace (10/hour, 429 after); the residual
  unknown-email 422 oracle is documented as accepted (invite requires owner/admin).
  Frontend `Register.tsx` — after register, if the response has a string `detail` and no
  `id` (the generic 200), navigate to `/login` instead of `/workspaces`.
- **Proving tests:** `tests/test_auth.py::test_register_duplicate_email_generic_response`
  (200 + generic message containing "already registered", no `id` in body, exactly 1 user
  row); `tests/test_security_phase1.py::test_invite_rate_limit` (10×422 then 429);
  `tests/live_pg.py::register` now accepts 200 or 201.

### Fix 14 — B-F13 (Low): production Secure-cookie guard; CSRF names honoured; CSRF rotation at login
- **Change:** `main.py` — refuse to start in production when `SESSION_COOKIE_SECURE` is
  false (mirrors the placeholder-secret guard). `deps.py` — `require_csrf` now uses
  `alias=settings.csrf_cookie_name` / `alias=settings.csrf_header_name` (honoured from
  settings at import time). `auth.py` — login rotates the CSRF token (sets a fresh CSRF
  cookie on success).
- **Proving tests:** `tests/test_security_phase1.py` production-mode subprocess probes
  (import raises `RuntimeError` mentioning `SESSION_COOKIE_SECURE` when false in production;
  docs/HSTS probe with `SESSION_COOKIE_SECURE=true`).

### Fix 15 — B-F14 / C-1.6 (Low/Medium): password max length + duplicate-register race
- **Change:** `config.py` — `password_max_length: int = 128`. `auth.py` — passwords
  `Field(min_length=12, max_length=128)` on Register/Login/PasswordResetConfirm (Login
  password `max_length=128`). `auth_service.py` — `_check_password_length` (min + max) used
  by `register_user` and `confirm_password_reset`; `register_user` catches `IntegrityError`
  on commit → rollback + `EmailTakenError`.
- **Proving tests:** `tests/test_security_phase1.py::test_password_max_length`
  (129 chars → 422 on register/login/reset-confirm).

### Fix 16 — B-F17 (Low): revoke PUBLIC EXECUTE on `app_current_*`
- **Change:** `m1/backend/app/db/roles.sql` — added `REVOKE EXECUTE ON FUNCTION
  public.app_current_merchant_id() FROM PUBLIC;` and the same for `app_current_user_id()`
  before the explicit grants (PostgreSQL grants EXECUTE to PUBLIC by default on new
  functions). Re-provisioned on both dev DBs (`courierledger_app`, `courierledger_dev`).
- **Proving tests:** `tests/test_security_phase1_live_pg.py::test_public_cannot_execute_context_helpers_live`
  (`has_function_privilege('public', …) = false` for both functions; runtime role = true;
  app still works).

### Fix 17 — B-F20 (Low): stale comments
- **Change:** `member_service.py` change_role message ("Owner transfer is a separate endpoint
  (POST /members/transfer-owner, M10)."); `logging_config.py` docstring corrected (no
  allow-list on log fields — see fix 2); `rls.sql` / `triggers.sql` "UNVERIFIED" headers
  replaced with VERIFIED against live PostgreSQL 16 (deferred triggers CL-01/CL-13/CL-16/CL-10
  live-verified in M6/M7).
- **Proving tests:** none (comments only); verified by reading.

### Fix 18 — C-1.7 (Medium): string length caps
- **Change:** `settlements.py` — `receipt_ref_label` max_length=500, `evidence_note`
  max_length=1000; `csv_import.py` — `validate_mapping` rejects source headers >500 chars.
- **Proving tests:** `tests/test_security_phase1.py` — receipt label/note max lengths
  (→ 422), mapping header >500 → `MappingError`.

### Fix 19 — C-1.9 (Low): pagination + error caps
- **Change:** `imports.py` — `list_imports` paginated with `limit` (default 50, le=100) /
  `offset`, returning envelope `{"imports": [...], "total":..., "limit":..., "offset":...}`.
  `csv_import.py` — `get_batch_detail` caps errors at 500 with a `truncated_errors` flag
  and loads statement lines only for the returned rows (previously all lines for the batch).
  Bank-receipts was already paginated (verified in `settlements.py` during Phase 1).
  `GET /api/workspaces` (all memberships) was not changed — a user can only belong to a
  bounded number of workspaces; noted as accepted.
- **Proving tests:** `tests/test_security_phase1.py::test_list_imports_pagination_and_detail_error_cap`
  (total 2, limit/offset pages, limit=101 → 422, `truncated_errors` False; 600 seeded
  errors → 500 returned, `truncated_errors` True). Frontend `listImports` unwraps the new
  envelope and still accepts the old bare-array shape.

### Fix 20 — C-2.2 (Medium): streaming read + stored content type
- **Change:** `imports.py` — upload now stream-reads in 1 MB chunks, aborting with 413 as
  soon as the total exceeds 10 MB (instead of read-then-reject). `csv_import.py` —
  `_normalise_content_type()` stores the declared type only if `text/csv` or
  `application/vnd.ms-excel`, else `"unknown"`; `_find_or_create_file_object` and
  `run_import` take a `content_type` param; `models.py` `ck_file_objects_content_type`
  CHECK widened to include `'unknown'`; new Alembic migration
  `0003_phase1_file_object_content_type.py` (revises 0002).
- **Proving tests:** `tests/test_security_phase1.py::test_upload_over_10mb_rejected_and_nothing_persisted`
  (413, nothing persisted, total 0); content-type stored (`text/csv` → `text/csv`,
  `application/pdf` → `unknown`).
  A non-`text/csv` content-type is recorded as `"unknown"` rather than rejected — browsers
  vary in what they send, so rejection was not chosen; the CSV parse itself remains the
  gate.

### Fix 21 — C-2.4 gap (a): `parse_failed` returns 422, not 201
- **Change:** `imports.py` — `upload_import` returns **422** (JSONResponse) when the batch
  status is `parse_failed` (batch still persisted with its error row).
- **Proving tests:** `tests/test_security_phase1.py::test_parse_failed_returns_422_and_batch_persisted`.
  Kept because the frontend + e2e still pass after the change (see `SECURITY_TEST_REPORT.md`).

### Fix 22 — C-5.1 (Medium/Low): CORS narrowed
- **Change:** `main.py` — `allow_methods=["GET","POST","PATCH","DELETE","OPTIONS"]`,
  `allow_headers=["Content-Type","X-CSRF-Token","Idempotency-Key"]`; single exact origin kept.
- **Proving tests:** frontend + e2e still pass (see `SECURITY_TEST_REPORT.md`).

### Fix 23 — C-7.2 (Low): idempotency-key length cap
- **Change:** `runs.py` — `CreateRunRequest.idempotency_key` max 128; `Idempotency-Key`
  header max_length=128. `imports.py` — form + header idempotency keys max_length=128.
- **Proving tests:** `tests/test_security_phase1.py` — idempotency key 129 chars → 422.

### Fix 24 — A-F5 (Low): random test passwords
- **Change:** `tests/conftest.py` — default test-account password is now random per test
  (`secrets.token_urlsafe(16)`, regenerated in the autouse `_fresh_db` fixture);
  `register`/`login`/`authed_client` default `password=None` → current test password.
  `tests/live_pg.py` — `_LIVE_DEFAULT_PASSWORD = secrets.token_urlsafe(16)`;
  `register`/`login`/`authed_client` defaults use it. `test_runtime_role_live_pg.py` and
  the 7 other live test files — `PASSWORD = secrets.token_urlsafe(16)`.
  `tests/test_auth.py` inline literals kept (test data; the duplicate test needs the same
  password as the first registration).
- **Proving tests:** the suites themselves (no fixed password remains in the harness).

### Fix 25 — A-F7 / C-8.2 (Low/Medium): docker-compose hardened
- **Change:** `m1/docker-compose.yml` — `ENVIRONMENT: ${ENVIRONMENT:-development}`;
  `POSTGRES_PASSWORD` / `SECRET_KEY` / `SESSION_SECRET` / `DATABASE_URL` password use
  `${VAR:?... must be set}` (fail closed, no `CHANGE_ME` default); DB port bound to
  loopback `127.0.0.1:5432:5432`; header comment updated.
- **Proving tests / verification:** YAML validated OK; inspection only — Docker cannot run
  in this VM (B-2 still open). A `compose.prod.yml` override remains to be written.

### Fix 26 — C-8.4 (Low): startup log states whether `.env` was loaded
- **Change:** `main.py` — startup log now includes a `dotenv_loaded` boolean (whether
  `.env` exists in cwd).
- **Proving tests:** none (log line only). `Settings extra="forbid"` was NOT set (too risky
  for deployments with extra vars, per task).

### Fix 27 — C-6.2 (Low): frontend 401/403 handling
- **Change:** `m1/frontend/src/lib/api.ts` — `apiFetch` rewritten with inner
  `doFetch(retried)`: on 401 clears the CSRF token and dispatches a `cl:unauthorized`
  window event, then throws `ApiError` as before; on 403 for a write, clears the CSRF
  token and retries exactly once with a freshly fetched token. `m1/frontend/src/App.tsx`
  — added `UnauthorizedRedirect` component (inside `BrowserRouter`) listening for
  `cl:unauthorized`, clearing the React Query cache and navigating to `/login` (replace).
- **Proving tests:** Vitest + Playwright e2e pass (see `SECURITY_TEST_REPORT.md`).

### Fix 28 — B-F6 (Medium): per-account login bucket
- **Change:** `rate_limit.py` — kept the (IP+email) login bucket; added a per-account
  (email-only) login bucket with the same limits. Documented that `X-Forwarded-For` is NOT
  read; uvicorn needs `--proxy-headers` + trusted `--forwarded-allow-ips` behind a proxy;
  Redis is required for multi-worker production.
- **Proving tests:** `tests/test_security_phase1.py::test_login_per_account_bucket`
  (failures from IP 1.1.1.1 block IP 2.2.2.2 for the same email, not for another email).
  **Remaining:** Redis migration (production requirement).

### Fix 29 — B-F12 (Low): `create_workspace()` — ATTEMPTED AND REVERTED
- **Attempted change:** `rls.sql` — `create_workspace()` asserted
  `p_user_id IS DISTINCT FROM public.app_current_user_id()` → raises, before the
  exists/active check. Assessed provably safe for the application path (single call site
  passes the session user's id; `deps.py` sets `app.user_id` from the same session).
- **Why reverted:** the assertion broke 29 existing live tests (`test_triggers_live_pg.py`,
  `test_m6a1_live_pg.py`, `test_idempotency_lease_live_pg.py`) whose fixtures call the
  function directly as the runtime role without setting `app.user_id` — the full live suite
  under the runtime role went from the 316-passed baseline to 29 fixture errors. Per the
  task rule (a fix that breaks the suite is reverted, not accommodated by weakening tests),
  the assertion was removed and the function restored to trusting `p_user_id`. The control
  is the single call site plus app-layer auth on the endpoint.
- **Kept:** `DROP POLICY IF EXISTS` guards for the three `merchant_memberships` policies
  (they had none, so `rls.sql` could not be re-applied — found during re-provisioning; no
  logic change).
- **Proving tests:** `tests/test_security_phase1_live_pg.py::test_create_workspace_trusts_p_user_id_residual_b_f12`
  pins the residual (direct SQL with another user's id still succeeds; API create still
  works). B-F12 is recorded as **remaining** (see "What remains").

### Fix 30 — A-F6 (Low): placeholder-style example passwords in docs
- **Change:** docs scrubbed — `m1/README.md:84-85` now instructs generating a fresh random
  password stored only in gitignored `.env` and the repo secret; `m1/README.md:186,187,200`,
  `m1/backend/README.md:411,535`, `m1/IMPLEMENTATION_CHECKLIST.md:237-238`,
  `m1/LOCAL_VERIFICATION.md:387,397` updated to the Phase 1 rotation / fail-closed wording;
  `m1/frontend/README.md:228` and `m1/backend/app/db/README.md:405` reworded to the Phase 1
  rotation. Boolean-verify: the documented placeholder string appears nowhere in the
  current tree (count 0).
- **Proving tests / verification:** grep boolean counts (0).

---

## What remains (in order)

### Phase 2 — before any production use
1. **Password-reset email provider** (B-F2/C-4.4 remainder, C-7.1 remainder). Reset is still
   log-only for local dev. Choose a provider, wire it, remove the log-only path entirely.
   Never ship log-only reset to production.
2. **Audit trail** (B-F10). `audit_logs` exists but nothing writes to it. Design and
   implement security-relevant event logging (login, role changes, revocations, financial
   mutations).
3. **Session caps / renewal / step-up** (B-F11). 7-day sessions with no renewal, no
   per-user cap, no step-up for sensitive actions. Consider 24 h + sliding refresh, or at
   least document the 7-day choice.
4. **Role-scoped RLS** (B-F7). The direct-SQL self-promotion probe pins the residual:
   a member who can run SQL as the runtime role can promote their own membership within
   their own workspace. Design role-scoped policies; until then the HTTP layer is the only
   role enforcement.
5. **Redis-backed rate limiting** (B-F6/C-7.1 remainder). In-memory limiters do not survive
   restart and do not share across workers. Also add limits on CSV upload and run creation,
   and a CAPTCHA or email-verification gate on registration.
6. **CSV import background jobs / per-request timeout** (C-2.1 remainder). A 50k-row import
   is still a single transaction in a single-process uvicorn with no timeout.
7. **Object storage for uploaded files** (C-2.5). Files live on the app server's local disk
   under `backend/var/`; move to object storage with per-tenant keys + signed, short-lived,
   membership-checked download URLs. Add a regression test asserting no route serves
   `var/imports`; document that `backend/var/` must never be mounted as static files.
8. **Export neutralisation** (C-2.3 remainder). Any future CSV/Excel export must neutralise
   formula-like cells (prepend `'` or a space) — flagging at ingest is done; the export
   half is a requirement on the export feature.
9. **Expired session/token cleanup** (B-F19). Maintenance procedure (scheduled job) to purge
   expired `user_sessions` / `password_reset_tokens` rows.

### Production deployment checklist (from Stream C §9, not yet done)
10. **Secrets/config:** replace every `CHANGE_ME` via a secret manager; set
    `ENVIRONMENT=production`; set `FRONTEND_URL` to the exact production HTTPS origin;
    set `SESSION_COOKIE_SECURE=true` (now enforced by the startup guard).
11. **Docs/headers:** verified done in code (fix 10); re-verify with a live header check
    against the production deployment.
12. **Deployment artefacts:** separate `compose.prod.yml` (no published DB port, no
    placeholder-password default, `ENVIRONMENT` parameterised); uvicorn `--proxy-headers
    --forwarded-allow-ips` + `TrustedHostMiddleware` when behind a proxy (C-8.3); serve
    the frontend from a hardened static host, not `vite preview` (C-5.5); narrow CORS
    (done in fix 22).
13. **Body-size limit:** no global JSON body-size limit exists yet (C-1.1, C-4.5) — add one
    at the app or reverse-proxy level.
14. **C-4.2 remainder:** audit the two bare `except Exception:` blocks in `run_service.py`;
    add a test that forces a DB error and asserts a generic 500 with no SQL in the body.
15. **C-1.8 / C-3.2 remainder:** prefer `Literal[...]` at the Pydantic layer for
    `role`/`outcome`/`match_status` (consistency); add a lint rule banning
    `dangerouslySetInnerHTML` and a test that a raw value containing
    `<img src=x onerror=alert(1)>` renders as text.

### Not fixing in Phase 1 — documented reasons
- **B-F12** (`create_workspace()` trusts `p_user_id`) — attempted and reverted in Phase 1:
  the in-function assertion broke 29 existing live tests whose fixtures call the function
  directly without setting `app.user_id`. Residual with the single-call-site discipline;
  a future fix must either update those fixtures to set the context or move the check
  into the application call site.
- **B-F7** (RLS role-scoped policies) — design change; probe test added (see above).
- **B-F10** (audit trail) — Phase 2 feature.
- **B-F11** (session caps/step-up) — Phase 2.
- **B-F15** (revoked-member session stays valid) — documented design: a revoked member
  keeps their session but every workspace-scoped call re-checks membership and returns 404.
- **B-F16** (maintenance SELECT all) — keep NOLOGIN, document: the role is NOLOGIN except
  during a controlled maintenance window.
- **B-F18** (identity tables no RLS) — accepted design.
- **B-F19** (expired session/token cleanup) — maintenance procedure for Phase 2.
- **A-F8** (P0 python-multipart) — P0 read-only; "never deploy P0 network-reachable".
- **A-F9** (npm audit blocked) — re-run in CI when the billing lock is resolved.
- **B-F6 Redis migration** — documented as a production requirement (fix 28 did the
  in-memory per-account bucket only).
- **C-2.5** (object storage) — deployment concern, documented above.
- **C-5.5** (vite preview) — deployment reference only.
- **C-8.3** (proxy headers) — documented, no code change: no proxy exists yet.

## Remaining production blockers (summary)

OD-12, OD-16, OD-17 (open design decisions from M1); B-2 Docker (containers cannot start in
this VM — `runc create failed: setns` — compose verified by YAML validation + inspection
only); ADR 1.0 missing (stack authority is still the kickoff brief only); GitHub Actions
billing lock (CI runs fail with no runner, no steps, no logs — not a code failure);
password-reset provider not chosen (log-only reset must never ship); market-validation gates
not run (M13 Day 7 preparation done, no interviews conducted, no gate passed); no
merchant-confirmed courier evidence; the items in "What remains" above (Phase 2 items 1–9
and deployment items 10–15).

**Phase 1 is ready for review** (see `SECURITY_TEST_REPORT.md` for the test evidence), but
the project is **NOT production-ready**.
