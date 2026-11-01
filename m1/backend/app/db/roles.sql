-- ============================================================================
-- CourierLedger M1 -- Roles and grants (roles.sql)
-- ============================================================================
-- APPLY BY: a database superuser / provisioning step, once, before the
-- application connects. This file is NOT executed by the application or
-- by Alembic. It creates the three roles and grants the privileges the
-- design requires. It does NOT wire the roles into the application: the
-- app's DATABASE_URL (app/config.py) currently names a single
-- connection, and separate runtime / migrator / maintenance connection
-- strings are a deployment decision for the coordinator -- see
-- "Role-enforced vs documented-only" below.
--
-- Roles (v0.1 E, CL-03, CL-04, CL-11, CL-12, CL-14, CL-15, CL-19):
--   courierledger_migrator    -- RESERVED, NOT IN SERVICE (Phase 3
--     decision, 2026-11-02 — Option 2, see below). The design once
--     said this role owns the schema and runs Alembic; live reality
--     is that it owns 0 objects, holds 0 grants, and never runs a
--     migration. The LIVE migrator is `courierledger` (the LOGIN
--     owner role): it owns the databases and all 33 tables in each,
--     is not a superuser, cannot BYPASSRLS, and is the role
--     MIGRATION_DATABASE_URL names. The migrator role is kept
--     NOLOGIN with NO password verifier (nulled below) so it cannot
--     be logged into even by accident; it is reserved for a future
--     production split (separate migration credential) if the
--     founder ever wants one — that split is a deployment change
--     (new vault password + MIGRATION_DATABASE_URL + REASSIGN OWNED),
--     not a redesign, and it needs no schema change.
--     WHY Option 2 over Option 1 (REASSIGN OWNED to the migrator):
--     every stated requirement is already met by `courierledger` as
--     migrator — runtime holds no migration privilege, owns nothing,
--     cannot BYPASSRLS; FORCE RLS stays enabled; migration privilege
--     is sufficient (owner); maintenance stays restricted; no app
--     connection uses a superuser. Option 1 would add a second
--     LOGIN credential to store/rotate (vault, .env, CI secret) and
--     force REASSIGN OWNED on every database plus changes to the
--     provisioning script, the live-test harness and both CI
--     workflows, while buying no live risk reduction on this
--     single-VM local-dev setup, where all credentials already live
--     in one gitignored .env held by one person. The operative
--     boundary — app connects as runtime, migrations run as owner —
--     is enforced by configuration (DATABASE_URL names the runtime
--     role; alembic/env.py reads MIGRATION_DATABASE_URL only) and
--     is proven by the Phase 3 live tests, not by the role's name.
--     Revisit if/when a production host with a real vault exists.
--   courierledger_runtime     -- the ONLY role the application uses at
--     runtime. Non-owner, non-superuser, NOBYPASSRLS (explicit). FORCE
--     RLS applies to it (and to the owner) on every tenant table.
--   courierledger_maintenance -- break-glass / scheduled maintenance:
--     idempotency purge, sweeper transitions, workspace Stage 2
--     anonymisation, anonymised exports. NOLOGIN except during a
--     controlled maintenance window.
--
-- FRESH-CLUSTER PROVISIONING ORDER (Phase 1.1 re-verification, proven
-- live 2026-11-01): create the NOLOGIN roles first (the DO block
-- below), then Alembic, then rls.sql + triggers.sql, then this file,
-- then seed. rls.sql first fails on the missing maintenance role;
-- this file first fails on the missing app_current_merchant_id().
-- See m1/scripts/provision_db.sh, which encodes this order.
--
-- ROLE-ENFORCED vs DOCUMENTED-ONLY (local single-server setup):
--   Role-enforced by this file (once applied by a superuser):
--     * the runtime role cannot BYPASSRLS, cannot CREATE/DROP/ALTER
--       objects, cannot UPDATE/DELETE append-only tables, cannot DELETE
--       idempotency_keys, cannot read or write global tables, and can
--       UPDATE reconciliation_runs only on (status, completed_at);
--     * the maintenance role is the only role that can DELETE
--       idempotency_keys (purge) and UPDATE merchants/users for Stage 2.
--   Documented-only (NOT enforced by this file, needs deployment work):
--     * that the application actually connects as courierledger_runtime
--       (today it connects with whatever DATABASE_URL says — in this
--       repo DATABASE_URL names the runtime role, and the app never
--       reads MIGRATION_DATABASE_URL; see app/config.py);
--     * that migrations actually connect as `courierledger`, the live
--       migrator (MIGRATION_DATABASE_URL names it; alembic/env.py
--       reads that variable and never the runtime URL). The reserved
--       courierledger_migrator role plays no part in this.
--     * that no pooled connection ever carries a previous tenant's
--       context (transaction-local set_config + pool reset is an
--       application/pooler duty, CL-11/T-06);
--     * that the migrator password / maintenance credentials are stored
--       in the Secure Vault and never in code or .env files committed
--       to the repo.
--   Until those deployment steps happen, the grants below are a
--   specification of the intended posture, not a live enforcement.
--   Mark them UNVERIFIED in README.md until applied and tested on live PG.
--
-- Passwords: this file deliberately contains NO passwords. Create each
-- role with a password supplied at provisioning time, e.g.
--   CREATE ROLE courierledger_runtime LOGIN PASSWORD '<from vault>';
-- and store it in the Secure Vault. Never commit a real password here.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Role creation (idempotent-ish: CREATE ROLE has no IF NOT EXISTS in
-- older PostgreSQL; the DO blocks below make re-application safe).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'courierledger_migrator') THEN
    CREATE ROLE courierledger_migrator NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'courierledger_runtime') THEN
    CREATE ROLE courierledger_runtime LOGIN NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'courierledger_maintenance') THEN
    CREATE ROLE courierledger_maintenance NOLOGIN;
  END IF;
