# CourierLedger — Security Decision Record (Phase 1.1, 2026-11-01)

Phase 1.1 of production-readiness: security closure & authorization hardening.
Phase 1 (2026-11-01) left four findings open as designed-for-Phase-1.1 work and
three as accepted/deferred residuals; this record explains what Phase 1.1 decided,
what it changed, what it deliberately did NOT change, and why. It is the companion
to `SECURITY_AUDIT.md` (findings), `SECURITY_REMEDIATION.md` (fixes) and
`SECURITY_TEST_REPORT.md` (test evidence). All counts in the test report are actual
executed results. Nothing in this record claims production readiness — the project
is **NOT production-ready** (see the blockers list at the end).

Scope of Phase 1.1 (from the authorising brief): role-scoped RLS (B-F7),
revoked-member session security (B-F15), session lifecycle hardening (B-F11,
including the B-F19 cleanup procedure), and an immutable audit trail (B-F10).
No rebuild, no V2.2.1 financial-logic change, no microservices, no Redis,
no deploy, no history rewrite, no force-push. The financial engine
(`app/domain/`) and the P0 prototype (`prototype-p0/`, read-only) were not touched.

## D-1. Role-scoped RLS on `merchant_memberships` (B-F7 — FIXED)

**Decision.** The `merchant_memberships` INSERT and UPDATE policies are now
role-scoped at the database layer, in addition to the existing application-layer
checks (`member_service`, owner/admin-only). Only an ACTIVE owner/admin of the
workspace may INSERT a membership or manage one (invite, change_role, revoke,
reactivation); only an owner may set `role='owner'`. The sole exception is
self-acceptance: the invited user themself may flip their OWN invited row to
active. A BEFORE UPDATE trigger (`fn_membership_role_guard`,
`trg_membership_role_guard` in `triggers.sql` §10) backstops the policy,
because a policy WITH CHECK cannot compare `NEW.role` to `OLD.role` — without
the trigger an invited user could smuggle a role change (e.g. viewer → owner)
into the accept UPDATE.

**Mechanism.** Two new SQL helpers in `rls.sql` (same shape as the existing
context helpers: LANGUAGE sql, STABLE, `SET search_path TO public, pg_catalog`,
never raise; PUBLIC EXECUTE revoked, runtime + maintenance granted):

- `app_current_user_role()` — the caller's ACTIVE membership role for the
  current merchant context, else NULL (fail-closed: invited/revoked rows do
  not count; no context → NULL). Its inner SELECT is policed by the
  membership SELECT policy, which never calls this function — no recursion.
- `app_current_user_role_effective()` — the transaction-start role snapshot
  (see D-2) for the current merchant when one exists, else the live role.

