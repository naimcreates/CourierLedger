-- ============================================================================
-- CourierLedger M1 -- Row-Level Security (rls.sql)
-- ============================================================================
-- APPLY BY: the migrator role, as a separate reviewed step AFTER migration
-- 0001 (which already ENABLEs + FORCEs RLS on the tenant root and every
-- tenant-owned table). This file is NOT executed by the application or by
-- Alembic; it is applied deliberately, once, and re-applied only by review.
--
-- Authority: v0.1 Section E, v0.2 CL-06 (malformed tenant UUID fails
-- closed), v0.2 CL-11 (trust boundary), v0.3 CL-15 (tombstone isolation),
-- v0.5 CL-20/CL-25 (advisory-lock protocols -- application level, see
-- README.md; they are not RLS policies and are not expressed here).
--
-- TENANT CONTEXT
-- The application sets, transaction-local, on EVERY transaction, from an
-- authenticated workspace resolution (never from an unvalidated
-- client-supplied value -- CL-11):
--     SELECT set_config('app.current_merchant_id', '<uuid>', true);
--     SELECT set_config('app.user_id', '<uuid>', true);
-- The setting name 'app.current_merchant_id' is the one already used by
-- app/db/session.py:set_tenant_context() (auth agent). NOTE: v0.1 E.2
-- names the setting 'app.merchant_id'; this build uses
-- 'app.current_merchant_id' everywhere (session.py, this file, README.md)
-- so the app and the database agree. Do not mix the two names.
-- The user setting is 'app.user_id' exactly as v0.1 E.2 names it; there
-- is no competing brief name for it, so no 'current_' variant exists.
-- The asymmetry (app.current_merchant_id vs app.user_id) is deliberate
-- and recorded in IMPLEMENTATION_CHECKLIST.md discrepancy 2.
-- There is NO default tenant anywhere (CL-06/CL-11).
--
-- FAIL-CLOSED: app_current_merchant_id() returns NULL for unset, empty,
-- whitespace or malformed values and NEVER raises inside a policy
-- (CL-06). A NULL right-hand side matches no rows for SELECT/UPDATE/
-- DELETE and rejects INSERT via WITH CHECK.
--
-- POLICY SHAPE (uniform, one column everywhere -- v0.1 A.7):
--   USING (merchant_id = app_current_merchant_id())
--   WITH CHECK (merchant_id = app_current_merchant_id())
-- Append-only tables get SELECT + INSERT policies ONLY: no UPDATE or
-- DELETE policy exists for the runtime role, so such statements affect
-- zero rows (backstopped by the append-only triggers in triggers.sql and
-- by the grants in roles.sql). Exceptions are documented inline:
-- reconciliation_runs (status/completed_at only, column-level grant) and
-- idempotency_keys (sanctioned lifecycle updates, CL-04/CL-14).
--
-- TABLES WITH NO MERCHANT RLS POLICY (deliberate, v0.1 C.1/C.2):
--   users, user_sessions, password_reset_tokens -- Identity class; access
--     is controlled by authentication and membership, not by a merchant
--     policy. Enabling RLS with no policy here would deny the auth
--     service its own reads (login looks up users by email).
--   couriers, courier_statement_templates -- Global reference data;
--     read-only for the runtime role via GRANTs in roles.sql.
--   merchants -- HAS a membership-based SELECT policy below (v0.1 E.3,
--     Blocker B-1 fix, 2026-10-31): a user sees a workspace only where
--     an active membership row links them (checked by an EXISTS subquery
--     against merchant_memberships, whose own SELECT policy lets a
--     user's OWN rows through -- so the subquery works without any RLS
--     bypass). There is NO general INSERT policy on merchants:
--     workspace creation runs ONLY through the controlled function
--     public.create_workspace() below. Direct INSERT by the runtime
--     role is denied (no policy -> default deny).
--   merchant_memberships -- SELECT is user-based OR merchant-based
--     (own rows, or all rows of the context merchant); INSERT/UPDATE
--     are merchant-context only, so a user cannot grant themselves a
--     membership in a workspace they do not belong to.
--   audit_logs -- merchant_id is NULL for pre-workspace identity events;
--     the SELECT policy requires merchant_id = context, so NULL-merchant
--     rows are invisible to tenant sessions (fail-closed). INSERT allows
--     NULL merchant_id for sign-in events; see the policy comment below.
--
-- FORCE RLS: ENABLE + FORCE on every tenant table EXCEPT merchants and
-- merchant_memberships (B-1 fix). Those two tables are ENABLE + NO FORCE:
-- the controlled create_workspace() function is owned by the table-owner
-- role (the role that runs migrations in this environment --
-- courierledger; in a production setup, the migrator role that owns the
-- schema), and a table owner is subject to RLS only under FORCE. With
-- NO FORCE the owner-side function can insert the merchant + owner
-- membership atomically without a tenant context existing yet, while the
-- runtime role -- which is never the table owner -- remains fully
-- policed by the policies below (verified live: direct INSERT by the
-- runtime role is denied, and SELECT returns only the caller's own
-- workspaces). The owner role is migration/admin-only and could disable
-- RLS outright, so NO FORCE on these two identity-root tables removes
-- no protection against the untrusted (runtime) role. All other tenant
-- tables keep FORCE. The initial migration still writes FORCE for all
-- 26 tables; this file, applied after the migration as the reviewed RLS
-- step, relaxes it for these two tables only. See
-- IMPLEMENTATION_CHECKLIST.md Blocker B-1 for the probe evidence
-- (including why SET row_security TO off does NOT bypass RLS -- it
-- makes affected queries raise instead -- and why a SECURITY DEFINER
-- helper in a policy cannot read merchant_memberships without the
-- user-based policy arm used here).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Tenant-context helper (CL-06). STABLE, never raises, NULL on malformed.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.app_current_merchant_id()
RETURNS uuid
LANGUAGE sql
STABLE
SET search_path TO public, pg_catalog
AS $$
  SELECT CASE
    WHEN current_setting('app.current_merchant_id', true) ~
         '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    THEN current_setting('app.current_merchant_id', true)::uuid
    ELSE NULL
  END