END $$;

-- The runtime role must never bypass RLS, even if a later GRANT or role
-- change tries to give it that power (CL-22 rule 6: no general-purpose
-- RLS bypass). The migrator (owner) is subject to FORCE RLS as well.
ALTER ROLE courierledger_runtime NOBYPASSRLS;
ALTER ROLE courierledger_maintenance NOBYPASSRLS;
ALTER ROLE courierledger_migrator NOBYPASSRLS;

-- NOLOGIN-role password verifiers (Phase 3 decision, 2026-11-02):
-- courierledger_migrator and courierledger_maintenance were
-- RECORDED in Phase 2 as carrying password verifiers despite
-- NOLOGIN — but that record rested on pg_roles, whose rolpassword
-- column is a constant '********' mask for every role (verified
-- live 2026-11-02 from the pg_roles view definition); it cannot
-- distinguish "has a verifier" from "has none". The correct source
-- is pg_authid (superuser-only). Whatever was there before, this
-- file now makes the state deterministic: both roles are set
-- PASSWORD NULL on every application (verified live in pg_authid
-- after the Phase 3 application). NOLOGIN blocked login either
-- way, so this was not an active hole — but a stored verifier
-- would have gone live the moment LOGIN was ever flipped, and the
-- plaintext behind any earlier verifier is not recorded anywhere
-- (the current provisioning scripts create both roles WITHOUT a
-- password). DECIDED: null them. Break-glass access to either role
-- goes through the postgres superuser (su postgres), which can SET
-- ROLE or grant LOGIN temporarily under a reviewed change; no
-- standing password exists to leak, rotate, or forget. This
-- statement is idempotent and runs on every provisioning
-- (roles.sql is applied by a superuser); a future production split
-- that puts the migrator into service MUST set a fresh vault
-- password at that point — never re-use a value that was nulled
-- here. If either ALTER below ever fails, provisioning fails
-- loudly (ON_ERROR_STOP=1); do not work around it by giving the
-- role a password.
ALTER ROLE courierledger_migrator PASSWORD NULL;
ALTER ROLE courierledger_maintenance PASSWORD NULL;

-- ---------------------------------------------------------------------------
-- Schema posture. PostgreSQL 15+ already revokes CREATE on public from
-- PUBLIC; the REVOKE below states the intended posture explicitly for
-- older servers and is harmless on newer ones.
-- ---------------------------------------------------------------------------
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO courierledger_runtime, courierledger_maintenance;
-- The owner role (`courierledger`, the live migrator) owns the schema
-- objects; ownership is set at creation time by running migrations as
-- that role. The reserved courierledger_migrator role holds no grants
-- and owns nothing (Phase 3 decision, see header).

-- ---------------------------------------------------------------------------
-- Global reference tables: read-only for the runtime role (v0.1 C.2).
-- No INSERT/UPDATE/DELETE/TRUNCATE. Adding a courier code is a migration.
-- ---------------------------------------------------------------------------
GRANT SELECT ON public.couriers TO courierledger_runtime;
GRANT SELECT ON public.courier_statement_templates TO courierledger_runtime;
GRANT SELECT ON public.column_mappings TO courierledger_runtime;
GRANT SELECT ON public.couriers TO courierledger_maintenance;
GRANT SELECT ON public.courier_statement_templates TO courierledger_maintenance;
GRANT SELECT ON public.column_mappings TO courierledger_maintenance;

