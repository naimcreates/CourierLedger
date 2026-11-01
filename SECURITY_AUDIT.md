# CourierLedger — Security Audit (consolidated)

**Scope:** CourierLedger M1, repo `~/workspace/goals/courierledger-planning-and-market-validation/`,
branch `main`, HEAD `637ddf1` (Milestone 13) at audit time. Backend `m1/backend` (FastAPI),
frontend `m1/frontend` (React/TypeScript/Vite/Tailwind), CSV import, workflows, compose.
The disposable P0 prototype (`prototype-p0/`) is read-only and was not re-audited except for its
known dependency vulnerabilities (A-F8).

**Method:** three read-only audit streams, consolidated here (2026-11-01):
- **Stream A** — secrets, git history, dependencies.
- **Stream B** — auth, sessions, RLS, authorisation.
- **Stream C** — frontend & API security, input handling, configuration
  (full report: `m1/security-audit-stream-c.md` — deleted after incorporation; this file is
  its permanent home).

**Severity key:** Critical = exploitable now, unauthenticated, data or integrity impact.
High = exploitable with a logged-in account or realistic abuse, or a missing control production
must have. Medium = real weakness needing a fix before production, lower exploitability or impact.
Low = hardening / defence-in-depth.

**Counting rule:** three findings were the same issue reported by two streams and are counted
once, under the first ID listed: B-F4 = C-1.1 (unauthenticated `/api/reconcile`), B-F2 = C-4.4
(reset token in the log), C-4.1 = C-5.3 (missing security headers). Overlapping but distinct
findings (e.g. B-F5 vs C-7.1 rate limiting, B-F13 vs C-5.2/C-6.3 cookie/CSRF) are kept as
separate entries and cross-referenced.

## Severity counts (unique findings, after dedupe)

| Severity | Count | Finding IDs |
|---|---|---|
| Critical | 2 | B-F1, C-1.1 / B-F4 |
| High | 8 | B-F2 / C-4.4, B-F3, C-1.2, C-1.3, C-2.1, C-2.3, C-4.1 / C-5.3, A-F1 |
| Medium | 21 | B-F5, B-F6, B-F7, B-F8, B-F9, B-F10, B-F11, B-F15, C-1.5, C-1.6, C-1.7, C-2.2, C-4.2, C-5.1, C-5.2, C-7.1, C-8.2, C-8.3, A-F2, A-F3, A-F8 |
| Low | 26 | B-F12, B-F13, B-F14, B-F16, B-F17, B-F18, B-F19, B-F20, C-1.8, C-1.9, C-1.10, C-3.2, C-3.3, C-4.3, C-4.5, C-5.4, C-5.5, C-6.2, C-6.3, C-7.2, C-8.4, C-8.5, A-F5, A-F6, A-F7, A-F9 |
| Positive (no finding) | 5 | C-1.4, C-2.4, C-2.5, C-3.1, C-6.1 |

57 unique findings + 5 positive findings. Per-stream raw counts: Stream B — Critical 1, High 3,
Medium 8, Low 8 (20 findings). Stream C — Critical 1, High 5, Medium 11, Low 14, Positive 5
(31 findings + 5 positives). Stream A — High 1, Medium 3, Low 4 (8 findings).

**Remediation status (Phase 1, 2026-11-01):** see `SECURITY_REMEDIATION.md` for what was fixed
in this phase and what remains. This audit file records the findings as found; it is not
updated to "fixed" — the remediation file is the status record.

**Remediation status (Phase 1.1, 2026-11-01):** B-F7, B-F10, B-F11, B-F15 and B-F19
were closed in Phase 1.1 (see the "Verification status" line under each finding, and
`SECURITY_DECISIONS.md` for the decision record). The findings themselves are recorded
as found; only the status lines were updated.

---

## Critical

### B-F1 — CRITICAL — Session revocation uses DELETE, which the runtime role has no grant for
- **Component:** `m1/backend/app/services/auth_service.py:132-152` (`revoke_session`,
  `revoke_all_sessions`); `m1/backend/app/db/roles.sql` (runtime grants SELECT/INSERT/UPDATE only
  on `user_sessions`).
- **Evidence:** logout / revoke-all / password-reset revocation executed
  `DELETE FROM user_sessions`. The runtime role has no DELETE grant, so logout returned 500,
  the session cookie was not cleared, and the session stayed valid. Password reset changed
  the password and then failed revocation, leaving attacker sessions valid.
- **Impact:** a stolen session cannot be revoked by the user; logout is broken under the
  runtime role.
- **Remediation:** revoke by `UPDATE user_sessions SET revoked_at = now()`; the `revoked_at`
  check in `get_user_by_session_token` already existed.
- **Verification status:** fixed in Phase 1 — see `SECURITY_REMEDIATION.md` (fix 1).
  Live-verified under the runtime role (`tests/test_security_phase1_live_pg.py`).

### C-1.1 / B-F4 — CRITICAL — `POST /api/reconcile` is unauthenticated and accepts an unbounded `list[dict]`
- **Component:** `m1/backend/app/api/reconcile.py:32-61`.
- **Evidence:** `ReconcileRequest` was `shipments: list[dict]`, `tolerance_poisha: int = 0`;
  the dependency was `_auth_placeholder()` which "intentionally does nothing". `GET /api/rules`
  was likewise unauthenticated.
- **Live verification (Stream C, 2026-11-01 ~17:26 Dhaka, no auth header, no cookie):**
  `curl -X POST http://127.0.0.1:8123/api/reconcile -d '{"shipments":[{"tracking_id":"T1"}],"tolerance_poisha":0}'`
  → `200`, full computed result. `curl http://127.0.0.1:8123/api/rules` → `200`.
- **Impact:** anyone who can reach the API can run the reconciliation engine for free,
  unlimited, with arbitrary shipment dicts. The engine is pure compute (no DB writes), so
  this is not a data-breach vector, but it is (a) unauthenticated use of a core business
  function, (b) an unbounded CPU/DoS surface — no cap on `len(shipments)`, no cap on dict
  size/depth, no request-body size limit in `main.py`, and (c) the engine echoes
  attacker-supplied values back in results/findings, so it is also an unauthenticated
  reflection point. Negative `tolerance_poisha` was accepted.
- **Remediation:** wire `get_current_user` + `require_csrf`; strict Pydantic shipment model
  (C-1.2); `max_length=1000` on the list; `tolerance_poisha ge=0`; auth on `GET /api/rules`.
- **Verification status:** fixed in Phase 1 — see `SECURITY_REMEDIATION.md` (fix 4).

---

## High

### B-F2 / C-4.4 — HIGH — Full raw password-reset token logged
- **Component:** `m1/backend/app/services/auth_service.py:159-176`; `m1/backend/app/logging_config.py`.
- **Evidence:** `request_password_reset` built
  `reset_link = f"{settings.frontend_url}/reset-password?token=<raw>"` and logged it:
  `logger.info("password_reset_link_issued", extra={"user_id": …, "reset_link": reset_link})`.
  The code comment stated "M1 ONLY: no email provider is wired up, so the link goes to the
  structured log. Production must email it and must NOT log it." The `JsonFormatter` merges
  any `extra` field into the JSON line, so there is no allow-list on log fields.
- **Impact:** anyone with log access can reset any account whose reset was requested. This
  was the single most sensitive log line in the codebase.
- **Other logging (verified by Stream C grep):** no other logger call passed a password, raw
  session/reset token, request body, raw CSV value, or bank-adjacent datum; other log lines
  carry ids + counts only. Uvicorn access logs are default plain-text and log request paths
  including query strings — no sensitive query params exist today.
- **Remediation:** log only `user_id` on token issue; wire a real email provider before
  production (provider not chosen in Phase 1 — remaining blocker).
- **Verification status:** fixed in Phase 1 (fix 2). The log-only password reset itself
  remains a production blocker (no provider chosen).

### B-F3 — HIGH — Viewer/member roles can mutate financial data
- **Component:** all API routers except members and settings PATCH.
- **Evidence:** only member-management and settings PATCH were role-checked. Viewer-role users
  could call courier-account creation, CSV import upload, run creation, order/shipment
  creation, and all six settlement POSTs.
- **Impact:** a read-only viewer could create or alter financial records.
- **Remediation:** enforce a write-role check (owner/admin/member write, viewer read-only) on
  every mutating endpoint; non-members still get 404 first, viewers get 403.
- **Verification status:** fixed in Phase 1 (fix 3).

### C-1.2 — HIGH — `POST /api/reconcile` accepts raw unvalidated dicts; engine cleans rather than rejects
- **Component:** `m1/backend/app/domain/engine.py:250-290` (`reconcile_shipment`), `reconcile.py:35`.
- **Evidence:** `shipments: list[dict]` — no schema at all. The engine copies each dict and
  "cleans" non-integer money fields to NULL + `DQ_INVALID_VALUE`, falls back to fixture rates
  when a rate is absent, and substitutes `"full_cod"` for an unsupported base. Unknown keys
  are silently ignored; nested structures are not type-checked.
- **Impact:** mostly correctness/robustness, not a direct injection (no SQL — engine is pure
  Python; no eval/exec found). Silently cleaning hostile input into "valid-looking" output
  weakens the honesty guarantees the rest of the system enforces; a huge/deeply-nested dict
  is a CPU/memory risk (see C-1.1).