$$;

COMMENT ON FUNCTION public.app_current_merchant_id() IS
  'Tenant context for RLS (CL-06/CL-11). Returns the UUID from the '
  'transaction-local app.current_merchant_id setting, or NULL for unset, '
  'empty or malformed values. Never raises inside a policy.';

-- ---------------------------------------------------------------------------
-- User-context helper (CL-06, same shape as the merchant helper).
-- v0.1 E.2 sets app.user_id alongside the merchant id; the app sets it
-- transaction-local from the authenticated session on every request
-- (app/db/session.py:set_user_context(), called from api/deps.py).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.app_current_user_id()
RETURNS uuid
LANGUAGE sql
STABLE
SET search_path TO public, pg_catalog
AS $$
  SELECT CASE
    WHEN current_setting('app.user_id', true) ~
         '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    THEN current_setting('app.user_id', true)::uuid
    ELSE NULL
  END
$$;

COMMENT ON FUNCTION public.app_current_user_id() IS
  'User context for RLS (CL-06/CL-11, v0.1 E.2). Returns the UUID from '
  'the transaction-local app.user_id setting, or NULL for unset, empty '
  'or malformed values. Never raises inside a policy.';

-- ============================================================================
-- merchants (tenant root): membership-based SELECT (v0.1 E.3, B-1 fix).
-- A user sees a workspace only where an active membership row links
-- them. The EXISTS subquery reads merchant_memberships under that
-- table's own user-based SELECT policy (see below), so it returns the
-- caller's own rows with no RLS bypass anywhere.
-- NO INSERT policy: creation runs ONLY through public.create_workspace()
-- below (v0.1 E.3 "rather than a general INSERT policy"). NO FORCE on
-- this table (see the header note): the owner-side creation function
-- inserts without a tenant context; the runtime role is still denied
-- direct INSERT (no policy -> default deny) and still policed on
-- SELECT by the policy below.
-- ============================================================================
DROP POLICY IF EXISTS merchants_tenant_select ON public.merchants;
CREATE POLICY merchants_tenant_select ON public.merchants
  FOR SELECT USING (
    EXISTS (
      SELECT 1 FROM public.merchant_memberships mm
      WHERE mm.merchant_id = merchants.id
        AND mm.user_id = public.app_current_user_id()
        AND mm.status = 'active'
    )
  );