-- ---------------------------------------------------------------------------
-- Identity tables: the runtime role reads/writes users, user_sessions and
-- password_reset_tokens only through the auth service's own paths (no
-- merchant RLS policy exists on them by design, v0.1 C.1). Grants here
-- are deliberately narrow: no DELETE on users (a user row is anonymised
-- in place under CL-15, never deleted while history references it).
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON public.users TO courierledger_runtime;
GRANT SELECT, INSERT, UPDATE ON public.user_sessions TO courierledger_runtime;
GRANT SELECT, INSERT, UPDATE ON public.password_reset_tokens TO courierledger_runtime;
GRANT SELECT, INSERT, UPDATE ON public.users TO courierledger_maintenance;
GRANT SELECT, INSERT, UPDATE ON public.user_sessions TO courierledger_maintenance;
GRANT SELECT, INSERT, UPDATE ON public.password_reset_tokens TO courierledger_maintenance;
-- No DELETE grant on users / user_sessions / password_reset_tokens for
-- either application role. Session expiry cleanup is a maintenance
-- DELETE under a controlled procedure, not a standing grant; see the
-- note at the end of this file.

-- ---------------------------------------------------------------------------
-- Tenant tables -- runtime grants.
-- Mutable tables: SELECT, INSERT, UPDATE only -- DELETE is granted
-- to the runtime role on NONE of the tenant tables. In particular
-- there is NO DELETE grant on merchant_memberships (an earlier
-- version of this comment claimed otherwise): revocation removes no
-- row, it is an UPDATE to status='revoked', and the
-- merchant_memberships policies carry no DELETE policy either.
-- Append-only tables: SELECT + INSERT only. No UPDATE, no DELETE.
-- ---------------------------------------------------------------------------

-- merchants (tenant root): read + controlled insert; no runtime UPDATE
-- or DELETE (closure/anonymisation are maintenance operations, CL-12/15).
GRANT SELECT, INSERT ON public.merchants TO courierledger_runtime;

-- Mutable tenant tables (SELECT, INSERT, UPDATE; DELETE withheld --
-- corrections create new records or runs, never delete history).
GRANT SELECT, INSERT, UPDATE ON public.merchant_memberships TO courierledger_runtime;
GRANT SELECT, INSERT, UPDATE ON public.merchant_courier_accounts TO courierledger_runtime;
GRANT SELECT, INSERT, UPDATE ON public.courier_rule_versions TO courierledger_runtime;
GRANT SELECT, INSERT, UPDATE ON public.orders TO courierledger_runtime;
GRANT SELECT, INSERT, UPDATE ON public.shipments TO courierledger_runtime;
GRANT SELECT, INSERT, UPDATE ON public.import_batches TO courierledger_runtime;
GRANT SELECT, INSERT, UPDATE ON public.settlement_batches TO courierledger_runtime;
GRANT SELECT, INSERT, UPDATE ON public.bank_receipts TO courierledger_runtime;
GRANT SELECT, INSERT, UPDATE ON public.workspace_settings TO courierledger_runtime;

-- Append-only tables (SELECT + INSERT only -- CL-03/CL-10/CL-16).
GRANT SELECT, INSERT ON public.courier_rule_evidence TO courierledger_runtime;
GRANT SELECT, INSERT ON public.file_objects TO courierledger_runtime;
GRANT SELECT, INSERT ON public.import_rows TO courierledger_runtime;
GRANT SELECT, INSERT ON public.import_errors TO courierledger_runtime;
GRANT SELECT, INSERT ON public.statement_lines TO courierledger_runtime;
GRANT SELECT, INSERT ON public.shipment_match_candidates TO courierledger_runtime;
GRANT SELECT, INSERT ON public.shipment_match_decisions TO courierledger_runtime;
GRANT SELECT, INSERT ON public.reconciliation_results TO courierledger_runtime;
GRANT SELECT, INSERT ON public.reconciliation_fee_lines TO courierledger_runtime;
GRANT SELECT, INSERT ON public.reconciliation_findings TO courierledger_runtime;
GRANT SELECT, INSERT ON public.settlement_allocations TO courierledger_runtime;
GRANT SELECT, INSERT ON public.settlement_variance_calculations TO courierledger_runtime;
GRANT SELECT, INSERT ON public.settlement_variance_calculation_inputs TO courierledger_runtime;
GRANT SELECT, INSERT ON public.audit_logs TO courierledger_runtime;