- **Remediation:** strict Pydantic model for a reconcile shipment (same StrictInt/enum
  discipline as `shipments.py`), `extra="forbid"`, per-field bounds; reject rather than clean.
  The engine itself was NOT changed (V2.2.1 logic preserved).
- **Verification status:** fixed in Phase 1 (fix 4). Note: the engine distinguishes an
  absent key from an explicit `None` in one place (`reported_excluded_tax_poisha` falls back
  to `excluded_tax_poisha` only when absent), so the handler passes
  `model_dump(exclude_none=True)`.

### C-1.3 — HIGH — `extra="forbid"` is missing on 6 of 15 request models
- **Component:** `m1/backend/app/api/*.py`.
- **Evidence (grep `extra=` across `backend/app/api/`):** `extra="forbid"` present on:
  `CreateWorkspaceRequest`, `CreateAccountRequest`, `InviteRequest`, `ChangeRoleRequest`,
  `TransferOwnerRequest`, `CreateReceiptRequest`, `CreateShipmentRequest`, `CreateOrderRequest`,
  `UpdateSettingsRequest`. **Absent on:** `RegisterRequest`, `LoginRequest`,
  `PasswordResetRequest`, `PasswordResetConfirm` (`auth.py:28-46`), `CreateBatchRequest`,
  `CreateAllocationRequest` (`settlements.py:50-73`), `CreateRunRequest` (`runs.py:44-50`),
  `ReconcileRequest` (`reconcile.py:32-35`, covered by C-1.1/C-1.2).
- **Impact:** Pydantic v2 default is `extra="ignore"`, so unknown fields are silently
  dropped — no mass-assignment vulnerability was found (see C-1.4), but silent acceptance
  hides client bugs and weakens the "reject what you don't understand" posture. For
  `CreateBatchRequest`/`CreateAllocationRequest`/`CreateRunRequest` this matters most, because
  those bodies sit next to tenant-scoped IDs.
- **Remediation:** add `model_config = ConfigDict(extra="forbid")` to all six models; test
  that each POST rejects an unknown field with 422.
- **Verification status:** fixed in Phase 1 (fix 7).

### C-2.1 — HIGH — No row, column, or field-size limit on CSV import
- **Component:** `m1/backend/app/api/imports.py:37,67-71`,
  `m1/backend/app/services/csv_import.py:193-248,507-590`.
- **Evidence:** the only size control was `MAX_UPLOAD_BYTES = 10 MB`, enforced **after**
  `await file.read()` had already loaded the whole file into memory. `parse_csv_bytes` did
  `rows = list(reader)` — the entire file materialised in memory, then a second `parsed`
  list, then a per-row loop inserting an `ImportRow` + `StatementLine` + candidates + errors
  per row with `db.flush()` per row, all in one transaction. No `MAX_ROWS`, no `MAX_COLUMNS`,
  `csv.field_size_limit` never set (Python `csv` default 131072 bytes per field). A 10 MB
  single-column file is ~500k short rows. No request timeout on the endpoint or uvicorn.
- **Impact:** an authenticated member can force a long-running, memory-heavy,
  single-transaction import (DB row bloat, lock contention, worker starvation — single-process
  uvicorn, no timeout). The most realistic availability abuse in the import path. The
  500-row cap on the **detail** endpoint does not limit ingestion.
- **Remediation:** row cap 50,000 (clean rejection above it), column cap 100, explicit
  `csv.field_size_limit`, stream-parse instead of `list(reader)`, streaming read with early
  10 MB abort (C-2.2), per-request timeout / background-job treatment for large imports
  before production.
- **Verification status:** fixed in Phase 1 except the timeout/background-job treatment,
  which remains (fix 8, fix 20).

### C-2.3 — HIGH — CSV formula injection: no sanitisation or flagging of `=`, `+`, `-`, `@`-leading cells
- **Component:** `m1/backend/app/services/csv_import.py` (parse + store),
  `m1/frontend/src/pages/ImportDetail.tsx:397-403`, `CourierStatements.tsx:421`.
- **Evidence:** grep for `formula`, `startswith` with `=+@`, or any sanitiser across
  `backend/app` and `frontend/src` returns **no hits**. Raw cell values are stored verbatim
  by design, returned verbatim in `get_batch_detail`, and rendered in the frontend.
- **Mitigating (verified):** values are rendered as React JSX text / `JSON.stringify(...)`
  inside JSX — React escapes text content, so **no formula executes in the browser** and
  there is no XSS via this path. There is **no CSV/Excel export feature** in the app, so
  today there is no in-app path from a stored `=…` cell into a spreadsheet.
- **Residual risk (why still High):** the stored raw values are the system's own data export
  surface in waiting — any future "download import as CSV/XLSX" feature, admin export, or
  copy-paste of raw values into Excel/Sheets would execute a leading `=`, `+`, `-`, `@`,
  `|` or `%` cell as a formula (e.g. `=cmd|' /C calc'!A0`). `ImportError.raw_value` also
  stores attacker-controlled raw strings verbatim. A courier statement CSV is
  attacker-influenced data (a merchant uploads a file they received/created).
- **Remediation:** at ingestion, flag cells whose stripped value starts with `=`, `+`, `-`,
  `@`, `|`, `%` (a `FORMULA_LIKE_VALUE` warning in `import_errors`, as for `VALUE_INVALID`);
  **and** at any future export boundary, neutralise such cells (e.g. prepend `'` or a space)
  per OWASP CSV-injection guidance. Do NOT silently rewrite stored raw values (verbatim
  preservation is a design rule) — flag at ingest, neutralise at export.
- **Verification status:** fixed in Phase 1 at ingest (fix 9). The export-neutralisation
  half remains, because no export feature exists yet — it is a requirement on any future
  export, recorded in `SECURITY_REMEDIATION.md`.

### C-4.1 / C-5.3 — HIGH — No security headers at all; docs/OpenAPI surface is public
- **Component:** `m1/backend/app/main.py:68-88`; `m1/frontend/index.html`, `m1/frontend/vite.config.ts`.
- **Evidence (code):** the only middleware was `CORSMiddleware`. No `TrustedHostMiddleware`,
  no header middleware. The global `@app.exception_handler(Exception)` returns
  `{"detail": "Internal server error."}` (500) and logs the traceback server-side — the
  **response** shape is correct.
- **Evidence (live, Stream C, 2026-11-01 ~17:26 Dhaka):** `GET /api/health` → headers:
  `date`, `server: uvicorn`, `content-length`, `content-type: application/json`,
  `vary: Origin`. **No** `strict-transport-security`, `x-content-type-options`,
  `x-frame-options`, `content-security-policy`, `referrer-policy`, or `permissions-policy`
  on any response observed (health, root, 422, CORS preflight). `GET /docs` → **200**;
  `GET /openapi.json` → **200** (FastAPI defaults — docs not disabled, no auth on them).
  The OpenAPI schema enumerates every route, model, and field name.
- **Impact:** missing HSTS/X-Content-Type-Options/frame-ancestors/CSP is a standard production
  hardening gap (clickjacking of the Vite app, MIME sniffing, no transport guarantee).
  Public `/docs` + `/openapi.json` gives an unauthenticated attacker a complete API map —
  material combined with C-1.1.