DROP POLICY IF EXISTS merchants_tenant_insert ON public.merchants;
-- (Intentionally NOT recreated: see above. Direct merchant INSERT by
-- any non-owner role is denied; creation is the function below.)
-- No UPDATE/DELETE policy for the runtime role on merchants: workspace
-- mutation and closure are controlled functions / maintenance operations
-- (CL-12/CL-15), not ordinary runtime writes. The migrator role (table
-- owner) is subject to FORCE RLS too; maintenance uses its own role
-- (see roles.sql) and explicit procedures.
ALTER TABLE public.merchants NO FORCE ROW LEVEL SECURITY;

-- Index for the membership-based merchants SELECT policy (and for the
-- app's list/get flows): look up a user's active memberships by user.
CREATE INDEX IF NOT EXISTS ix_merchant_memberships_user_active
  ON public.merchant_memberships (user_id, status, merchant_id);

-- ---------------------------------------------------------------------------
-- Controlled workspace creation (v0.1 E.3, B-1 fix).
-- SECURITY DEFINER, owned by the role that applies this file -- the
-- table-owner / migration role in this environment (courierledger; in a
-- production setup, the migrator role that owns the schema). Ownership
-- by the table owner is load-bearing, not cosmetic: with NO FORCE on
-- merchants / merchant_memberships the owner bypasses RLS on those two
-- tables, which is what lets this function insert a merchant + owner
-- membership atomically before any tenant context exists. A function
-- owned by a non-owner role could not do that (FORCE or not, a
-- non-owner is policed; SET row_security TO off does NOT bypass RLS --
-- it makes affected queries raise -- verified by probe 2026-10-31).
-- CL-26 hardening: function-level SET search_path to trusted schemas
-- only, all objects schema-qualified, no dynamic SQL, EXECUTE revoked
-- from PUBLIC and granted only to the runtime role (see below).
-- The function validates the caller (user must exist and be active)
-- and the name (1-200 chars, matching the merchants CHECK), then
-- inserts the merchant and the creator's owner membership (status
-- active) in one transaction -- the caller rolls back on any error, so
-- a merchant is never created without its owner membership. Timestamps
-- are set explicitly with now(): the identity tables use app-side
-- defaults, so a raw SQL INSERT must supply created_at/updated_at.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.create_workspace(
  p_user_id uuid,
  p_name text,
  p_trading_name text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public, pg_catalog
AS $$
DECLARE
  v_merchant_id uuid := gen_random_uuid();
BEGIN
  IF p_user_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM public.users u
    WHERE u.id = p_user_id AND u.status = 'active'
  ) THEN
    RAISE EXCEPTION 'create_workspace: unknown or inactive user';
  END IF;
  IF p_name IS NULL OR length(p_name) < 1 OR length(p_name) > 200 THEN
    RAISE EXCEPTION 'create_workspace: name must be 1-200 chars';
  END IF;
  IF p_trading_name IS NOT NULL AND length(p_trading_name) > 200 THEN
    RAISE EXCEPTION 'create_workspace: trading_name must be at most 200 chars';
  END IF;
  INSERT INTO public.merchants
    (id, name, trading_name, status, created_by_user_id, created_at, updated_at)
    VALUES (v_merchant_id, p_name, p_trading_name, 'active', p_user_id, now(), now());
  INSERT INTO public.merchant_memberships
    (id, merchant_id, user_id, role, status, invited_by_user_id, created_at, updated_at)
    VALUES (gen_random_uuid(), v_merchant_id, p_user_id, 'owner', 'active', p_user_id, now(), now());
  RETURN v_merchant_id;
END
$$;

COMMENT ON FUNCTION public.create_workspace(uuid, text, text) IS
  'Controlled workspace creation (v0.1 E.3, B-1 fix). Creates a merchant '
  'and its creator owner membership atomically. The ONLY way to create a '
  'merchant: there is no general INSERT policy on merchants. SECURITY '
  'DEFINER, table-owner owned, search_path hardened (CL-26).';