The UPDATE policy is ONE combined policy, not two split ones: PostgreSQL ORs
the USING of one permissive policy with the WITH CHECK of another, so a
manager arm and a self-accept arm in separate policies could mix in ways
neither arm allows alone. USING reads the live role (visibility never
outlives the caller's live manager status); WITH CHECK reads the effective
role (see D-2). `create_workspace()` is unaffected (SECURITY DEFINER + NO
FORCE on the two identity-root tables, unchanged). SELECT is unchanged.

**Alternatives considered.**

- *Application layer only (status quo from Phase 1).* Rejected as the sole
  control: any caller who can run SQL as the runtime role could self-promote.
  The app checks remain as the first line (good UX, 403s) and RLS is now the
  backstop — complementary, not a second inconsistent system: both enforce
  the same rule (owner/admin manages, owner alone grants owner).
- *Two split UPDATE policies (manager arm + self-accept arm).* Rejected:
  PostgreSQL's permissive-policy OR semantics would let the arms mix.
- *Trigger-only enforcement (no policy change).* Rejected: the policy is the
  primary statement-level control; the trigger exists only for the
  NEW-vs-OLD comparison the policy cannot express.

**Evidence.** `tests/test_rls_roles_live_pg.py` (11 live tests, runtime role):
helper values (NULL no-context/invited/outsider, owner/viewer, PUBLIC cannot
EXECUTE / runtime can); cross-tenant UPDATE blocked (rowcount 0) +
cross-tenant INSERT rejected + B row unchanged; viewer/member INSERT
rejected, admin INSERT works; member/viewer self-promote blocked (rowcount
0); admin role-change works but owner-grant raises; owner role changes +
transfer-style swap (both orders); demotion-first swap with a second owner
present; admin cannot manufacture an owner snapshot; invited-user smuggle
raises, accept-shaped status-only UPDATE works; HTTP accept end-to-end;
admin revoke + reactivate via direct SQL. The Phase 1 B-F7 probe test was
rewritten (same file location, renamed
`test_direct_sql_self_promotion_blocked_by_rls_b_f7`) to assert the blocking
behaviour — a behaviour change, not a weakening.

**Residual (honest).** An admin can still demote an owner to admin via direct
SQL (the app blocks it with 422 `owner_role_immutable`); the DB layer only
blocks the takeover completion (the subsequent owner-grant). That was outside
the brief's scope and is recorded here, not hidden.

## D-2. Transaction-start role snapshot (transfer_owner safety)

**Decision.** The guard trigger records the caller's pre-change role on the
FIRST role-changing membership UPDATE of a transaction, in the
transaction-local setting `app.membership_role_snapshot`
(`'<merchant uuid>:<role>'`); `app_current_user_role_effective()` prefers
that snapshot for the same merchant and falls back to the live role otherwise.

**Why it exists (proven necessary, not speculative).** The brief's literal
design (live-role WITH CHECK owner-guard) broke `transfer_owner`: the swap
changes two rows in one transaction (target member → owner, caller owner →
admin) and SQLAlchemy flushes the two UPDATEs in primary-key order, so the
caller's demotion can land BEFORE the target's promotion — a live-role check
then sees 'admin' for the promotion and rejects a legitimate transfer. This
was shown by a service-layer probe (`new row violates row-level security
policy`), not by reasoning. The snapshot preserves the security property
(only an owner grants owner) while making the swap order-independent.

**Safety properties (all probed, not assumed).**

- The snapshot is written only from the live-role lookup, only when the
  caller actually holds a role, and only on a role-changing update — a
  caller who merely touches their own row (`updated_at`, status) does not
  create one, so an admin cannot manufacture an 'owner' snapshot by
  updating their own row first (probed: touch-own-row then
  legit-change-then-grant — both refused).
- It is never consulted for INSERT (which uses the live role).
- Trust boundary: a caller running arbitrary SQL as the runtime role could
  forge the setting — but the same caller can already forge `app.user_id`
  itself (full impersonation), which defeats every policy here without any
  snapshot. The snapshot adds no new capability (documented in code in
  both `rls.sql` and `triggers.sql`).
- The trigger is deliberately plain/invoker-rights, NOT SECURITY DEFINER —
  a documented exception to the file's CL-22 rule 1 — so it resolves the
  caller's role under the caller's own RLS visibility, not as the table
  owner (who bypasses RLS on this NO FORCE table).
- GUC quirk found by hand probe: a custom GUC set transaction-locally
  reverts to `''`, not NULL, after COMMIT — so the snapshot-"unset" test is
  `COALESCE(..., '') = ''`, not `IS NULL` (the `IS NULL` version silently
  skipped snapshots in later transactions on pooled connections; caught by
  the 2-owner-transfer probe).

**Alternative considered.** *Reorder the flush in `transfer_owner`*
(promote first, demote second). Rejected: it would couple a security
invariant to SQLAlchemy flush ordering, which is an implementation detail
that could change; the snapshot makes the property order-independent at
the layer that enforces it.

## D-3. Financial-table role-scoping — DEFERRED, with evidence

**Decision.** A DB-layer viewer-block on the 8 mutable `FOR ALL` policies
(`workspace_settings`, `merchant_courier_accounts`, `courier_rule_versions`,
`orders`, `shipments`, `import_batches`, `settlement_batches`,
`bank_receipts`) was evaluated and NOT implemented in Phase 1.1.

**Why.** 19 `set_config('app.current_merchant_id', …)` direct-SQL sites
exist across live tests, but only 3 test files ever set `app.user_id`
(`test_runtime_role_live_pg.py`, `test_security_phase1_live_pg.py`,
`test_workspaces_live_pg.py`). The owner-role seed fixtures in
`test_m6a1_live_pg.py`, `test_triggers_live_pg.py`, `test_runs_live_pg.py`,
`test_settlement_live_pg.py`, `test_settings_live_pg.py`,
`test_idempotency_lease_live_pg.py` set merchant context only — and the
owner is FORCE-RLS policed on those tables, so a role requirement would
return NULL and block every seeded write. Fixing that needs a fixture
sweep across ~7 files, out of scope for this milestone. The app path itself
is safe (`deps.py` sets `app.user_id` on every authenticated request;
`require_write_role` blocks viewers with 403, Phase 1 fix 3). The helper
exists, the membership table proves the pattern; financial-table
role-scoping remains app-layer-only. Recorded in the `workspace_settings`
comment in `rls.sql`.

**What this means.** A viewer who can run SQL as the runtime role can still
write financial rows directly. That is the same residual class B-F7 was in
for memberships, now narrowed to the financial tables. The HTTP layer is
the only role enforcement there; the next milestone that touches those
fixtures should do the sweep and add the policies.

## D-4. Revoked-member session security (B-F15 — FIXED as designed)

**Decision.** Revocation now has two layers, and the Phase 1 "documented
design" (session stays valid, workspace calls 404 via fresh membership
recheck) is retained as the base layer:

1. **Base (unchanged):** every workspace-scoped call re-checks authoritative
   membership status (`workspace_service.get_membership` returns ACTIVE
   memberships only), so a revoked member's existing session is denied on
   every workspace endpoint (404) on the very next request, with no session
   row touched.
2. **New (Phase 1.1):** when the revoked user holds NO other active
   membership, `revoke_member` also revokes ALL of their sessions
   (`auth_service.revoke_all_sessions` — UPDATE `revoked_at`, Phase 1 fix
   1), so a single-workspace user is logged out entirely on revoke. A
   multi-workspace user keeps their session for their other workspaces —
   the revoked workspace still 404s via the base layer. This is a second,
   separate commit after the revoke itself, so a failure here cannot roll
   back the revocation.
3. **New (Phase 1.1):** a manager-only `POST
   /api/workspaces/{id}/members/{user_id}/revoke-sessions` endpoint
   ("force logout", for cause) revokes a member's sessions regardless of
   their other memberships. The target must hold (or have held) a
   membership row in this workspace — an admin cannot log out an arbitrary
   user. CL-20 locked, audit-logged (`members.sessions_force_revoked`),
   returns `{"revoked": n}`.

**RLS care (live PG).** The post-revoke "other memberships" count runs
under the TARGET's user context, not the actor's: the
`merchant_memberships` SELECT policy is user-based OR merchant-based, so
the actor's context cannot see the target's rows in OTHER workspaces. The
target id comes from the verified membership row, never from the client,
and the context is used for that read only (the revocation UPDATE is
already committed; the session revocation touches identity tables, which
have no merchant policy). It is not an impersonation — no write is
authorised by it. Documented in `member_service.py`.

**Alternatives considered.**

- *Revoke all sessions on every revoke (simplest).* Rejected: it would log
  a multi-workspace user out of workspaces where they are still a member.
- *Short session TTL / recheck membership on every request including
  `/me`.* Rejected as the mechanism: the base layer already rechecks on
  every workspace call; `/me` is a global identity endpoint and a revoked
  single-workspace user is now 401 there anyway via layer 2. No TTL change
  was needed.