- **Remediation:** header middleware (HSTS `max-age=31536000; includeSubDomains` behind
  HTTPS, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` / `frame-ancestors 'none'`,
  a CSP for the Vite app, `Referrer-Policy: no-referrer`, `Permissions-Policy`); disable
  docs in production (`docs_url=None, redoc_url=None, openapi_url=None` when
  `environment == "production"`, or gate behind auth).
- **Verification status:** fixed in Phase 1 (fix 10).

### A-F1 — HIGH — Exposed local-dev password still in the current tree
- **Component:** `.github/workflows/ci.yml`, `.github/workflows/ci-selfhosted.yml`, docs.
- **Evidence (Stream A, as tasked):** the exposed local-dev placeholder password was claimed
  to be present as 10 SQL `ALTER ROLE … WITH PASSWORD` literals. **Re-check in Phase 1
  corrected this:** the ALTER/CREATE ROLE statements in both workflow files interpolate
  the env var `${CI_PG_PASSWORD}`, not a literal value. The actual exposure found was:
  (a) a second distinct 19-char literal fallback password at `ci.yml:48` and
  `ci-selfhosted.yml:66` (`${{ secrets.CI_POSTGRES_PASSWORD || '<literal>' }}`), also quoted
  in `m1/LOCAL_VERIFICATION.md:387` and `m1/IMPLEMENTATION_CHECKLIST.md:237`; and
  (b) the live local-dev password was the publicly documented placeholder string
  (named in `m1/README.md:84-85,200`, `m1/backend/README.md:411,535`,
  `m1/IMPLEMENTATION_CHECKLIST.md:212`, `m1/LOCAL_VERIFICATION.md:135,200,397`).
  The historical committed password (commit `6c6c03b`, 10 chars) also appeared in the
  current tree in 3 test files as example env values (5 occurrences) and in stale
  `__pycache__` files.
- **Impact:** a publicly documented string was the live DB password for both DB roles; the
  fallback literal was a second live credential in CI files. Both are local-dev only —
  **no production credential was ever committed** (boolean-verified).
- **Remediation:** remove all literals; fail closed when the secret is unset; rotate the
  GitHub repo secret and both local DB roles to one fresh random value; scrub docs and test
  files of literal values.
- **Verification status:** fixed in Phase 1 (fix 5). Boolean verification in
  `SECURITY_REMEDIATION.md` and `PRODUCTION_SECRETS_GUIDE.md`. The historical value
  remains in git history — see the history-cleanup plan (approval-gated, NOT executed).

---

## Medium

### B-F5 — MEDIUM — No rate limit on register/reset-request; prior reset tokens not invalidated
- **Component:** `m1/backend/app/api/auth.py`, `m1/backend/app/services/auth_service.py`,
  `m1/backend/app/services/rate_limit.py`.
- **Evidence:** only login was rate-limited. Register and password-reset request had no
  limit; issuing a new reset token did not invalidate prior unused tokens.
- **Impact:** unbounded account creation and reset-token generation (log flooding — each
  request wrote a token row and, before fix 2, a log line containing the live reset link).
- **Remediation:** per-IP rate limit on register (50/hour) and reset-request (10/hour);
  invalidate prior unused reset tokens on issue (`UPDATE used_at=now()` — UPDATE grant
  exists on `password_reset_tokens`).
- **Verification status:** fixed in Phase 1 (fix 12). See also C-7.1.

### B-F6 — MEDIUM — Login rate limit in-memory, per (IP+email), no proxy handling
- **Component:** `m1/backend/app/services/rate_limit.py`, `m1/backend/app/api/auth.py:120`.
- **Evidence:** the login limiter is process-local in-memory (does not survive restart,
  does not share across workers) and keys only on (IP + lower-cased email). Behind a reverse
  proxy, `request.client.host` is the proxy's IP unless uvicorn runs with `--proxy-headers`
  and a trusted proxy list (see C-8.3).
- **Remediation:** add a per-account (email-only) bucket in addition to the per-IP bucket;
  document that `X-Forwarded-For` must only be trusted from a configured proxy and that
  Redis is required for multi-worker production (no Redis code change in Phase 1).
- **Verification status:** partially fixed in Phase 1 (fix 28 — per-account bucket added).
  Redis migration remains.

### B-F7 — MEDIUM (residual) — RLS is tenant-scoped, not role-scoped
- **Component:** `m1/backend/app/db/rls.sql` (`merchant_memberships` policies).
- **Evidence:** the `merchant_memberships` UPDATE policy is
  `USING (merchant_id = app_current_merchant_id())` — any session in the workspace can
  UPDATE membership rows in that workspace, including its own role. Role enforcement lives
  only in the application layer (`member_service`, owner/admin-only).
- **Impact:** a member who can run SQL as the runtime role can promote their own membership
  within their own workspace.
- **Remediation:** role-scoped RLS policies — a design change, NOT done in Phase 1.
- **Verification status:** NOT fixed in Phase 1. A direct-SQL probe test pins the residual
  behaviour (`tests/test_security_phase1_live_pg.py::test_direct_sql_self_promotion_not_blocked_by_rls_residual_b_f7`):
  the self-promotion UPDATE succeeds at the SQL layer, while the HTTP layer still refuses
  it (403).
- **Phase 1.1 (2026-11-01) — FIXED.** `merchant_memberships` INSERT/UPDATE are now
  role-scoped: only an active owner/admin may insert or manage a membership, only an
  owner may set `role='owner'`, the sole exception is self-acceptance of an invited row,
  backstopped by the `fn_membership_role_guard` trigger (which also closes the
  NEW-vs-OLD role-smuggling hole a policy cannot express) and a transaction-start role
  snapshot so `transfer_owner` is not broken by flush order. New helpers
  `app_current_user_role()` / `app_current_user_role_effective()` in `rls.sql`. The
  probe test was rewritten to assert blocking
  (`test_direct_sql_self_promotion_blocked_by_rls_b_f7`) and 11 new live tests in
  `tests/test_rls_roles_live_pg.py` prove cross-tenant AND cross-role restrictions
  under the runtime role. Financial-table role-scoping was evaluated and DEFERRED
  (fixture sweep needed first) — see `SECURITY_DECISIONS.md` D-3. Residual: an admin
  can still demote an owner to admin via direct SQL (app blocks it with 422); the DB
  layer only blocks the subsequent owner-grant.

### B-F8 — MEDIUM — Most live tests run as owner role
- **Component:** `m1/backend/tests/live_pg.py` and the live test files.
- **Evidence:** the live harness defaulted to the owner role (`courierledger`); the runtime
  role's grants were therefore not exercised by most live tests.
- **Remediation:** run the full live suite under the runtime role
  (`CL_LIVE_PG_ROLE=courierledger_runtime`); add runtime-role live tests for the Phase 1
  fixes.
- **Verification status:** fixed in Phase 1 — the full live suite was run under the runtime
  role; see `SECURITY_TEST_REPORT.md`.

### B-F9 — MEDIUM — Enumeration via register 400 + invite 422 + login timing (no dummy verify)
- **Component:** `m1/backend/app/api/auth.py`, `m1/backend/app/services/auth_service.py`,
  `m1/backend/app/api/members.py`.
- **Evidence:** register with an existing email → `400 "An account with this email already
  exists."` (see also C-4.3). Invite with an unknown email → 422. Login on an unknown user
  skipped the Argon2 verify, so timing differed.
- **Remediation:** register with an existing email returns 200 + a generic message (201 when
  created); dummy Argon2 verify against a fixed hash on the unknown-user / inactive /
  no-local-password login paths; invite keeps the 422 but has per-workspace rate limiting,
  and the residual invite oracle is documented as accepted (invite requires owner/admin).
- **Verification status:** fixed in Phase 1 (fix 13), except the documented residual invite
  oracle.

### B-F10 — MEDIUM — `audit_logs` table exists but nothing ever writes to it
- **Component:** `m1/backend/app/db/models.py`, `m1/backend/app/db/roles.sql`.
- **Evidence:** the table and grants exist; no service inserts audit rows.
- **Impact:** no audit trail for security-relevant actions.
- **Remediation:** Phase 2 feature — NOT done in Phase 1.
- **Phase 1.1 (2026-11-01) — FIXED.** Single writer
  `m1/backend/app/services/audit_service.py` (`record_audit`, closed 23-action
  vocabulary, never commits — the audit row shares the mutation's transaction);
  call sites in auth, members, imports, runs, settlements and settings. No
  schema change (the table already existed). Append-only enforcement and
  tenant isolation live-verified under the runtime role (UPDATE/DELETE raise
  `InsufficientPrivilege`; A's rows invisible under B's context). Detail never
  contains passwords, tokens, bank account numbers, raw file contents or
  email addresses. See `SECURITY_DECISIONS.md` D-7, including the
  INSERT … RETURNING trap found live and fixed app-side (explicit
  `created_at`).

### B-F11 — MEDIUM — No session caps / renewal / step-up
- **Component:** `m1/backend/app/services/auth_service.py`, `m1/backend/app/config.py`.
- **Evidence:** sessions last 7 days with no renewal, no per-user session cap, no step-up
  authentication for sensitive actions.
- **Remediation:** Phase 2 feature — NOT done in Phase 1.
- **Phase 1.1 (2026-11-01) — FIXED in part.** 10-session cap per user
  (evict-oldest on login); step-up authentication for owner transfer
  (`POST /api/auth/step-up`, 10-minute verification, 15-minute fresh-login
  window, Alembic 0005 adds `step_up_expires_at` to `user_sessions`);
  logout also clears the CSRF cookie; password-reset confirm rate-limited
  (50/hour/IP) and restructured to a single commit (password + `used_at` +
  session revocation). Deliberately NOT done, with reasons in
  `SECURITY_DECISIONS.md` D-5: no sliding refresh, no rotation on login,
  no 24-hour lifetime, no Redis. See D-5/D-6 for the full lifecycle rules.

### B-F15 — MEDIUM (residual) — Revoked member's session stays valid (documented design)
- **Component:** `m1/backend/app/services/auth_service.py`, `m1/backend/app/api/deps.py`.
- **Evidence:** membership revocation does not revoke the user's sessions; `get_user_by_session_token`
  checks `revoked_at` only, not membership status.
- **Remediation:** documented design, NOT changed in Phase 1. A revoked member keeps their
  session but every workspace-scoped call re-checks membership and returns 404.
- **Phase 1.1 (2026-11-01) — FIXED as designed.** The base layer is unchanged
  (fresh membership recheck → 404 on every workspace call). New: when the
  revoked user holds NO other active membership, `revoke_member` also revokes
  ALL of their sessions (single-workspace users are logged out entirely);
  multi-workspace users keep their session for their other workspaces. New
  manager-only `POST /api/workspaces/{id}/members/{user_id}/revoke-sessions`
  ("force logout", for cause, CL-20 locked, audit-logged). OD-11 preserved.
  Live-verified in `tests/test_security_phase11_live_pg.py` (single- and
  multi-workspace revoke, force-revoke). See `SECURITY_DECISIONS.md` D-4.

### C-1.5 — MEDIUM — Money fields are not uniformly StrictInt; no upper bound anywhere
- **Component:** `m1/backend/app/api/settlements.py:50-73`, `runs.py:44-50`, `reconcile.py:35`.
- **Evidence:** `CreateBatchRequest.expected_batch_payout_poisha: int | None = Field(ge=0)`,
  `CreateReceiptRequest.amount_poisha: int = Field(gt=0)`,
  `CreateAllocationRequest.allocated_poisha: int = Field(gt=0)` — plain `int`, not `StrictInt`;
  Pydantic coerces `True` → `1` and `"100"` → `100`. `CreateRunRequest.tolerance_poisha`
  plain `int`, no `ge=0` at the model (server-side check in `run_service.py:319-328` catches
  bool/negative, so the effective behaviour was safe but the boundary inconsistent).
  `ReconcileRequest.tolerance_poisha: int = 0` — no bound at all. By contrast
  `CreateOrderRequest.order_cod_poisha`, `CreateShipmentRequest.amount_to_collect_poisha`,
  `UpdateSettingsRequest.tolerance_poisha` were already `StrictInt` with `ge=0`.
  **No money field anywhere had an upper bound** — a value above PostgreSQL `bigint` range
  (≈9.2×10¹⁸) would reach the DB layer; behaviour on overflow was not live-tested, likely
  a 500 from the DB driver rather than a clean 422.
- **Remediation:** `StrictInt` for every money field; upper bound `le=10**15` poisha
  (≈ ৳10¹³) at the Pydantic layer so overflow is a 422, not a 500.
- **Verification status:** fixed in Phase 1 (fix 11).

### C-1.6 — MEDIUM — No maximum password length; Argon2id on unbounded input
- **Component:** `m1/backend/app/api/auth.py:28-46`, `m1/backend/app/services/auth_service.py:67-69,183-184`.
  (Same fix as B-F14.)
- **Evidence:** `password: str = Field(min_length=12)` — minimum only, no `max_length`, in
  `RegisterRequest`, `LoginRequest`, `PasswordResetConfirm`. Argon2id parameters are
  OWASP-recommended (`security.py`: time_cost=3, memory_cost=65536 KiB, parallelism=4) — hashing
  itself is sound — but Argon2 hashes the **entire** input, so a multi-MB password body is
  a CPU/memory DoS vector on register/login/reset-confirm. No global JSON body-size limit
  (see C-1.1).
- **Remediation:** `max_length=128` on all three password fields + service-layer enforcement.
- **Verification status:** fixed in Phase 1 (fix 15).

### C-1.7 — MEDIUM — String length limits are good but uneven; a few unbounded strings reach the DB
- **Component:** `m1/backend/app/api/*.py`, `m1/backend/app/services/csv_import.py`.
- **Evidence:** most text fields carry `max_length` (workspace name/trading_name 200, courier
  refs 200, order ref 500, shipment tracking 200 / evidence refs 500). **Unbounded:**
  `CreateReceiptRequest.receipt_ref_label` and `evidence_note` (bare `str | None`, land in
  `Text` columns — no DB error, but an unbounded string is stored and returned verbatim);
  `CreateRunRequest.courier_fixture` (validated against `FIXTURES` keys server-side, so a
  long string is rejected — safe in effect); `CreateRunRequest.idempotency_key` (see C-7.2);
  import `mapping` source-header strings (`validate_mapping` requires non-empty strings but
  sets no max length — a 10 MB mapping value is bounded only by the 10 MB upload cap).
- **Remediation:** `max_length` 500/1000 on `receipt_ref_label`/`evidence_note`; 500 on
  mapping header strings.
- **Verification status:** fixed in Phase 1 (fix 18).

### C-2.2 — MEDIUM — Upload size is checked after the full file is read into memory; no MIME/extension/content validation
- **Component:** `m1/backend/app/api/imports.py:50,67-80`, `csv_import.py:193-248`.
- **Evidence:** `data = await file.read()` then `len(data) > MAX_UPLOAD_BYTES` → 413. No
  `UploadFile` size limit via Starlette, no streaming read. **No content-type check:**
  `file.content_type` never inspected; `content_type="text/csv"` hardcoded when the
  `FileObject` row is created regardless of what was uploaded. **No extension check**
  server-side (the frontend `<input accept=".csv,text/csv">` is advisory only).
  The only real gate was "does it decode as UTF-8 and parse as CSV".
- **Impact:** low direct security impact — a non-CSV file that parses as CSV is treated as
  CSV (garbage-in, flagged rows); a non-CSV binary is rejected at UTF-8 decode with a
  graceful `parse_failed`. The issue is (a) memory: the 413 is enforced too late, and
  (b) mislabelling: any file is recorded as `text/csv`.
- **Remediation:** stream-read with an early abort at 10 MB; store the actual declared type
  (allow-list `text/csv`, `application/vnd.ms-excel`, else `"unknown"`).
- **Verification status:** fixed in Phase 1 (fix 20). A non-`text/csv` content-type is
  recorded as `"unknown"` rather than rejected — browsers vary in what they send, so
  rejection was not chosen; the CSV parse itself remains the gate.

### C-4.2 — MEDIUM — Unhandled-exception path is generic, but several failure modes can still surface as 500s
- **Component:** `m1/backend/app/main.py:76-88`, `m1/backend/app/services/run_service.py:785,794`.
- **Evidence:** the global handler catches `Exception` and returns the generic message — but
  `run_service.py:785,794` has bare `except Exception:` blocks whose behaviour (swallow vs
  re-raise) was not fully traced. A DB-level failure (e.g. bigint overflow from C-1.5, or
  an RLS/trigger violation surfacing as an unhandled SQLAlchemy error) would hit the generic
  handler and return the safe 500 — but the structured log line then contains the full
  traceback including SQL text and bound values (server-side only; safe if logs are
  access-controlled).
- **Remediation:** keep the generic handler; add a test that forces a DB error and asserts a
  generic 500 with no SQL in the body; audit the two bare `except Exception` blocks in
  `run_service.py`.
- **Verification status:** partially addressed in Phase 1 — the overflow path is now a 422
  (C-1.5 fix 11), so the most concrete 500 trigger is gone. The `run_service.py` bare
  `except` blocks were not audited in Phase 1 — remaining.

### C-5.1 — MEDIUM/LOW — CORS: single exact origin, credentials allowed — correct shape, but methods/headers are wildcard
- **Component:** `m1/backend/app/main.py:60-66`.
- **Evidence:** `CORSMiddleware(allow_origins=[settings.frontend_url], allow_credentials=True,
  allow_methods=["*"], allow_headers=["*"])` with `frontend_url` default
  `http://127.0.0.1:5173`. **Live preflight (Stream C, 2026-11-01):** `OPTIONS /api/auth/login`
  with `Origin: http://127.0.0.1:5173` → `200`,
  `access-control-allow-origin: http://127.0.0.1:5173`,
  `access-control-allow-credentials: true`,
  `access-control-allow-methods: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT, QUERY`,
  `access-control-allow-headers: x-csrf-token,content-type`. With
  `Origin: https://evil.example`, the health response carried **no**
  `access-control-allow-origin` (correctly refused).
- **Assessment:** origin handling is right (exact single origin, not `*`, not a regex, not
  reflected). `allow_methods=["*"]` + `allow_headers=["*"]` is broader than needed — the API
  only needs `GET, POST, PATCH, DELETE(?)` and `content-type, x-csrf-token, idempotency-key`
  — and with `allow_credentials=True` a wildcard method/header set is exactly the combination
  the CSRF double-submit token is meant to contain. The CSRF check is the compensating
  control, present on every state-changing route except the unauthenticated `/api/reconcile`
  (C-1.1 — the hole is the missing auth, not the CORS config).
- **Remediation:** narrow to `allow_methods=["GET","POST","PATCH","DELETE","OPTIONS"]`,
  `allow_headers=["Content-Type","X-CSRF-Token","Idempotency-Key"]`; keep the single-origin
  list; production `FRONTEND_URL` must be the exact HTTPS origin.
- **Verification status:** fixed in Phase 1 (fix 22). Frontend + e2e still pass.

### C-5.2 — MEDIUM — Session cookie is `HttpOnly` but `Secure=False` and `SameSite=lax` by default; CSRF cookie is readable by JS by design
- **Component:** `m1/backend/app/config.py`, `m1/backend/app/api/auth.py:60-92`,
  `m1/backend/app/api/deps.py:50-65`.
- **Evidence:** `_set_session_cookie`: `httponly=True, secure=settings.session_cookie_secure
  (default False), samesite=settings.session_cookie_samesite (default "lax"), path="/",
  max_age=7 days`. `_set_csrf_cookie`: `httponly=False` (required — the frontend JS must read
  it to send the header), same `secure`/`samesite`, `max_age` also 7 days. CSRF check is
  constant-time double-submit (`hmac.compare_digest`); missing/mismatched → 403. CSRF token
  is 256-bit `secrets.token_urlsafe(32)`. The CSRF token is issued by an unauthenticated
  `GET /api/auth/csrf` and returned **both** as a cookie and in the JSON body — standard for
  this pattern. The CSRF token is cached in frontend memory and re-fetched on 403; it is
  **not rotated** after login/logout (same token lifetime as the session, 7 days).
- **Assessment:** `Secure=False` is correct for local HTTP dev and **unsafe for production**
  if left as-is. The default is the hazard: nothing forces `Secure=True` when
  `ENVIRONMENT=production` (unlike the placeholder-secret guard). `SameSite=lax` is reasonable
  (top-level GET navigation sends the cookie; cross-site POSTs do not, which also blunts
  CSRF). The CSRF cookie being JS-readable is inherent to the double-submit pattern; the risk
  is bounded because the session cookie itself is `HttpOnly` and the CSRF token alone is
  useless without the cookie.
- **Remediation:** refuse to start in production with `SESSION_COOKIE_SECURE=false` (mirror
  the existing placeholder-secret guard); rotate the CSRF token on login/logout (see C-6.3).
- **Verification status:** fixed in Phase 1 (fix 14 — production guard + login rotation).
  `SameSite=strict` was not adopted (not needed; `lax` is reasonable).

### C-7.1 — MEDIUM — Only login is rate-limited; registration, password-reset request, CSV upload, and run creation have no limit
- **Component:** `m1/backend/app/services/rate_limit.py`, `m1/backend/app/api/auth.py:122-135`.
- **Evidence:** the **only** use of `rate_limit` in the codebase was the login endpoint:
  5 failed logins per (IP + lower-cased email) per 15 minutes → 429. Grep for
  `rate_limit|is_blocked|record_failure` outside `rate_limit.py` returns only `auth.py`.
  **No limiting on:** `POST /api/auth/register` (unbounded account creation — combined with
  the email-enumeration 400, an attacker can probe which emails exist and create accounts
  at will), `POST /api/auth/password-reset/request` (unbounded reset-link generation — each
  request writes a token row **and a log line containing the live reset link**, C-4.4),
  `POST /api/workspaces/{id}/imports` (only the 10 MB cap; repeated uploads are bounded by
  idempotency keys only when the caller reuses a key), `POST …/runs`,
  `POST …/settlement-batches`, `POST …/receipts`, member invites, or any GET list endpoint.
  The login limiter itself is **process-local in-memory** (does not survive restart, does not
  share across workers) — a multi-worker deployment resets it per worker.
- **Abuse potential:** registration spam / fake-account creation; password-reset email/log
  flooding (and, once email is wired, outbound-mail abuse and cost); CSV-upload compute/DB
  flooding (compounds C-2.1); run/settlement creation spam inside a compromised or
  self-registered account (a workspace is free to create, so an attacker can create
  workspaces and hammer their own — the cost falls on the server, not the attacker).
- **Remediation:** per-IP (and per-account, where authenticated) rate limits on register,
  password-reset request, CSV upload, and run creation — at minimum a shared-store (Redis)
  limiter before any multi-worker production deployment; replace the in-memory login limiter
  with the same store. Add a CAPTCHA or email-verification gate on registration before
  production.
- **Verification status:** partially fixed in Phase 1 — register (50/hour/IP), reset-request
  (10/hour/IP), and member invite (10/hour/workspace) are now limited (fixes 12, 13, 28).
  CSV upload and run-creation limits, the Redis migration, and CAPTCHA/email verification
  remain.

### C-8.2 — MEDIUM — `docker-compose.yml` hardcodes `ENVIRONMENT: development` and publishes the database port
- **Component:** `m1/docker-compose.yml`.
- **Evidence:** backend service: `ENVIRONMENT: development` (literal, not `${ENVIRONMENT:-…}`);
  db service: `ports: ["5432:5432"]`; `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-CHANGE_ME}`
  — if the variable is unset the placeholder is used silently. The file header says "local
  development stack" — the risk is reuse: this is the only compose file in the repo, and
  nothing prevents it being used to stand up a "production" instance with dev settings.
- **Remediation:** parameterise `ENVIRONMENT` (`${ENVIRONMENT:-development}`), never default
  a password to `CHANGE_ME` (fail closed if unset), remove the published DB port or bind it
  to loopback, and add a `compose.prod.yml` override. Add the `SESSION_COOKIE_SECURE`
  production guard (C-5.2) so a dev-mode compose cannot silently serve insecure cookies in
  prod.
- **Verification status:** fixed in Phase 1 except the `compose.prod.yml` override, which
  remains (fix 25). Docker cannot run in this VM — verified by YAML validation + inspection
  only (B-2 still open).

### C-8.3 — MEDIUM — Uvicorn is run without `--proxy-headers` / trusted-proxy config, and `request.client.host` is trusted for rate limiting
- **Component:** `m1/backend/Dockerfile`, `m1/docker-compose.yml`, `m1/backend/app/api/auth.py:120`.
- **Evidence:** `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]` and
  compose `command: uvicorn app.main:app --host 0.0.0.0 --port 8000` — no `--proxy-headers`,
  no `--forwarded-allow-ips`. Login rate limiting keys on `request.client.host`. Behind any
  reverse proxy, `client.host` is the proxy's IP unless `--proxy-headers` is set with a
  trusted proxy list — then all clients share one rate-limit bucket (self-DoS / bypass
  confusion) and logs misattribute IPs.
- **Remediation:** when a proxy is introduced, run uvicorn with
  `--proxy-headers --forwarded-allow-ips=<proxy IP(s)>` and `TrustedHostMiddleware`; document
  that `X-Forwarded-For` must only be trusted from that proxy.
- **Verification status:** NOT fixed in Phase 1 — documented as a production requirement
  (no proxy exists yet; no code change).

### A-F2 — MEDIUM — Literal fallback password in CI workflow files (and docs)
- **Component:** `.github/workflows/ci.yml:48`, `.github/workflows/ci-selfhosted.yml:66`,
  `m1/LOCAL_VERIFICATION.md:387`, `m1/IMPLEMENTATION_CHECKLIST.md:237`.
- **Evidence:** `${{ secrets.CI_POSTGRES_PASSWORD || '<literal 19-char value>' }}` — a
  second distinct literal fallback password, also quoted in two docs. (The first literal
  is A-F1.)
- **Remediation:** remove the fallback; fail closed when the secret is unset.
- **Verification status:** fixed in Phase 1 (fix 5). Boolean-verified: the literal appears
  nowhere in the current tree (count 0 in both workflow files and both docs).

### A-F3 — MEDIUM — Self-hosted CI workflow drops the shared dev databases and resets shared role passwords
- **Component:** `.github/workflows/ci-selfhosted.yml`.
- **Evidence:** the workflow used databases `courierledger_app`/`courierledger_dev` and ran
  `ALTER ROLE … WITH PASSWORD` against the shared roles — a CI run could wipe or re-credential
  the development databases.
- **Remediation:** use dedicated CI-only databases (`courierledger_ci_app` /
  `courierledger_ci_dev`), never drop `courierledger_app`/`courierledger_dev`, do not reset
  shared role passwords (the workflow uses the secret for DATABASE_URLs; the secret must
  equal the current role password for self-hosted runs).
- **Verification status:** fixed in Phase 1 (fix 6). All `ALTER ROLE … WITH PASSWORD`
  statements removed from the self-hosted workflow (0 left).

### A-F8 — MEDIUM — P0 prototype dependency vulnerabilities (python-multipart 0.0.20)
- **Component:** `prototype-p0/` (read-only).
- **Evidence:** backend `pip-audit` clean; P0 python-multipart 0.0.20 has 6 known vulns
  (PYSEC-2026-1852, -3038, -3039, -3040, -3036, -3037; fix ≥ 0.0.31).
- **Remediation:** NOT applied — P0 is read-only by task instruction. Documented as
  remaining with "never deploy P0 network-reachable".

---

## Low

### B-F12 — LOW — `create_workspace()` trusts `p_user_id`
- **Component:** `m1/backend/app/db/rls.sql` (`create_workspace`).
- **Evidence:** the SECURITY DEFINER function accepted any `p_user_id` with no check that it
  matches the authenticated user (`app.user_id`).
- **Remediation attempted and REVERTED in Phase 1:** an assertion
  (`p_user_id IS DISTINCT FROM public.app_current_user_id()` → raise) was added and then
  reverted, because it broke 29 existing live tests (`test_triggers_live_pg.py`,
  `test_m6a1_live_pg.py`, `test_idempotency_lease_live_pg.py`) whose fixtures call the
  function directly as the runtime role without setting `app.user_id`. The function
  therefore still trusts its argument. The control is the single call site
  (`workspace_service.create_workspace` passes the session user's id; `deps.py` sets
  `app.user_id` from the same session before the call) plus the app-layer auth on the
  endpoint.
- **Verification status:** NOT fixed in Phase 1 — recorded as remaining. A residual probe
  test pins the behaviour
  (`tests/test_security_phase1_live_pg.py::test_create_workspace_trusts_p_user_id_residual_b_f12`):
  a direct-SQL caller can create a workspace for another user. Also fixed during Phase 1
  re-provisioning (kept): the three `merchant_memberships` policies had no
  `DROP POLICY IF EXISTS` guards, so `rls.sql` could not be re-applied; guards added
  (no logic change).

### B-F13 — LOW — Secure cookie opt-in, CSRF drift
- **Component:** `m1/backend/app/api/deps.py`, `m1/backend/app/config.py`, `m1/backend/app/api/auth.py`.
- **Evidence:** `SESSION_COOKIE_SECURE` defaulted false with no production guard; `deps.py`
  hardcoded the CSRF cookie/header names instead of honouring `settings.csrf_cookie_name` /
  `csrf_header_name`; the CSRF token was not rotated at login.
- **Remediation:** production startup guard (see C-5.2); `require_csrf` now uses the settings
  names; CSRF token rotated at login.
- **Verification status:** fixed in Phase 1 (fix 14).

### B-F14 — LOW — Password min-length only; duplicate-register race → 500
- **Component:** `m1/backend/app/api/auth.py`, `m1/backend/app/services/auth_service.py`.
  (Same fix as C-1.6.)
- **Evidence:** no password maximum (see C-1.6); a concurrent duplicate registration raised
  an unhandled `IntegrityError` → 500.
- **Remediation:** `max_length=128` on the three password fields + service-layer
  enforcement; catch `IntegrityError` in `register_user` and map to the generic response.
- **Verification status:** fixed in Phase 1 (fix 15).

### B-F16 — LOW — Maintenance role has SELECT on all tables
- **Component:** `m1/backend/app/db/roles.sql`.
- **Evidence:** `courierledger_maintenance` is granted broad SELECT.
- **Remediation:** NOT changed in Phase 1 — the role is NOLOGIN except during a controlled
  maintenance window; documented as accepted.

### B-F17 — LOW — `app_current_*` EXECUTE granted to PUBLIC
- **Component:** `m1/backend/app/db/roles.sql`.
- **Evidence:** PostgreSQL grants EXECUTE to PUBLIC by default on new functions, so
  `app_current_merchant_id()` / `app_current_user_id()` were executable by PUBLIC.
- **Remediation:** `REVOKE EXECUTE … FROM PUBLIC` before the explicit grants to
  `courierledger_runtime` and `courierledger_maintenance`.
- **Verification status:** fixed in Phase 1 (fix 16); re-provisioned on both dev DBs;
  live-verified (`has_function_privilege('public', …) = false`, runtime = true).

### B-F18 — LOW — Identity tables have no RLS (design)
- **Component:** `m1/backend/app/db/rls.sql`, `m1/backend/app/db/models.py`.
- **Evidence:** `users`, `user_sessions`, `password_reset_tokens` are not RLS-protected;
  access is by application query only.
- **Remediation:** accepted design — NOT changed in Phase 1.

### B-F19 — LOW — Expired sessions / tokens accumulate
- **Component:** `m1/backend/app/services/auth_service.py`, `m1/backend/app/db/models.py`.
- **Evidence:** no scheduled cleanup of expired `user_sessions` / `password_reset_tokens` rows.
- **Remediation:** maintenance procedure for Phase 2 — NOT done in Phase 1.
- **Phase 1.1 (2026-11-01) — procedure exists, invocation pending.**
  `auth_service.purge_expired_auth_rows(db)` DELETEs expired `user_sessions`
  and expired-or-used `password_reset_tokens` in one commit and returns the
  counts. It is a procedure, not an endpoint: it must run with an
  owner/migrator connection during a maintenance window (the runtime role
  has no DELETE grant). No scheduler or invocation exists yet — a
  deployment/operations decision. Covered by a SQLite test (expired rows
  deleted, live/revoked-unexpired rows kept).

### B-F20 — LOW — Stale comments
- **Component:** `m1/backend/app/services/member_service.py`, `m1/backend/app/logging_config.py`,
  `m1/backend/app/db/rls.sql`, `m1/backend/app/db/triggers.sql`.
- **Evidence:** `member_service.change_role` said "Owner transfer is not supported in M9"
  (it is, since M10); `logging_config` claimed an allow-list on log fields that does not
  exist; `rls.sql`/`triggers.sql` headers said "UNVERIFIED" after the deferred triggers
  were live-verified in M6/M7.
- **Remediation:** all four comments corrected in Phase 1 (fix 17).

### C-1.8 — LOW — Enum validation is done by hand in services, not by Pydantic literals; error shape is inconsistent
- **Component:** `m1/backend/app/api/shipments.py:97-113`, `members.py:51-64`,
  `statements.py:222-227`, `settings.py:56`.
- **Evidence:** `outcome`/`outcome_source` are validated by `@field_validator` against
  `SHIPMENT_OUTCOMES`/`OUTCOME_SOURCES` (good, 422 on unknown); `locale` uses
  `Literal["en","en_bn"]` (good); member `role` is a bare `str` validated in
  `member_service.py:340,473` against `INVITABLE_ROLES` (owner → 422 `role_not_invitable`);
  `match_status` query param validated by hand against `MATCH_STATUSES`. All paths reject
  unknown values — no bypass found — but the 422 payload differs by layer (Pydantic
  list-of-objects vs `{"message","reason"}` vs plain string), which the frontend already
  has to special-case.
- **Remediation:** prefer `Literal[...]` at the Pydantic layer for `role`/`outcome`/
  `match_status` so validation is uniform; not a security fix, a consistency one.
- **Verification status:** NOT changed in Phase 1 (consistency only).

### C-1.9 — LOW — Pagination is capped on most list endpoints, but three list endpoints are uncapped
- **Component:** `m1/backend/app/api/*.py`, `m1/backend/app/services/csv_import.py`.
- **Evidence:** capped (`Query(20|50, ge=1, le=100)`, `offset ge=0`): runs, run results, run
  findings, settlements, shipments, orders, statements (`page_size` 20, le=100).
  **Uncapped (no limit/offset params at all):** `GET /api/workspaces/{id}/imports`
  (`.scalars().all()` over every batch), `GET /api/workspaces` (all memberships),
  `GET /api/workspaces/{id}/bank-receipts` (list-all shape; limit params not present in the
  read portion — **not fully verified** by Stream C). Import **detail** is capped at 500
  rows with a `truncated` flag — good — but its **errors** list was uncapped
  (`select(ImportError)… .scalars().all()` with no `.limit()`), and the statement-lines
  subquery in `get_batch_detail` was also uncapped (all lines for the batch loaded to build
  `line_by_row`).
- **Impact:** a workspace with many imports / a batch with many error rows returns an
  unbounded JSON payload — response-size / memory DoS by an authenticated member, and slow
  pages. Low severity because it requires an authenticated member and a large pre-existing
  dataset.
- **Remediation:** paginate `list_imports` and `bank-receipts` (same `limit le=100` pattern);
  cap the errors list in `get_batch_detail` (500 + `truncated_errors` flag) and page
  statement lines instead of loading all.
- **Verification status:** fixed in Phase 1 for `list_imports`, the errors cap, and the
  statement-lines cap (fix 19). `bank-receipts` was already paginated (verified in
  `settlements.py` during Phase 1). `GET /api/workspaces` (all memberships) was not changed
  — a user can only belong to a bounded number of workspaces; noted as accepted.

### C-1.10 — LOW — Path/query parameter validation is otherwise sound (positive-adjacent)
- **Component:** all routers.
- **Evidence:** every path id is typed `uuid.UUID` (FastAPI returns 422 on a malformed UUID).
  Query filters: `order_id`/`import_batch_id` typed `uuid.UUID | None`; `tracking_id` is a
  bare `str | None` filter passed to the service — SQLAlchemy parameterises it, so no
  injection; no length cap on it (trivial). All SQL goes through SQLAlchemy ORM /
  parameterised `select()` — **no string-built SQL, no f-string query, no `text()` with
  interpolation was found** by grep.
- **Verification status:** no fix needed.

### C-3.2 — LOW — User-controlled strings are rendered as React text (escaped) — safe in the DOM; two rendering spots deserve a note
- **Component:** `m1/frontend/src/pages/ImportDetail.tsx`, `CourierStatements.tsx`, `Imports.tsx`.
- **Evidence:** workspace names, file names, CSV raw values, and error messages are rendered
  as JSX text interpolations. React escapes text content by default, so `<script>`,
  `<img onerror=…>`, and `=cmd|…` strings display as inert text. Backend error strings are
  surfaced via `readErrorMessage` and rendered as text, not HTML. No `v-html`-style bypass
  exists.
- **Notes:** (a) `JSON.stringify(row.raw_values)` renders the **entire** raw row — including
  any attacker-crafted string — but as escaped text, so this is display-only exposure, not
  execution. (b) `original_filename` is stored verbatim; if it is ever rendered it must stay
  as JSX text. (c) The one place a raw value could become dangerous is a future export/copy
  path — see C-2.3.
- **Remediation:** none required for the current DOM rendering; add a lint rule banning
  `dangerouslySetInnerHTML` and a test that a raw value containing
  `<img src=x onerror=alert(1)>` renders as text.
- **Verification status:** NOT changed in Phase 1 (no fix needed; lint rule + test remain).

### C-3.3 — LOW — Backend renders no HTML at all; error responses are JSON
- **Component:** `m1/backend/app/main.py`, `m1/backend/app/api/*.py`.
- **Evidence:** every response is a JSON dict / Pydantic model / `JSONResponse`. No
  Jinja/templates/HTMLResponse in the codebase (grep: no hits). FastAPI's default validation
  errors are JSON — live-verified (see C-4.1). The OpenAPI docs UI (`/docs`, Swagger) is
  served by FastAPI itself — see C-5.4.
- **Verification status:** no fix needed.

### C-4.3 — LOW — 404-vs-403 behaviour is consistently 404 for cross-tenant resources (positive finding, one nuance)
- **Component:** every router's `_require_membership`.
- **Evidence:** non-members get `404 {"detail":"Workspace not found."}` on every
  workspace-scoped route — existence of another tenant's workspace/batch/run/line is not
  leaked. Verified in code across all routers and live for the unauthenticated workspace
  case (401, not 404 — correct, since no session exists at all). **Nuance:** role failures
  are correctly 403 (settings, member management), so an attacker can distinguish "exists
  but forbidden" (403) from "not found" (404) **only** for settings/member-management
  actions on a workspace they already belong to — not a cross-tenant leak.
- **Other enumeration notes:** register with an existing email → `400` (see B-F9/C-7.1).
  Login is correctly generic (`401 "Invalid email or password."` for unknown email and
  wrong password alike). Password-reset request always returns 200 — good.
- **Verification status:** no fix needed; cross-tenant 404 behaviour is now pinned by live
  tests (`tests/test_security_phase1_live_pg.py::test_cross_tenant_probes_404_live`, 35
  probes).

### C-4.5 — LOW — Validation error payloads echo input and internal field names
- **Component:** FastAPI default 422s; `m1/backend/app/api/shipments.py`, `settlements.py`.
- **Evidence (live):** `{"detail":[{"type":"int_parsing","loc":["body","tolerance_poisha"],
  "msg":"…","input":"x"}]}` — the offending value is echoed. Some 422s include internal
  reason codes and one 422 embeds the caller-supplied value in the message
  (`f"Unknown courier code: {body.courier_code!r}."`). None of these leak secrets, paths, or
  SQL; they leak internal field/enum names (already visible in `/openapi.json`). The main
  risk is a very long reflected input being echoed back in a 422 — bounded by the request
  body size, which is itself unbounded (see C-1.1).
- **Remediation:** no change required for production beyond disabling `/openapi.json` (C-4.1)
  and adding a body-size limit; consider stripping `"input"` from 422s if a WAF/proxy is not
  in front.
- **Verification status:** NOT changed in Phase 1 (docs disabled in production in fix 10;
  body-size limit remains).

### C-5.4 — MEDIUM — FastAPI docs (`/docs`, `/redoc`, `/openapi.json`) are enabled and unauthenticated (see also C-4.1)
- **Component:** `m1/backend/app/main.py:58`.
- **Evidence (live):** `GET /docs` → 200; `GET /openapi.json` → 200. (An earlier probe of
  `/api/docs` returned 404 — docs live at the root `/docs`, not under `/api`.)
- **Remediation:** disable in production or gate behind authentication.
- **Verification status:** fixed in Phase 1 (fix 10 — disabled when `ENVIRONMENT=production`).
  Note: the §5.4 heading in the Stream C report says "MEDIUM", but Stream C's own
  authoritative severity table (§9) lists C-5.4 under Low; the consolidated count follows
  the table (Low).

### C-5.5 — LOW — Vite dev proxy and frontend serving notes
- **Component:** `m1/frontend/vite.config.ts`, `m1/frontend/Dockerfile`.
- **Evidence:** dev proxy forwards same-origin `/api` → `http://127.0.0.1:8000`
  (`changeOrigin: true`) — dev-only, correct for local work. The frontend Dockerfile serves
  the production bundle with `vite preview` on `0.0.0.0:5173`, which **does not apply the dev
  proxy** (the Dockerfile's own comment says the containerised frontend cannot reach the
  backend yet and the compose service stays commented out). `vite preview` is not a
  production static server (no security headers, no hardening).
- **Remediation:** production frontend must be served by a hardened static host / reverse
  proxy with security headers and an `/api` reverse-proxy to the backend; do not ship
  `vite preview` as the production server.
- **Verification status:** NOT fixed in Phase 1 — deployment reference only.

### C-6.2 — LOW — 401/403 handling is present but thin; CSRF token is silently dropped on failure
- **Component:** `m1/frontend/src/lib/api.ts:130-202`, `m1/frontend/src/App.tsx:27-55`.
- **Evidence:** `apiFetch` throws `ApiError(status, message)` on any non-OK response;
  `getCsrfToken()` fetches a fresh token when none is cached and falls back to reading the
  `cl_csrf` cookie; `clearCsrfToken()` exists "e.g. after a 403" but **no call site was
  found** that invokes it on a 403 in the code read. The auth guard (`ProtectedLayout`)
  blocks rendering behind a pending `GET /api/auth/me` and redirects to `/login` on error —
  **no protected page renders data before auth resolves** (positive). Individual pages do
  not appear to have a global 401 → redirect-to-login interceptor: a 401 mid-session
  surfaces as a page-level error via React Query rather than a redirect — usability, not a
  leak. `credentials: 'include'` is set on every fetch — correct for the cookie session.
  `apiFetch` attaches the CSRF header only when a token could be obtained; if
  `getCsrfToken()` returns null (backend unreachable), the write proceeds **without** the
  header and the backend returns 403 — fail-closed, correct.
- **Remediation:** wire `clearCsrfToken()` + a single retry on 403 in `apiFetch`, and a
  global 401 handler that redirects to `/login` (clearing React Query state).
- **Verification status:** fixed in Phase 1 (fix 27). Verified with Vitest + e2e.

### C-6.3 — LOW — CSRF token is fetched over an unauthenticated GET and cached for the session lifetime
- **Component:** `m1/backend/app/api/auth.py` (`GET /api/auth/csrf`),
  `m1/frontend/src/lib/api.ts:84-117`.
- **Evidence:** `GET /api/auth/csrf` requires no session; it issues a fresh 256-bit token,
  sets the cookie, and returns it in JSON. The frontend caches it in memory for the page
  lifetime and reuses it for every write. No rotation on login/logout was found.
- **Impact:** low — the double-submit pattern does not require the token to be secret from
  the holder (the attacker must also hold the victim's session cookie, which is `HttpOnly`
  + `SameSite=lax`); a token obtained by an unauthenticated caller is useless without a
  victim session. Rotation would still be good hygiene.
- **Verification status:** fixed in Phase 1 — CSRF token rotated at login (fix 14).

### C-7.2 — LOW — Idempotency keys are unbounded client strings with no length cap
- **Component:** `m1/backend/app/api/imports.py:53-54`, `runs.py:44-50,73`.
- **Evidence:** `idempotency_key: str | None = Form(...)` / header `Idempotency-Key`, and
  `CreateRunRequest.idempotency_key: str | None` — no `max_length`. The key is hashed/used
  as a DB lookup key; a multi-KB key is stored/hashed per request. Bounded in practice by
  the request itself, but a cap (e.g. 128 chars) costs nothing.
- **Remediation:** `max_length=128` on all idempotency-key inputs.
- **Verification status:** fixed in Phase 1 (fix 23).

### C-8.4 — LOW — Settings use `extra="ignore"` and read `.env` from the working directory
- **Component:** `m1/backend/app/config.py`.
- **Evidence:** `SettingsConfigDict(env_file=".env", extra="ignore")` — unknown env vars are
  silently ignored (a typo'd `SESSION_COOKIE_SECURE` would silently fall back to `false` —
  the dangerous direction for a boolean). `env_file=".env"` resolves relative to the process
  working directory, so running uvicorn from the wrong directory silently loads no `.env`
  and falls back to compiled-in defaults (including the placeholder DB password).
- **Remediation:** consider `extra="forbid"` for settings (fail fast on typos) and an absolute
  `env_file` path or an explicit startup log line naming the settings source (the startup
  log already logs `environment` + DB scheme — extend it to log whether `.env` was loaded).
- **Verification status:** partially fixed in Phase 1 — the startup log now states whether
  `.env` was loaded (fix 26). `Settings extra="forbid"` was NOT set (too risky for
  deployments with extra vars, per task).

### C-8.5 — LOW — `frontend/.env.example` is minimal and safe; no Vite secret is exposed
- **Component:** `m1/frontend/.env.example`.
- **Evidence:** single entry `VITE_API_URL=` (empty). No secret is referenced. Note: any
  `VITE_*` variable is baked into the client bundle at build time — nothing sensitive is set
  today; keep it that way.
- **Verification status:** no fix needed.

### A-F5 — LOW — Test helper password in `tests/conftest.py`
- **Component:** `m1/backend/tests/conftest.py`, `m1/backend/tests/live_pg.py`, live test files.
- **Evidence:** test helpers used a fixed literal password for test accounts.
- **Remediation:** generate a random password per test (`secrets.token_urlsafe`).
- **Verification status:** fixed in Phase 1 (fix 24). The historical literal that appeared
  in 3 test files as example env values was replaced with `<your-local-db-password>`.

### A-F6 — LOW — Placeholder-style example passwords in docs
- **Component:** `m1/README.md`, `m1/LOCAL_VERIFICATION.md`, `m1/backend/app/db/roles.sql`
  comment, `m1/IMPLEMENTATION_CHECKLIST.md`.
- **Evidence:** docs named a real-looking placeholder string as the example DB password.
- **Remediation:** replace with obviously-fake markers (e.g. `<choose-a-password>`) or
  `${VAR}` references. Boolean-verify none equals any real credential.
- **Verification status:** fixed in Phase 1 (fix 30). The documented placeholder string
  appears nowhere in the current tree (count 0, boolean-verified).

### A-F7 — LOW — `docker-compose.yml` password defaults and DB port (see also C-8.2)
- **Component:** `m1/docker-compose.yml`.
- **Evidence:** `POSTGRES_PASSWORD` defaulted to `CHANGE_ME`; DB port published on
  `5432:5432`; `ENVIRONMENT` hardcoded to `development`.
- **Remediation:** parameterise `ENVIRONMENT` (`${ENVIRONMENT:-development}`), fail closed
  if a password is unset, bind the DB port to loopback (`127.0.0.1:5432:5432`).
- **Verification status:** fixed in Phase 1 (fix 25). Verified by YAML validation +
  inspection only — Docker cannot run in this VM (B-2 still open).

### A-F9 — LOW — npm audit blocked by egress (not verified)
- **Component:** `m1/frontend` (npm dependencies).
- **Evidence:** `npm audit` could not run (egress proxy blocked); OSV fallback was clean
  (0 vulns in 254 locked packages) — a fallback, not a full audit.
- **Remediation:** re-run `npm audit` in CI when the GitHub billing lock is resolved.
- **Verification status:** NOT verified in Phase 1 — remains.

---

## Positive findings (worth preserving)

### C-1.4 — Mass assignment: no exploitable case found
Tenant id is never taken from a body: every router derives `merchant_id` from the
`{workspace_id}` path parameter after `get_membership`. `expected_collectible_cod_poisha` is
server-derived; `status`/`bank_status`/`allocation_status` are server-set; `created_by_user_id`
comes from the session user; `negative_payout_permitted=False` is hardcoded in `csv_import.py`.
`CreateShipmentRequest` accepts `is_superseded` + `superseded_by_shipment_id`, but the service
validates both (successor must exist, be in the same workspace **and** the same order) and a
superseded COD shipment never counts as the active one. `CreateBatchRequest.run_id` is validated
to be a run in the same workspace. `CreateRunRequest.courier_fixture` is validated against
`FIXTURES`; `rule_version_id`/`courier_fixture` exactly-one rule enforced.
`CreateRunRequest.tolerance_poisha` is re-validated server-side (bool rejected, `< 0` rejected).
Residual note: `CreateShipmentRequest.merchant_courier_account_id` is accepted but validated to
belong to the workspace. No case was found where a caller can set a computed amount, a status,
a tenant id, or `created_by`. Verified by reading each router + the cited service checks.
Not live-tested with forged cross-tenant ids — now pinned by the live cross-tenant probe tests
in Phase 1.

### C-2.4 — CSV parser robustness: malformed input fails gracefully (with two gaps)
Encoding: `data.decode("utf-8-sig")` — BOM stripped, non-UTF-8 → `ImportFailedError` →
batch `parse_failed` + `VALUE_UNPARSABLE` error row, committed, summary returned (not a 500).
Other encodings (e.g. Windows-1252 / Shift-JIS) are **not** attempted — a legitimate non-UTF-8
courier file fails (usability, not security). `csv.reader` default dialect; `csv.Error`
(including the 128 KiB field-size limit and NUL-byte errors) is caught → `ImportFailedError` →
same graceful path. Field-count mismatch per row → `parse_status='failed'`, row preserved,
import continues. Empty file → `ImportFailedError("File is empty.")`; header with no usable
columns → same; **duplicate header names (case-insensitive) → whole file rejected**. Blank
lines → `skipped_blank` rows, kept with row numbers.
**Gaps:** (a) the `parse_failed` path returned HTTP **201** with `status:"parse_failed"` — a
client that checks only the status code thinks the upload succeeded; **fixed in Phase 1
(fix 21 — now 422, frontend + e2e still pass)**. (b) The `ImportFailedError` message embeds
the stdlib exception text — it contains no user data beyond the parser's own wording, so
disclosure is minimal.

### C-2.5 — Uploaded file content is never served back over HTTP (positive finding); on-disk storage is the residual
Grep for `FileResponse|StreamingResponse|StaticFiles|Content-Disposition|send_file` across
`backend/app` and `frontend/src`: **no hits**. No endpoint returns file bytes; the detail
endpoint returns only parsed `raw_values` JSON. Files are written content-addressed as
`backend/var/imports/<merchant_id>/<sha256>.csv` — the filename on disk is the SHA-256, **not**
the user-supplied `file.filename` (stored only as a DB text column, never used as a path — no
path-traversal via filename). `storage_key` is `local://<merchant_id>/<sha256>.csv`.
**Residuals (Medium/Low):** (a) files live on the application server's local disk under
`backend/var/` — any other process/user on that host, or a future static-file mount, could
read them; no encryption-at-rest and no access check at the filesystem layer (the API-level
check is "no endpoint serves them", which is true today but fragile). (b) Cross-tenant
retrieval via the API is not possible (no endpoint), but the on-disk layout keys only by
merchant id + hash — a bug in a future download endpoint that forgot the merchant check would
expose all tenants' files. (c) `original_filename` is stored verbatim and rendered in the UI
— see C-3.2.
**Remediation:** move file storage to object storage with per-tenant keys + signed,
short-lived download URLs behind the membership check before production; add a regression
test asserting no route serves `var/imports`; document that `backend/var/` must never be
mounted as static files. **Not done in Phase 1** — deployment concern, documented as
remaining.

### C-3.1 — No `dangerouslySetInnerHTML` / `innerHTML` / `javascript:` URLs anywhere
Grep across `frontend/src` (ts/tsx) and `backend/app`: **zero hits**. All `href`s in the
frontend are static internal paths. No user-controlled URL is ever placed in an `href`/`src`.

### C-6.1 — No auth token in `localStorage`/`sessionStorage`
Grep in `frontend/src` (excluding tests): only `frontend/src/lib/workspace.ts` — it stores
**the selected workspace id** (a UUID), not any credential. No token is stored in web storage
anywhere. The session lives in the `HttpOnly` `cl_session` cookie; the CSRF token lives in
the non-HttpOnly `cl_csrf` cookie **and** in an in-memory JS variable, never persisted to
storage. The stored workspace id is validated against the server-returned workspace list
before use, so a tampered value cannot select a workspace the user does not belong to; the
backend re-checks membership on every call.

---

## Verdicts (from Stream C §9, carried forward)

- **CSV / formula-injection verdict: NOT SAFE for production as-is at audit time; flagging
  added in Phase 1.** No sanitisation or flagging of `=`, `+`, `-`, `@`-leading cells existed
  anywhere in the backend or frontend (C-2.3). The current React UI does not execute formulas
  (text is escaped), and there is no in-app CSV/Excel export, so there is **no live exploit
  in the app today** — but stored raw values are verbatim and API-returnable, so the first
  export feature (or a user pasting raw values into a spreadsheet) creates a live
  CSV-injection path. Flag at ingest (done, Phase 1) + neutralise at export (required on any
  future export). Separately, the CSV import path was **not DoS-safe**: no row/column/field
  limits and a 10 MB post-read size check (C-2.1, C-2.2) — caps and streaming added in
  Phase 1.
- **Error-disclosure verdict: MOSTLY SAFE, with two exceptions.** 500s return a generic
  `{"detail":"Internal server error."}` and no traceback/SQL/config in the response
  (code-verified, not live-triggered); cross-tenant 404s do not leak existence; login is
  enumeration-safe. Exceptions: (1) **the password-reset link was written to the structured
  log** (C-4.4) — removed in Phase 1; anyone with log access could reset any account.
  (2) **`/docs` and `/openapi.json` were public** (C-4.1/C-5.4, live-verified 200) — the full
  API map was unauthenticated; disabled in production in Phase 1. Validation 422s echo input
  and internal field names (standard FastAPI behaviour; low risk once docs are gated).

## What was verified live vs in code only (Stream C, at audit time)

- **Live-verified (curl, 2026-11-01 ~17:26 Dhaka, against the already-running 127.0.0.1:8123):**
  no security headers on any response; CORS preflight (exact origin allowed, evil origin
  refused, methods/headers wildcard); unauthenticated `POST /api/reconcile` → 200;
  `GET /api/rules` → 200; `GET /docs` → 200; `GET /openapi.json` → 200; `GET /api/docs` →
  404; unauthenticated workspace GET → 401; 422 shape with echoed input.
- **Code-verified only:** 500 response shape (generic handler read; no live 500 triggered);
  cookie flags (`Secure`/`SameSite`/`HttpOnly` — no login performed, so no `Set-Cookie`
  observed); CSV parse-failure 201 shape; bigint-overflow behaviour; `bank-receipts`
  pagination (not fully re-read); whether any page calls `clearCsrfToken`.
- **Not verified:** anything requiring an authenticated session; behaviour under a real
  reverse proxy / HTTPS; log contents at runtime.