REVOKE ALL ON FUNCTION public.create_workspace(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.create_workspace(uuid, text, text) TO courierledger_runtime;

-- ============================================================================
-- Tenant-owned tables: SELECT + INSERT (+ UPDATE/DELETE where mutable).
-- Generated pattern; the table list is TENANT_TABLES in app/db/models.py.
-- Append-only tables intentionally have NO update/delete policy.
-- ============================================================================

-- merchant_memberships (B-1 fix): SELECT is user-based OR
-- merchant-based -- a user reads their own membership rows (this is
-- what the merchants membership-based SELECT subquery above relies on,
-- and what the app's list/get flows query), and a merchant-context
-- session reads all memberships of that merchant. INSERT/UPDATE stay
-- merchant-context only: a user cannot grant themselves (or anyone) a
-- membership in a workspace they do not belong to. No DELETE policy:
-- the runtime role has no DELETE grant here (roles.sql); revocation is
-- an UPDATE to status='revoked'. NO FORCE (see the header note): the
-- owner-side create_workspace() function inserts the owner membership.
DROP POLICY IF EXISTS merchant_memberships_tenant_all ON public.merchant_memberships;
CREATE POLICY merchant_memberships_tenant_select ON public.merchant_memberships
  FOR SELECT USING (
    user_id = public.app_current_user_id()
    OR merchant_id = public.app_current_merchant_id()
  );
CREATE POLICY merchant_memberships_tenant_insert ON public.merchant_memberships
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());
CREATE POLICY merchant_memberships_tenant_update ON public.merchant_memberships
  FOR UPDATE USING (merchant_id = public.app_current_merchant_id())
  WITH CHECK (merchant_id = public.app_current_merchant_id());
ALTER TABLE public.merchant_memberships NO FORCE ROW LEVEL SECURITY;

-- merchant_courier_accounts (mutable, audited)
DROP POLICY IF EXISTS merchant_courier_accounts_tenant_all ON public.merchant_courier_accounts;
CREATE POLICY merchant_courier_accounts_tenant_all ON public.merchant_courier_accounts
  FOR ALL USING (merchant_id = public.app_current_merchant_id())
  WITH CHECK (merchant_id = public.app_current_merchant_id());

-- courier_rule_versions (versioned; in-place edit of an active version is
-- blocked by the trigger in triggers.sql, not by the policy)
DROP POLICY IF EXISTS courier_rule_versions_tenant_all ON public.courier_rule_versions;
CREATE POLICY courier_rule_versions_tenant_all ON public.courier_rule_versions
  FOR ALL USING (merchant_id = public.app_current_merchant_id())
  WITH CHECK (merchant_id = public.app_current_merchant_id());

-- courier_rule_evidence (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS courier_rule_evidence_tenant_select ON public.courier_rule_evidence;
CREATE POLICY courier_rule_evidence_tenant_select ON public.courier_rule_evidence
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS courier_rule_evidence_tenant_insert ON public.courier_rule_evidence;
CREATE POLICY courier_rule_evidence_tenant_insert ON public.courier_rule_evidence
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- orders (mutable before reconciliation; runs snapshot what they used)
DROP POLICY IF EXISTS orders_tenant_all ON public.orders;
CREATE POLICY orders_tenant_all ON public.orders
  FOR ALL USING (merchant_id = public.app_current_merchant_id())
  WITH CHECK (merchant_id = public.app_current_merchant_id());

-- shipments (mutable, outcome changes audited)
DROP POLICY IF EXISTS shipments_tenant_all ON public.shipments;
CREATE POLICY shipments_tenant_all ON public.shipments
  FOR ALL USING (merchant_id = public.app_current_merchant_id())
  WITH CHECK (merchant_id = public.app_current_merchant_id());

-- file_objects (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS file_objects_tenant_select ON public.file_objects;
CREATE POLICY file_objects_tenant_select ON public.file_objects
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS file_objects_tenant_insert ON public.file_objects;
CREATE POLICY file_objects_tenant_insert ON public.file_objects
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- import_batches (status/counts mutable during import; mapping_snapshot,
-- file and account references immutable after parsing -- application rule)
DROP POLICY IF EXISTS import_batches_tenant_all ON public.import_batches;
CREATE POLICY import_batches_tenant_all ON public.import_batches
  FOR ALL USING (merchant_id = public.app_current_merchant_id())
  WITH CHECK (merchant_id = public.app_current_merchant_id());

-- import_rows (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS import_rows_tenant_select ON public.import_rows;
CREATE POLICY import_rows_tenant_select ON public.import_rows
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS import_rows_tenant_insert ON public.import_rows;
CREATE POLICY import_rows_tenant_insert ON public.import_rows
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- import_errors (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS import_errors_tenant_select ON public.import_errors;
CREATE POLICY import_errors_tenant_select ON public.import_errors
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS import_errors_tenant_insert ON public.import_errors;
CREATE POLICY import_errors_tenant_insert ON public.import_errors
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- statement_lines (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS statement_lines_tenant_select ON public.statement_lines;
CREATE POLICY statement_lines_tenant_select ON public.statement_lines
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS statement_lines_tenant_insert ON public.statement_lines;
CREATE POLICY statement_lines_tenant_insert ON public.statement_lines
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- shipment_match_candidates (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS shipment_match_candidates_tenant_select ON public.shipment_match_candidates;
CREATE POLICY shipment_match_candidates_tenant_select ON public.shipment_match_candidates
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS shipment_match_candidates_tenant_insert ON public.shipment_match_candidates;
CREATE POLICY shipment_match_candidates_tenant_insert ON public.shipment_match_candidates
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- shipment_match_decisions (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS shipment_match_decisions_tenant_select ON public.shipment_match_decisions;
CREATE POLICY shipment_match_decisions_tenant_select ON public.shipment_match_decisions
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS shipment_match_decisions_tenant_insert ON public.shipment_match_decisions;
CREATE POLICY shipment_match_decisions_tenant_insert ON public.shipment_match_decisions
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- reconciliation_runs (append-only except status/completed_at: SELECT +
-- INSERT policies here; the UPDATE path is a column-level grant in
-- roles.sql plus the guard trigger in triggers.sql)
DROP POLICY IF EXISTS reconciliation_runs_tenant_select ON public.reconciliation_runs;
CREATE POLICY reconciliation_runs_tenant_select ON public.reconciliation_runs
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS reconciliation_runs_tenant_insert ON public.reconciliation_runs;
CREATE POLICY reconciliation_runs_tenant_insert ON public.reconciliation_runs
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS reconciliation_runs_tenant_update ON public.reconciliation_runs;
CREATE POLICY reconciliation_runs_tenant_update ON public.reconciliation_runs
  FOR UPDATE USING (merchant_id = public.app_current_merchant_id())
  WITH CHECK (merchant_id = public.app_current_merchant_id());

-- reconciliation_results (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS reconciliation_results_tenant_select ON public.reconciliation_results;
CREATE POLICY reconciliation_results_tenant_select ON public.reconciliation_results
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS reconciliation_results_tenant_insert ON public.reconciliation_results;
CREATE POLICY reconciliation_results_tenant_insert ON public.reconciliation_results
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- reconciliation_fee_lines (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS reconciliation_fee_lines_tenant_select ON public.reconciliation_fee_lines;
CREATE POLICY reconciliation_fee_lines_tenant_select ON public.reconciliation_fee_lines
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS reconciliation_fee_lines_tenant_insert ON public.reconciliation_fee_lines;
CREATE POLICY reconciliation_fee_lines_tenant_insert ON public.reconciliation_fee_lines
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- reconciliation_findings (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS reconciliation_findings_tenant_select ON public.reconciliation_findings;
CREATE POLICY reconciliation_findings_tenant_select ON public.reconciliation_findings
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS reconciliation_findings_tenant_insert ON public.reconciliation_findings;
CREATE POLICY reconciliation_findings_tenant_insert ON public.reconciliation_findings
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- settlement_batches (mutable for status and allocation totals)
DROP POLICY IF EXISTS settlement_batches_tenant_all ON public.settlement_batches;
CREATE POLICY settlement_batches_tenant_all ON public.settlement_batches
  FOR ALL USING (merchant_id = public.app_current_merchant_id())
  WITH CHECK (merchant_id = public.app_current_merchant_id());

-- bank_receipts (mutable for completion marking; corrections are new rows)
DROP POLICY IF EXISTS bank_receipts_tenant_all ON public.bank_receipts;
CREATE POLICY bank_receipts_tenant_all ON public.bank_receipts
  FOR ALL USING (merchant_id = public.app_current_merchant_id())
  WITH CHECK (merchant_id = public.app_current_merchant_id());

-- settlement_allocations (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS settlement_allocations_tenant_select ON public.settlement_allocations;
CREATE POLICY settlement_allocations_tenant_select ON public.settlement_allocations
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS settlement_allocations_tenant_insert ON public.settlement_allocations;
CREATE POLICY settlement_allocations_tenant_insert ON public.settlement_allocations
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- settlement_variance_calculations (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS settlement_variance_calculations_tenant_select ON public.settlement_variance_calculations;
CREATE POLICY settlement_variance_calculations_tenant_select ON public.settlement_variance_calculations
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS settlement_variance_calculations_tenant_insert ON public.settlement_variance_calculations;
CREATE POLICY settlement_variance_calculations_tenant_insert ON public.settlement_variance_calculations
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- settlement_variance_calculation_inputs (append-only: SELECT + INSERT only)
DROP POLICY IF EXISTS settlement_variance_calculation_inputs_tenant_select ON public.settlement_variance_calculation_inputs;
CREATE POLICY settlement_variance_calculation_inputs_tenant_select ON public.settlement_variance_calculation_inputs
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS settlement_variance_calculation_inputs_tenant_insert ON public.settlement_variance_calculation_inputs;
CREATE POLICY settlement_variance_calculation_inputs_tenant_insert ON public.settlement_variance_calculation_inputs
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());

-- workspace_settings (mutable, audited; tolerance settable only by
-- Owner/Admin -- an application rule, not expressible in this policy)
DROP POLICY IF EXISTS workspace_settings_tenant_all ON public.workspace_settings;
CREATE POLICY workspace_settings_tenant_all ON public.workspace_settings
  FOR ALL USING (merchant_id = public.app_current_merchant_id())
  WITH CHECK (merchant_id = public.app_current_merchant_id());

-- audit_logs: SELECT requires a merchant context (NULL-merchant rows are
-- invisible to tenant sessions -- fail-closed). INSERT allows NULL
-- merchant_id for pre-workspace identity events (e.g. sign-in) because
-- no workspace context exists yet at that point; those rows can only be
-- read back by the maintenance role, never by a tenant session.
DROP POLICY IF EXISTS audit_logs_tenant_select ON public.audit_logs;
CREATE POLICY audit_logs_tenant_select ON public.audit_logs
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS audit_logs_tenant_insert ON public.audit_logs;
CREATE POLICY audit_logs_tenant_insert ON public.audit_logs
  FOR INSERT WITH CHECK (
    merchant_id IS NULL OR merchant_id = public.app_current_merchant_id()
  );

-- idempotency_keys: SELECT + INSERT + UPDATE (sanctioned lifecycle
-- updates only -- claim/reclaim/completion/failure, CL-04/CL-14/CL-19/
-- CL-24). No DELETE policy: purge is maintenance-role only (CL-19).
DROP POLICY IF EXISTS idempotency_keys_tenant_select ON public.idempotency_keys;
CREATE POLICY idempotency_keys_tenant_select ON public.idempotency_keys
  FOR SELECT USING (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS idempotency_keys_tenant_insert ON public.idempotency_keys;
CREATE POLICY idempotency_keys_tenant_insert ON public.idempotency_keys
  FOR INSERT WITH CHECK (merchant_id = public.app_current_merchant_id());
DROP POLICY IF EXISTS idempotency_keys_tenant_update ON public.idempotency_keys;
CREATE POLICY idempotency_keys_tenant_update ON public.idempotency_keys
  FOR UPDATE USING (merchant_id = public.app_current_merchant_id())
  WITH CHECK (merchant_id = public.app_current_merchant_id());

-- ============================================================================
-- End of rls.sql. Verification (UNVERIFIED until run against live PG --
-- see README.md): as the runtime role, with context set to merchant A,
-- SELECT from every tenant table returns only A's rows; with context
-- unset/malformed it returns zero rows; cross-tenant INSERT is rejected;
-- UPDATE/DELETE on append-only tables affects zero rows.
-- ============================================================================