- *Do nothing (keep Phase 1 design).* Rejected for the single-workspace
  case: a revoked user's session remaining globally valid was the finding.

**Preserved.** OD-11 (last-owner invariant): revoke still 422s on the last
active owner; transfer-owner is unchanged except for the audit row and the
step-up requirement (D-6). No unrelated user is invalidated: the
force-revoke endpoint is scoped to one member of one workspace.

**Evidence.** `tests/test_security_phase11.py` (SQLite) + `tests/test_security_phase11_live_pg.py`
(live, runtime role): revoke single-workspace → 401 everywhere including
`/auth/me`; revoke multi-workspace → session survives for the other
workspace, revoked one 404s; force-revoke manager-only (403/404 cases),
target's sessions 401, others unaffected. Two pre-existing tests were
updated for the deliberate behaviour change (revoke now 401s a
single-workspace member's session; the tests re-log-in to continue):
`tests/test_members.py::test_revoke_denies_workspace_immediately_and_reinvite_works`
and `tests/test_members_live_pg.py::test_revoke_denies_live_session_live`.

## D-5. Session lifecycle (B-F11 — FIXED in part; two items deliberately not done)

**Decided and implemented.**

- **Cap: 10 concurrent live sessions per user** (`session_max_concurrent =
  10`, `app/config.py`). Evict-oldest on login: when a login would exceed
  the cap, the oldest live sessions are revoked down to cap − 1, then the
  new session is inserted; evictions and insert share one commit. Rationale
  for evict-oldest over reject: a user who has genuinely lost devices
  should not be locked out of logging in; the evicted device is logged out
  on its next request. The cap is counted in Python (dialect-safe), not in
  SQL.
- **Step-up authentication (see D-6).**
- **Logout clears the CSRF cookie too** (previously only the session cookie
  was cleared; the 7-day CSRF cookie survived logout).
- **Password-reset confirm rate limit: 50/hour per IP** via the existing
  generic bucket (`reset_confirm_max_attempts` / `reset_confirm_window_seconds`).
- **Password-reset confirm is now a single commit:** password change +
  `used_at` + session-revoke UPDATE share one `db.commit()` (Phase 1 left a
  two-commit window: password committed, then revoke-all committed
  separately).
- **Expired session/token cleanup procedure (B-F19):**
  `auth_service.purge_expired_auth_rows(db)` DELETEs expired `user_sessions`
  (revoked or not) and expired-or-used `password_reset_tokens`, one commit,
  returns `{"sessions": n, "reset_tokens": m}`. It is a procedure, not an
  endpoint: it must run with an owner/migrator connection during a
  maintenance window (the runtime role has no DELETE grant). No scheduler
  or invocation exists yet — that is a deployment/operations decision, not
  a code gap to fill silently. B-F19 is therefore closed as "procedure
  exists, invocation pending" rather than "scheduled job exists".

**Deliberately NOT done, with reasons.**

- **No sliding refresh / no session rotation on login.** The 7-day absolute
  expiry is kept (documented choice). Sliding refresh would extend sessions
  indefinitely for an active attacker who stole a cookie; rotation on
  login would break legitimate multi-device use (each new login would kill
  the other device's session). The cap + step-up + revoke paths are the
  controls instead.
- **No 24-hour session lifetime.** Considered (Phase 1 remediation note);
  rejected as unjustified friction for a local-dev tool whose users are
  small merchants on shared devices — the cap and step-up cover the
  sensitive paths without punishing ordinary read-only use.
- **No Redis.** In-memory rate limiting (Phase 1) is retained; Redis was
  not proven necessary for a single-process local-dev deployment, and the
  brief forbids introducing it without justification. Redis-backed rate
  limiting remains a Phase 2 production item.

**Documented lifecycle rules (as implemented).** Sessions: 7-day absolute
expiry; at most 10 live per user, evict-oldest; logout revokes the current
session and clears both cookies; "logout from all sessions" is
`POST /api/auth/revoke-all` (existing, Phase 1) plus the manager
force-revoke endpoint (D-4); password reset revokes all prior sessions in
the same commit as the password change; step-up (D-6) gates owner transfer;
expired rows are purged only by the maintenance procedure above.

## D-6. Step-up authentication for sensitive actions

**Decision.** `POST /api/auth/step-up` (auth + CSRF): a password re-check
stamps `step_up_expires_at = now + 600s` on the matching session row
(new column, Alembic migration 0005) and returns 200; a wrong password
returns a generic 401 and the failure counts against the existing login
buckets (IP+email and per-account). `deps.require_step_up` is satisfied by
a live step-up verification OR a session created within 900s
(`step_up_fresh_login_seconds` — the normal login → transfer flow keeps
working without a second password prompt). Otherwise 403 with
"Step-up authentication required. Please re-enter your password."

**Scope (deliberate, narrow).** Applied to the transfer-owner endpoint
only. Owner transfer is the single most consequential membership action
(irreversible without the new owner, changes who controls the workspace).
Ordinary read-only operations and routine membership changes (invite,
role change, revoke) are NOT step-up-gated — that would be friction
without a matching threat, and role change/revoke are already
owner/admin-only and reversible. Security-credential changes (password
change itself) are covered by the reset flow's own token, not by step-up.

**Alternatives considered.**

- *Step-up on every membership mutation.* Rejected: friction without a
  distinct threat model; the attacker who holds a live session can already
  do those actions, and step-up on transfer covers the irreversible one.
- *Re-authentication via a fresh login (new session).* Rejected: it would
  disturb the user's other devices and sessions; stamping the existing
  session is the minimal change.
- *TOTP / second factor.* Out of scope: no second-factor infrastructure
  exists; password re-check is the available factor.

**Follow-up (not done in 1.1).** No frontend step-up prompt was built; the
15-minute fresh-login window keeps the normal login → transfer flow
working. A frontend prompt is a UI task for a later milestone.

**Evidence.** `tests/test_security_phase11.py`: aged session → 403; wrong
password → 401, still 403; correct → 200, transfer works; expired
verification → 403, re-verify → 200. Live: fresh works, 16-minute-old
session → 403 (aged by direct SQL), wrong step-up 401, correct step-up →
transfer works, last-owner revoke still 422.

## D-7. Immutable audit trail (B-F10 — FIXED)

**Decision.** Build on the existing `audit_logs` table (v0.1 C.7) — no schema
change was needed. The single writer is the new
`m1/backend/app/services/audit_service.py`: `record_audit(db, *, actor,
merchant_id, action, entity_table, entity_id, detail, actor_type="user")`,
with a closed 23-action vocabulary (unknown action raises `ValueError`),
never committing (the caller owns the transaction, so the audit row and the
change commit or roll back together), actor/merchant only from server-side
arguments, and `detail` restricted to ids/statuses/counts/poisha amounts.

**Coverage (23 actions).** Auth/identity (merchant NULL): `auth.register`,
`auth.login_success`, `auth.login_failed` (actor None, no email in detail),
`auth.logout`, `auth.revoke_all` (revoked count),
`auth.password_reset_requested` (known user: actor set; unknown account:
actor None, reason only), `auth.password_reset_completed` (sessions
revoked), `auth.step_up_verified`. Membership: `members.invited` (incl.
reactivation), `members.invitation_accepted`, `members.role_changed`
(old→new), `members.revoked`, `members.owner_transferred` (from→to),
`members.sessions_force_revoked` (revoked count). Business:
`imports.uploaded` (incl. `parse_failed` batches), `runs.created`,
`settlements.batch_created`, `settlements.receipt_created`,
`settlements.receipt_completed`, `settlements.allocated`,
`settlements.reversed`, `settlements.variance_calculated`,
`settings.updated` (only when a value actually changes, old→new).

**Deliberately NOT audited:** read endpoints; idempotent replays that
change nothing (re-invite of already-invited, accept of already-active,
complete of an already-complete receipt, run/import idempotency replays —
no new entity exists to audit); failed validations (422) and rate-limited
(429) requests (nothing mutated); workspace/courier-account/order/shipment
creation and the reconcile endpoint (not in the 1.1 vocabulary — reconcile
is compute-only and persists nothing).

**What is NOT in `detail`, ever:** passwords, tokens, bank account numbers,
raw file contents, email addresses. `login_failed` records no email at all.

**The INSERT … RETURNING trap (found live, fixed app-side).** The first
live run 500'd on register: `INSERT INTO audit_logs` violated RLS even
though the INSERT policy allows NULL merchant. Root cause: the ORM emits
`INSERT ... RETURNING created_at` for the server-defaulted column, and for
`INSERT ... RETURNING` PostgreSQL ALSO applies the table's SELECT policy to
the returned row — a NULL-merchant row can never pass that policy.
Verified with a minimal reproduction table and plain psql (RETURNING form
fails, plain INSERT succeeds, for owner and runtime alike); reproduced
identically against a pristine-HEAD `rls.sql` database, so it was
pre-existing and not caused by the Phase 1.1 SQL changes. Fix is app-side
in `record_audit`: `created_at` is set explicitly (app clock, UTC), which
makes the ORM emit a plain INSERT with no RETURNING. Documented in the
module docstring. Any future ORM insert into `audit_logs` (or any table
whose SELECT policy can reject its own new row) must follow the same rule.

**Immutability and isolation.** `audit_logs` is in the append-only set:
the `fn_append_only_guard` trigger rejects UPDATE/DELETE (live-verified:
both raise `InsufficientPrivilege` — the runtime role has no UPDATE/DELETE
grant, and the trigger is the backstop), and the table has RLS with a
tenant-scoped SELECT policy (live-verified: A's rows visible under A's
context, zero under B's). No new financial-history table was created: the
existing reconciliation/settlement history tables already cover financial
state; `audit_logs` covers who did what, when — the two are complementary,
not duplicated.

**Migration.** Alembic 0005 (`0005_phase11_session_step_up.py`) adds
`step_up_expires_at` (nullable timestamptz) to `user_sessions`. Both dev
DBs (`courierledger_app`, `courierledger_dev`) are at 0005. The audit trail
itself needed no migration (the table already existed).

## D-8. What Phase 1.1 did not change (residuals carried forward)

- **B-F12** (`create_workspace()` trusts `p_user_id`) — unchanged; probe
  test still pins it. The single-call-site discipline is the control.
- **B-F16** (maintenance SELECT all) — unchanged; NOLOGIN except during a
  controlled maintenance window.
- **B-F18** (identity tables have no RLS) — unchanged; accepted design.
- **Financial-table role-scoping** — deferred, see D-3.
- **Password reset remains log-only** (no email provider chosen) — never
  ship log-only to production.
- **Frontend step-up prompt** — not built (see D-6).
- **`purge_expired_auth_rows`** has no scheduler/invocation yet (see D-5).
- **Phase 2 items** (unchanged): password-reset email provider, Redis rate
  limiting, CSV background jobs/timeout, object storage, export
  neutralisation, body-size limit, `compose.prod.yml`.
- **`test_alembic_uses_migration_database_url`** now hardcodes 0005 and
  must move with the next migration (noted in the test).

## Remaining production blockers (unchanged verdict)

M1 is **NOT production-ready**. OD-12, OD-16, OD-17; B-2 Docker
(containers cannot start in this VM); ADR 1.0 missing; GitHub Actions
billing lock (pushes trigger a failed CI run — recorded, not chased);
password-reset provider not chosen; market-validation gates not run; no
merchant-confirmed courier evidence. Phase 1.1 is ready for review; it
does not change the production-readiness verdict.