-- reconciliation_runs: SELECT + INSERT, plus a NARROW column-level
-- UPDATE grant on (status, completed_at) ONLY (v0.1 C.5). No DELETE.
-- The guard trigger in triggers.sql rejects any other column change.
GRANT SELECT, INSERT ON public.reconciliation_runs TO courierledger_runtime;
GRANT UPDATE (status, completed_at) ON public.reconciliation_runs TO courierledger_runtime;

-- idempotency_keys: SELECT + INSERT + UPDATE (the sanctioned lifecycle
-- updates -- claim/reclaim/completion/failure, CL-04/CL-14). No DELETE:
-- purge is maintenance-role only (CL-19).
GRANT SELECT, INSERT, UPDATE ON public.idempotency_keys TO courierledger_runtime;

-- ---------------------------------------------------------------------------
-- Tenant tables -- maintenance grants.
-- Maintenance may SELECT everything (read-only except where named), and:
--   * DELETE idempotency_keys (purge, CL-19 -- only completed/failed
--     rows past expires_at; the predicate is in the purge procedure,
--     not in the grant);
--   * UPDATE merchants / merchant_courier_accounts / users (Stage 2
--     anonymisation, CL-15 -- in place, in one maintenance transaction);
--   * UPDATE idempotency_keys (sweeper transition to failed, CL-19);
--   * UPDATE file_objects (purged_at, CL-15/G).
-- Maintenance may NOT DELETE or UPDATE any financial-history table.
-- ---------------------------------------------------------------------------
GRANT SELECT ON ALL TABLES IN SCHEMA public TO courierledger_maintenance;
GRANT DELETE ON public.idempotency_keys TO courierledger_maintenance;
GRANT UPDATE ON public.idempotency_keys TO courierledger_maintenance;
GRANT UPDATE ON public.merchants TO courierledger_maintenance;
GRANT UPDATE ON public.merchant_courier_accounts TO courierledger_maintenance;
GRANT UPDATE ON public.file_objects TO courierledger_maintenance;
GRANT UPDATE ON public.orders TO courierledger_maintenance;
-- Orders are mutable-class rows whose identifying fields may be
-- scrubbed in Stage 2 (CL-15); the grant above is for that purpose only.

-- ---------------------------------------------------------------------------
-- Functions. The runtime role may call the tenant-context helper only
-- indirectly through set_config; it must NOT be able to call the
-- validation functions except as triggers fire them. No EXECUTE grants
-- on validation functions are given to the runtime role here: trigger
-- functions execute as their owner (the migrator) when the trigger
-- fires, which is the CL-22 pattern. EXECUTE on
-- public.app_current_merchant_id() is granted to the runtime role
-- because RLS policies call it on every tenant query. The same holds
-- for public.app_current_user_id() (B-1 fix: the membership-based
-- merchants SELECT policy and the merchant_memberships SELECT policy
-- call it). The same in-file grant pattern holds for
-- public.app_current_user_role() (Phase 1.1, B-F7 fix): REVOKE FROM
-- PUBLIC plus GRANT TO courierledger_runtime / maintenance live in
-- rls.sql next to its definition, not here, so the grant travels
-- with the function. EXECUTE on public.create_workspace() is granted in rls.sql
-- next to the function definition (runtime role only, PUBLIC revoked);
-- it is not repeated here, so the grant travels with the function.
-- ---------------------------------------------------------------------------
REVOKE EXECUTE ON FUNCTION public.app_current_merchant_id() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.app_current_user_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.app_current_merchant_id() TO courierledger_runtime;
GRANT EXECUTE ON FUNCTION public.app_current_merchant_id() TO courierledger_maintenance;
GRANT EXECUTE ON FUNCTION public.app_current_user_id() TO courierledger_runtime;
GRANT EXECUTE ON FUNCTION public.app_current_user_id() TO courierledger_maintenance;

-- ---------------------------------------------------------------------------
-- Idempotency lifecycle predicates (CL-19/CL-24) -- DOCUMENTED HERE as
-- the authoritative predicate table; they are executed by the
-- application as single conditional UPDATEs, not by a trigger.
-- Common identity conditions (every row): (merchant_id, operation,
-- idempotency_key) match the request AND request_hash matches; a hash
-- mismatch is the CL-04 conflict error in every state.
-- LEASE CLOCK: every lease comparison below uses clock_timestamp()
-- (statement-execution wall clock), NEVER now() (transaction-start
-- time). A worker that lets its lease lapse mid-operation loses the
-- operation; completion is the last write of the work transaction and
-- a zero-row completion rolls the whole work transaction back (CL-24).
--
--   Lease renewal:       status='in_progress' AND claim_token=:token
--                        AND lease_expires_at > clock_timestamp().
--                        Effect: lease_expires_at = clock_timestamp()
--                        + lease_duration. Zero rows: ownership lost --
--                        stop and roll back; do not complete.
--   Reclaim (retry):     status='in_progress' AND
--                        lease_expires_at <= clock_timestamp() AND
--                        attempt_count < attempt_cap (default 5).
--                        Effect: fresh claim_token, new lease_expires_at,
--                        attempt_count + 1, reclaiming instance recorded.
--   Cap transition:      status='in_progress' AND
--                        lease_expires_at <= clock_timestamp() AND
--                        attempt_count >= attempt_cap. Effect: failed,
--                        lease_expires_at = NULL, token retained for audit.
--   Completion:          status='in_progress' AND claim_token=:token
--                        AND lease_expires_at > clock_timestamp()
--                        (CL-24 uniform ownership rule -- the lease
--                        condition is REQUIRED, not optional).
--                        Effect: completed, completed_at set, typed result
--                        reference set, lease_expires_at = NULL.
--   Worker failure:      status='in_progress' AND claim_token=:token
--                        AND lease_expires_at > clock_timestamp().
--                        Effect: failed, lease_expires_at = NULL.
--   Sweeper failure:     status='in_progress' AND
--                        lease_expires_at <= clock_timestamp().
--                        Maintenance role only. Effect: failed, lease NULL.
--   Purge:               status IN ('completed','failed') AND
--                        expires_at <= clock_timestamp().
--                        Maintenance role only; never a runtime grant.
--                        Deletes the key row only -- never the batch/run.
--   A live in_progress row (lease unexpired) is NEVER purgeable.
--
-- Per-user advisory lock (CL-20/CL-25): membership creation, invitation
-- creation/acceptance, reactivation, eligibility-affecting status
-- changes, session creation/renewal/refresh/extension/reactivation and
-- Stage 2 anonymisation must all take pg_advisory_xact_lock keyed by
-- user_id before reading or writing protected state. Pure revocation /
-- deletion of a session explicitly does NOT take the lock. This is an
-- application-protocol duty, not a grant; it is stated here so the
-- protocol lives next to the roles that rely on it.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Per-role timeouts (Phase 2, applied live 2026-11-01, verified via
-- pg_roles.rolconfig). No restart needed. These bound runaway queries
-- and leaked idle transactions: an idle transaction holds locks and
-- can block the CL-20 advisory-lock protocol, which is
-- transaction-scoped. The 30s runtime statement bound was chosen
-- after the 50k-row CSV import and run creation were observed to
-- complete well inside it; if a legitimate operation ever exceeds
-- 30s, raise the timeout for that path only (SET LOCAL in that
-- transaction), never globally.
-- ---------------------------------------------------------------------------
ALTER ROLE courierledger_runtime SET statement_timeout = '30s';
ALTER ROLE courierledger_runtime SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE courierledger_runtime SET lock_timeout = '5s';

ALTER ROLE courierledger_maintenance SET statement_timeout = '10min';
ALTER ROLE courierledger_maintenance SET idle_in_transaction_session_timeout = '10min';
ALTER ROLE courierledger_maintenance SET lock_timeout = '30s';

-- ============================================================================
-- Notes / open items for the coordinator
-- 1. No passwords are set in this file (see header). Create the roles
--    with vault-supplied passwords at provisioning time.
-- 2. Session-expiry cleanup (DELETE on user_sessions / password_reset_
--    tokens) has no standing grant by design; if a cleanup job needs
--    one, grant it to the maintenance role in a reviewed change, scoped
--    to expired rows only.
-- 3. Until the application is configured with separate DATABASE_URLs
--    per role, these grants are a specification, not live enforcement
--    (see "Role-enforced vs documented-only" in the header).
-- 4. NOLOGIN-role passwords: DECIDED Phase 3 (2026-11-02) — nulled.
--    Phase 2 recorded both roles as carrying password verifiers,
--    but that record rested on pg_roles, whose rolpassword column
--    is a constant '********' mask for every role and cannot
--    distinguish "has a verifier" from "has none" (verified live
--    2026-11-02 from the view definition). The correct source is
--    pg_authid (superuser-only); both roles are now PASSWORD NULL
--    there, verified live. Break-glass goes through the postgres
--    superuser. Do not set a password on either role and do not
--    flip LOGIN on either role without a reviewed change. The one
--    exception is a future production split that puts the migrator
--    into service (see header): that change sets a FRESH vault
--    password at the time, never a value that was nulled here.
-- ============================================================================
