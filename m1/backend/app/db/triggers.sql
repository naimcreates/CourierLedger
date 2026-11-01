-- ============================================================================
-- CourierLedger M1 -- Triggers (triggers.sql)
-- ============================================================================
-- APPLY BY: the migrator role, as a separate reviewed step AFTER migration
-- 0001 and rls.sql. This file is NOT executed by the application or by
-- Alembic. All functions are owned by the migrator role.
--
-- Authority: v0.1 D/E/I, v0.2 CL-01/CL-03/CL-10, v0.3 CL-13/CL-16/CL-17,
-- v0.4 CL-18/CL-21/CL-22, v0.5 CL-23/CL-26.
--
-- CL-17 (the governing classification): cross-table / cross-row invariants
-- are NOT CHECKs. A plain CHECK is row-local only; PostgreSQL has no
-- CREATE ASSERTION. Every cross-table invariant below is therefore a
-- DEFERRED CONSTRAINT TRIGGER (DEFERRABLE INITIALLY DEFERRED) that
-- re-derives the invariant at commit. Row-local rules stay as CHECKs in
-- the models / migration.
--
-- CL-22 + CL-26 (execution context, six rules + rule 7) -- every validation
-- function below follows all seven:
--   1. Owned by the migrator role; SECURITY DEFINER.
--   2. merchant_id is derived from the firing row (NEW/OLD), never from
--      the session's tenant context.
--   3. No BYPASSRLS anywhere; the functions do not rely on the caller's
--      RLS context at all -- every query carries an explicit tenant
--      predicate (rule 4).
--   4. Explicit tenant predicates on every query.
--   5. Fail-closed anchor check: a NULL merchant_id on the firing row
--      raises instead of validating an empty set.
--   6. No general-purpose RLS bypass is created.
--   7. (CL-26) Each function carries its own fixed search_path
--      (SET search_path TO public, pg_catalog), every application object
--      reference is schema-qualified (public.), and there is no dynamic
--      SQL anywhere in this file.
--
-- LEASE-CLOCK RULE (CL-14/CL-19/CL-24): PostgreSQL now() is
-- transaction-start time, so it is the WRONG clock for lease comparisons
-- in a long work transaction. Every lease comparison in this design --
-- the conditional UPDATE predicates for claim / reclaim / renewal /
-- completion / failure / sweeper / purge documented in README.md and
-- roles.sql -- MUST use clock_timestamp() (statement-execution wall
-- clock). No trigger in this file compares a lease (lease logic lives in
-- the application's conditional updates); this rule is stated here so a
-- reader of the trigger file does not copy a now() comparison into one.
--
-- CL-18 / CL-23 (malformed-history fixtures): the CL-13 chain trigger is
-- scoped to the DELTA (rows inserted by the current transaction, see
-- fn_match_decision_chain_check). A transaction that merely repairs a
-- pre-existing malformed history through the sanctioned CL-13 2(d) paths
-- is not rejected for the pre-existing malformation. Deliberately
-- malformed T-26 fixtures are loaded BEFORE these triggers are created,
-- in a disposable test database only (load-before-trigger is the sole
-- permitted route -- CL-23 removed the replica-mode route outright:
-- session_replication_role is superuser-only and table-owner
-- trigger-disable DDL is not session-scoped).
--
-- UNVERIFIED: no live PostgreSQL was available when this file was
-- written (see README.md). Every function below is real, complete
-- PL/pgSQL, but none has been executed against a server.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Append-only guard (v0.1 E, CL-03, CL-10, CL-16)
--    BEFORE UPDATE OR DELETE on every append-only table. The runtime role
--    has no UPDATE/DELETE policy or grant on these tables either; this
--    trigger is the database backstop if a policy or grant is ever wrong.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fn_append_only_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public, pg_catalog
AS $$
BEGIN
  RAISE EXCEPTION 'append-only table %: % is not permitted (row %)',
    TG_TABLE_NAME, TG_OP, COALESCE(OLD.id::text, 'unknown');
  RETURN NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- 2. Reconciliation-run guard (v0.1 C.5)
--    The ONLY post-creation update permitted on a run is status and
--    completed_at. Any other column change, and any DELETE, is rejected.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fn_reconciliation_run_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public, pg_catalog
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'reconciliation_runs: DELETE is not permitted (run %)', OLD.id;
  END IF;
  IF OLD.merchant_id IS NULL THEN
    RAISE EXCEPTION 'reconciliation_runs: run % has no merchant_id', OLD.id;
  END IF;
  IF (OLD.id, OLD.merchant_id, OLD.import_batch_id, OLD.merchant_courier_account_id,
      OLD.rule_version_id, OLD.rule_version_no, OLD.rule_content_hash,
      OLD.rule_snapshot, OLD.input_snapshot, OLD.mapping_snapshot,
      OLD.tolerance_poisha, OLD.engine_version, OLD.run_hash,
      OLD.created_by_user_id, OLD.created_at)
     IS DISTINCT FROM
     (NEW.id, NEW.merchant_id, NEW.import_batch_id, NEW.merchant_courier_account_id,
      NEW.rule_version_id, NEW.rule_version_no, NEW.rule_content_hash,
      NEW.rule_snapshot, NEW.input_snapshot, NEW.mapping_snapshot,
      NEW.tolerance_poisha, NEW.engine_version, NEW.run_hash,
      NEW.created_by_user_id, NEW.created_at)
  THEN
    RAISE EXCEPTION 'reconciliation_runs: only status and completed_at may change (run %)', OLD.id;
  END IF;
  RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- 3. Rule-version guard (v0.1 C.2, T-13)
--    An active or superseded rule version may not be edited in place; a
--    correction is a new version. Only its status (and effective_to) may
--    change, e.g. activation superseding it. Drafts may be edited.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fn_rule_version_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public, pg_catalog
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status = 'active' THEN
      RAISE EXCEPTION 'courier_rule_versions: DELETE of active version % is not permitted', OLD.id;
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.merchant_id IS NULL THEN
    RAISE EXCEPTION 'courier_rule_versions: version % has no merchant_id', OLD.id;
  END IF;
  IF OLD.status IN ('active', 'superseded')
     AND (OLD.merchant_courier_account_id, OLD.version_no, OLD.cod_fee_base,
          OLD.cod_fee_rate_bps, OLD.contractual_delivery_fee_poisha, OLD.dc_confirmed,
          OLD.dc_evidence_ref, OLD.delivery_fee_poisha, OLD.return_fee_poisha,
          OLD.vat_mode, OLD.vat_rate_bps, OLD.negative_net_payout_permitted,
          OLD.rule_definition, OLD.content_hash, OLD.created_by_user_id)
         IS DISTINCT FROM
         (NEW.merchant_courier_account_id, NEW.version_no, NEW.cod_fee_base,
          NEW.cod_fee_rate_bps, NEW.contractual_delivery_fee_poisha, NEW.dc_confirmed,
          NEW.dc_evidence_ref, NEW.delivery_fee_poisha, NEW.return_fee_poisha,
          NEW.vat_mode, NEW.vat_rate_bps, NEW.negative_net_payout_permitted,
          NEW.rule_definition, NEW.content_hash, NEW.created_by_user_id)
  THEN
    RAISE EXCEPTION 'courier_rule_versions: active/superseded version % may not be edited in place; create a new version', OLD.id;
  END IF;
  RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- 4. CL-01 overall-status derivation (deferred constraint trigger)
--    Re-derives the CL-01 precedence at commit for each result touched:
--      needs_review  if any required value is NULL or any data-quality
--                    finding exists;
--      mismatch      if V_R, S or V_cond is non-zero outside the run's
--                    tolerance (only these three can produce a
--                    contractual payout mismatch);
--      open          if only C or scenario V_total is non-zero outside
--                    tolerance (V_total is scenario-only: it can move a
--                    result to open, never to mismatch, and never changes
--                    contractual_payout_status, decided by V_cond alone);
--      matched       only if no finding is non-zero outside tolerance.
--    The engine computes the status; this trigger is the commit-time
--    backstop (CL-17: this is a trigger precisely because it reads
--    findings -- a CHECK cannot).
--
--    CORRECTION (M6, 2026-10-31, verified live against PostgreSQL 16.15):
--    as first written, the "must be mismatch" branches below fired
--    whenever V_R, S or V_cond was outside tolerance, with NO exemption
--    for results that are needs_review because a data-quality finding
--    exists or a required value is NULL. That contradicted the trigger's
--    own stated derivation and v0.2 CL-01 precedence -- needs_review
--    FIRST ("If any required value is NULL or a data-quality finding
--    blocks exact calculation, overall status is needs_review. Else, if
--    V_R, S or V_cond is non-zero outside tolerance, overall status is
--    mismatch") -- and its test T-16(d), which expects a financial
--    variance plus missing data to be needs_review. Live probe
--    (scratch DB, 2026-10-31): a DQ finding + S = -1000 outside
--    tolerance + overall needs_review was REJECTED with "must be
--    mismatch", while the same values stored as mismatch were rejected
--    by the needs_review-first branch -- so the legitimate T-16(d)
--    result was unrepresentable either way. The fix gates both mismatch
--    branches on NOT (v_has_null OR v_has_dq): the "must be mismatch"
--    requirement applies only when no DQ/NULL forces needs_review. The
--    engine (V2.2.1) is the financial authority; this trigger is only
--    its commit-time backstop. Nothing else in this function changed.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fn_result_status_check()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public, pg_catalog
AS $$
DECLARE
  v_tolerance bigint;
  v_has_null boolean;
  v_has_dq boolean;
  v_bad_fee boolean;
  v_bad_stmt boolean;
  v_bad_cond boolean;
  v_coll_out boolean;
  v_scen_out boolean;
BEGIN
  -- Rule 5 (CL-22): fail-closed anchor check.
  IF NEW.merchant_id IS NULL THEN
    RAISE EXCEPTION 'CL-01: reconciliation result % has no merchant_id', NEW.id;
  END IF;
  SELECT r.tolerance_poisha INTO v_tolerance
    FROM public.reconciliation_runs r
   WHERE r.merchant_id = NEW.merchant_id AND r.id = NEW.run_id;
  IF v_tolerance IS NULL THEN
    RAISE EXCEPTION 'CL-01: run % not found for result % (merchant %)',
      NEW.run_id, NEW.id, NEW.merchant_id;
  END IF;

  v_has_null := (
    NEW.expected_collectible_poisha IS NULL OR NEW.reported_collected_poisha IS NULL
    OR NEW.c_poisha IS NULL OR NEW.x_e_poisha IS NULL OR NEW.x_r_poisha IS NULL
    OR NEW.x_upper_r_poisha IS NULL OR NEW.delta_poisha IS NULL
    OR NEW.v_r_poisha IS NULL OR NEW.p_e_poisha IS NULL OR NEW.p_cond_poisha IS NULL
    OR NEW.p_r_poisha IS NULL OR NEW.s_poisha IS NULL
    OR NEW.v_cond_poisha IS NULL OR NEW.v_total_poisha IS NULL);
  SELECT EXISTS (
    SELECT 1 FROM public.reconciliation_findings f
     WHERE f.merchant_id = NEW.merchant_id AND f.result_id = NEW.id
       AND f.category = 'data_quality'
  ) INTO v_has_dq;
  IF (v_has_null OR v_has_dq) AND NEW.overall_status <> 'needs_review' THEN
    RAISE EXCEPTION 'CL-01: result % has a NULL required value or a data-quality finding but overall_status=% (must be needs_review)',
      NEW.id, NEW.overall_status;
  END IF;

  v_bad_fee  := NEW.v_r_poisha IS NOT NULL AND abs(NEW.v_r_poisha) > v_tolerance;
  v_bad_stmt := NEW.s_poisha IS NOT NULL AND abs(NEW.s_poisha) > v_tolerance;
  v_bad_cond := NEW.v_cond_poisha IS NOT NULL AND abs(NEW.v_cond_poisha) > v_tolerance;
  -- M6 correction (see header): the "must be mismatch" requirement
  -- applies only when no DQ finding / required NULL forces needs_review.
  IF NOT (v_has_null OR v_has_dq)
     AND (v_bad_fee OR v_bad_stmt OR v_bad_cond) AND NEW.overall_status <> 'mismatch' THEN
    RAISE EXCEPTION 'CL-01: result % has a fee/statement/contractual variance outside tolerance but overall_status=% (must be mismatch)',
      NEW.id, NEW.overall_status;
  END IF;
  IF NOT (v_has_null OR v_has_dq)
     AND NEW.overall_status = 'mismatch' AND NOT (v_bad_fee OR v_bad_stmt OR v_bad_cond) THEN
    RAISE EXCEPTION 'CL-01: result % is mismatch with no fee/statement/contractual variance outside tolerance',
      NEW.id;
  END IF;

  v_coll_out := NEW.c_poisha IS NOT NULL AND abs(NEW.c_poisha) > v_tolerance;
  v_scen_out := NEW.v_total_poisha IS NOT NULL AND abs(NEW.v_total_poisha) > v_tolerance;
  IF NEW.overall_status = 'open' AND NOT (v_coll_out OR v_scen_out) THEN
    RAISE EXCEPTION 'CL-01: result % is open with no collection or scenario variance outside tolerance',
      NEW.id;
  END IF;

  IF NEW.overall_status = 'matched' AND EXISTS (
    SELECT 1 FROM public.reconciliation_findings f
     WHERE f.merchant_id = NEW.merchant_id AND f.result_id = NEW.id
       AND f.amount_poisha IS NOT NULL AND abs(f.amount_poisha) > v_tolerance
  ) THEN
    RAISE EXCEPTION 'CL-01: result % is matched while a finding is non-zero outside tolerance',
      NEW.id;
  END IF;
  RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- 5. CL-13 current-decision derivation (read helper, CL-13 section 2(b))
--    Pure function of the stored rows; uses no timestamps to choose a
--    winner. Returns the head decision when the history is well-formed
--    (one root, one head, one chain visiting every row); 'rejected' when
--    there are multiple heads but every head is rejected (unanimous
--    rejection); NULL when undecided (no rows) or conflicted (any other
--    malformed state -- the candidate must not be used by a run).
--    SECURITY DEFINER with an explicit tenant predicate (CL-22 rule 4);
--    a NULL merchant returns NULL (fail-closed, rule 5).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fn_current_match_decision(
  p_merchant_id uuid, p_candidate_id uuid)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO public, pg_catalog
AS $$
DECLARE
  v_root_count int;
  v_head_count int;
  v_walk_count int;
  v_row_count int;
  v_head_decision text;
  v_all_heads_rejected boolean;
BEGIN
  IF p_merchant_id IS NULL OR p_candidate_id IS NULL THEN
    RETURN NULL;
  END IF;
  SELECT count(*) INTO v_row_count
    FROM public.shipment_match_decisions d
   WHERE d.merchant_id = p_merchant_id AND d.candidate_id = p_candidate_id;
  IF v_row_count = 0 THEN
    RETURN NULL; -- undecided
  END IF;
  SELECT count(*) INTO v_root_count
    FROM public.shipment_match_decisions d
   WHERE d.merchant_id = p_merchant_id AND d.candidate_id = p_candidate_id
     AND d.supersedes_decision_id IS NULL;
  -- Heads: rows no other row names as predecessor.
  SELECT count(*) INTO v_head_count
    FROM public.shipment_match_decisions d
   WHERE d.merchant_id = p_merchant_id AND d.candidate_id = p_candidate_id
     AND NOT EXISTS (
       SELECT 1 FROM public.shipment_match_decisions s
        WHERE s.merchant_id = p_merchant_id AND s.candidate_id = p_candidate_id
          AND s.supersedes_decision_id = d.id);
  IF v_root_count = 1 AND v_head_count = 1 THEN
    WITH RECURSIVE walk(id) AS (
      SELECT d.id FROM public.shipment_match_decisions d
       WHERE d.merchant_id = p_merchant_id AND d.candidate_id = p_candidate_id
         AND d.supersedes_decision_id IS NULL
      UNION ALL
      SELECT d.id FROM public.shipment_match_decisions d
        JOIN walk w ON d.supersedes_decision_id = w.id
       WHERE d.merchant_id = p_merchant_id AND d.candidate_id = p_candidate_id
    )
    SELECT count(*) INTO v_walk_count FROM walk;
    IF v_walk_count = v_row_count THEN
      SELECT d.decision INTO v_head_decision
        FROM public.shipment_match_decisions d
       WHERE d.merchant_id = p_merchant_id AND d.candidate_id = p_candidate_id
         AND NOT EXISTS (
           SELECT 1 FROM public.shipment_match_decisions s
            WHERE s.merchant_id = p_merchant_id AND s.candidate_id = p_candidate_id
              AND s.supersedes_decision_id = d.id);
      RETURN v_head_decision;
    END IF;
  END IF;
  -- Multiple heads, all rejected: closed by unanimous rejection.
  SELECT bool_and(d.decision = 'rejected') INTO v_all_heads_rejected
    FROM public.shipment_match_decisions d
   WHERE d.merchant_id = p_merchant_id AND d.candidate_id = p_candidate_id
     AND NOT EXISTS (
       SELECT 1 FROM public.shipment_match_decisions s
        WHERE s.merchant_id = p_merchant_id AND s.candidate_id = p_candidate_id
          AND s.supersedes_decision_id = d.id);
  IF v_head_count > 1 AND v_all_heads_rejected THEN
    RETURN 'rejected';
  END IF;
  RETURN NULL; -- conflicted: no current decision
END;
$$;

-- ---------------------------------------------------------------------------
-- 6. CL-13 match-decision chain check, DELTA-SCOPED (deferred, CL-18)
--    At commit, for each candidate touched by the transaction, split its
--    rows into the DELTA (inserted by this transaction, identified by
--    xmin -- see note below) and the PRIOR state (already committed).
--    The transaction is rejected iff the DELTA introduces a malformation:
--      (a) the delta contains a root while a root already exists in the
--          prior state, or more than one root when there is no prior state;
--      (b) a delta successor names a predecessor that is not the head
--          implied by the prior state plus earlier delta rows;
--      (c) the delta leaves a previously well-formed history forked
--          (more than one head after the delta, where the prior state
--          was well-formed).
--    A transaction that merely REPAIRS a pre-existing malformed history
--    through the sanctioned CL-13 2(d) paths (inserting a rejecting
--    successor naming an existing head) introduces no malformation and is
--    NOT rejected. An unrelated transaction touching a different candidate
--    is never rejected because a malformation exists elsewhere.
--
--    Delta identification (CL-18, ADAPT): rows inserted by the current
--    transaction have xmin = txid_current() (xmin::text::bigint). This
--    holds for non-wrapped transaction ids; wraparound behaviour must be
--    proven in the B-3 verification environment before this finding is
--    treated as verified.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fn_match_decision_chain_check()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public, pg_catalog
AS $$
DECLARE
  v_prior_roots int;
  v_delta_roots int;
  v_prior_rows int;
  v_prior_heads int;
  v_total_heads int;
  v_walk_count int;
  v_prior_well_formed boolean;
  r record;
BEGIN
  IF NEW.merchant_id IS NULL THEN
    RAISE EXCEPTION 'CL-13: decision % has no merchant_id', NEW.id;
  END IF;

  SELECT count(*) INTO v_prior_roots
    FROM public.shipment_match_decisions d
   WHERE d.merchant_id = NEW.merchant_id AND d.candidate_id = NEW.candidate_id
     AND d.supersedes_decision_id IS NULL
     AND d.xmin::text::bigint <> txid_current();
  SELECT count(*) INTO v_delta_roots
    FROM public.shipment_match_decisions d
   WHERE d.merchant_id = NEW.merchant_id AND d.candidate_id = NEW.candidate_id
     AND d.supersedes_decision_id IS NULL
     AND d.xmin::text::bigint = txid_current();
  -- (a) Roots introduced by the delta.
  IF v_prior_roots = 0 AND v_delta_roots <> 1 THEN
    RAISE EXCEPTION 'CL-13: candidate % must gain exactly one root, got %', NEW.candidate_id, v_delta_roots;
  END IF;
  IF v_prior_roots >= 1 AND v_delta_roots > 0 THEN
    RAISE EXCEPTION 'CL-13: candidate % already has a root; a second root is a malformation', NEW.candidate_id;
  END IF;

  -- (b) Every delta successor must name the head implied by prior state
  -- plus earlier delta rows: i.e. its predecessor is either a prior head
  -- or a delta row, and no other row (prior or delta) already names that
  -- predecessor (the partial unique index is the statement-time backstop;
  -- this is the commit-time re-check).
  FOR r IN
    SELECT d.id, d.supersedes_decision_id
      FROM public.shipment_match_decisions d
     WHERE d.merchant_id = NEW.merchant_id AND d.candidate_id = NEW.candidate_id
       AND d.supersedes_decision_id IS NOT NULL
       AND d.xmin::text::bigint = txid_current()
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM public.shipment_match_decisions p
       WHERE p.merchant_id = NEW.merchant_id AND p.candidate_id = NEW.candidate_id
         AND p.id = r.supersedes_decision_id
    ) THEN
      RAISE EXCEPTION 'CL-13: decision % supersedes non-existent decision %', r.id, r.supersedes_decision_id;
    END IF;
    IF EXISTS (
      SELECT 1 FROM public.shipment_match_decisions o
       WHERE o.merchant_id = NEW.merchant_id AND o.candidate_id = NEW.candidate_id
         AND o.supersedes_decision_id = r.supersedes_decision_id AND o.id <> r.id
    ) THEN
      RAISE EXCEPTION 'CL-13: decision % already has a successor; candidate % would fork', r.supersedes_decision_id, NEW.candidate_id;
    END IF;
    -- Predecessor must be a prior head or a delta row.
    IF NOT EXISTS (
      SELECT 1 FROM public.shipment_match_decisions p
       WHERE p.merchant_id = NEW.merchant_id AND p.candidate_id = NEW.candidate_id
         AND p.id = r.supersedes_decision_id
         AND (p.xmin::text::bigint = txid_current()
              OR NOT EXISTS (
                SELECT 1 FROM public.shipment_match_decisions s
                 WHERE s.merchant_id = NEW.merchant_id AND s.candidate_id = NEW.candidate_id
                   AND s.supersedes_decision_id = p.id
                   AND s.xmin::text::bigint <> txid_current()))
    ) THEN
      RAISE EXCEPTION 'CL-13: decision % does not name the current head of candidate %', r.id, NEW.candidate_id;
    END IF;
  END LOOP;

  -- (c) If the prior state was well-formed, the combined history must
  -- still have exactly one head. (If the prior state was malformed, the
  -- repair paths in (a)/(b) are allowed through.)
  SELECT count(*) INTO v_prior_rows
    FROM public.shipment_match_decisions d
   WHERE d.merchant_id = NEW.merchant_id AND d.candidate_id = NEW.candidate_id
     AND d.xmin::text::bigint <> txid_current();
  IF v_prior_rows > 0 AND v_prior_roots = 1 THEN
    SELECT count(*) INTO v_prior_heads
      FROM public.shipment_match_decisions d
     WHERE d.merchant_id = NEW.merchant_id AND d.candidate_id = NEW.candidate_id
       AND d.xmin::text::bigint <> txid_current()
       AND NOT EXISTS (
         SELECT 1 FROM public.shipment_match_decisions s
          WHERE s.merchant_id = NEW.merchant_id AND s.candidate_id = NEW.candidate_id
            AND s.supersedes_decision_id = d.id
            AND s.xmin::text::bigint <> txid_current());
    WITH RECURSIVE walk(id) AS (
      SELECT d.id FROM public.shipment_match_decisions d
       WHERE d.merchant_id = NEW.merchant_id AND d.candidate_id = NEW.candidate_id
         AND d.supersedes_decision_id IS NULL
         AND d.xmin::text::bigint <> txid_current()
      UNION ALL
      SELECT d.id FROM public.shipment_match_decisions d
        JOIN walk w ON d.supersedes_decision_id = w.id
       WHERE d.merchant_id = NEW.merchant_id AND d.candidate_id = NEW.candidate_id
         AND d.xmin::text::bigint <> txid_current()
    )
    SELECT count(*) INTO v_walk_count FROM walk;
    v_prior_well_formed := (v_prior_heads = 1 AND v_walk_count = v_prior_rows);
    IF v_prior_well_formed THEN
      SELECT count(*) INTO v_total_heads
        FROM public.shipment_match_decisions d
       WHERE d.merchant_id = NEW.merchant_id AND d.candidate_id = NEW.candidate_id
         AND NOT EXISTS (
           SELECT 1 FROM public.shipment_match_decisions s
            WHERE s.merchant_id = NEW.merchant_id AND s.candidate_id = NEW.candidate_id
              AND s.supersedes_decision_id = d.id);
      IF v_total_heads <> 1 THEN
        RAISE EXCEPTION 'CL-13: candidate % was well-formed and the delta leaves % heads', NEW.candidate_id, v_total_heads;
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- 7. CL-03 / CL-13(f) statement-line level (deferred constraint trigger)
--    At most one currently-confirmed candidate per statement line. Each
--    candidate's chain can advance independently, so superseding one
--    candidate's confirmed head with a rejection restores the invariant;
--    this trigger is the commit-time backstop.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fn_confirmed_candidate_line_check()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public, pg_catalog
AS $$
DECLARE
  v_line_id uuid;
  v_confirmed_count int;
BEGIN
  IF NEW.merchant_id IS NULL THEN
    RAISE EXCEPTION 'CL-03: decision % has no merchant_id', NEW.id;
  END IF;
  SELECT c.statement_line_id INTO v_line_id
    FROM public.shipment_match_candidates c
   WHERE c.merchant_id = NEW.merchant_id AND c.id = NEW.candidate_id;
  IF v_line_id IS NULL THEN
    RAISE EXCEPTION 'CL-03: candidate % not found for decision %', NEW.candidate_id, NEW.id;
  END IF;
  SELECT count(*) INTO v_confirmed_count
    FROM public.shipment_match_candidates c
   WHERE c.merchant_id = NEW.merchant_id AND c.statement_line_id = v_line_id
     AND public.fn_current_match_decision(NEW.merchant_id, c.id) = 'confirmed';
  IF v_confirmed_count > 1 THEN
    RAISE EXCEPTION 'CL-03: statement line % has % currently-confirmed candidates', v_line_id, v_confirmed_count;
  END IF;
  RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- 8. CL-16 / CL-21 variance-calculation backstop (deferred, CL-22 pattern)
--    A calculation row may commit only if, at insert time, its snapshots
--    agree with its listed input set and the CL-16 preconditions hold:
--    the batch's expected payout is NOT NULL and equals the snapshot;
--    every receipt currently linked to the batch is complete and is
--    listed; the listed allocation set equals the batch's current
--    allocation set (including reversal rows); the net allocated total
--    from the listed allocations equals the snapshot; and the batch is
--    fully allocated. Later changes invalidate the CURRENT variance by
--    derivation (CL-21 input-set equality at read time); they never edit
--    this row.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fn_variance_calculation_check()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public, pg_catalog
AS $$
DECLARE
  v_expected bigint;
  v_alloc_status text;
  v_net_from_inputs bigint;
  v_listed_receipts int;
  v_current_receipts int;
  v_listed_allocs int;
  v_current_allocs int;
BEGIN
  IF NEW.merchant_id IS NULL THEN
    RAISE EXCEPTION 'CL-16: variance calculation % has no merchant_id', NEW.id;
  END IF;
  SELECT b.expected_batch_payout_poisha, b.allocation_status
    INTO v_expected, v_alloc_status
    FROM public.settlement_batches b
   WHERE b.merchant_id = NEW.merchant_id AND b.id = NEW.settlement_batch_id;
  IF v_expected IS NULL THEN
    RAISE EXCEPTION 'CL-16: batch % has no calculable expected payout; no variance calculation may exist', NEW.settlement_batch_id;
  END IF;
  IF NEW.expected_batch_payout_poisha <> v_expected THEN
    RAISE EXCEPTION 'CL-16: calculation % snapshots expected payout % but batch % expects %',
      NEW.id, NEW.expected_batch_payout_poisha, NEW.settlement_batch_id, v_expected;
  END IF;
  IF v_alloc_status <> 'fully_allocated' THEN
    RAISE EXCEPTION 'CL-16: batch % is not fully allocated (status %)', NEW.settlement_batch_id, v_alloc_status;
  END IF;
  -- Every receipt currently linked to the batch must be complete.
  IF EXISTS (
    SELECT 1 FROM public.bank_receipts r
     WHERE r.merchant_id = NEW.merchant_id AND r.settlement_batch_id = NEW.settlement_batch_id
       AND r.is_complete IS NOT TRUE
  ) THEN
    RAISE EXCEPTION 'CL-16: batch % has an incomplete receipt; no variance calculation may exist', NEW.settlement_batch_id;
  END IF;
  -- Listed receipt set must equal the batch's current receipt set.
  SELECT count(*) INTO v_listed_receipts
    FROM public.settlement_variance_calculation_inputs i
   WHERE i.merchant_id = NEW.merchant_id AND i.calculation_id = NEW.id AND i.input_kind = 'receipt';
  SELECT count(*) INTO v_current_receipts
    FROM public.bank_receipts r
   WHERE r.merchant_id = NEW.merchant_id AND r.settlement_batch_id = NEW.settlement_batch_id;
  IF v_listed_receipts <> v_current_receipts THEN
    RAISE EXCEPTION 'CL-16: calculation % lists % receipts but batch % currently has %',
      NEW.id, v_listed_receipts, NEW.settlement_batch_id, v_current_receipts;
  END IF;
  -- Listed allocation set must equal the batch's current allocation set.
  SELECT count(*) INTO v_listed_allocs
    FROM public.settlement_variance_calculation_inputs i
   WHERE i.merchant_id = NEW.merchant_id AND i.calculation_id = NEW.id AND i.input_kind = 'allocation';
  SELECT count(*) INTO v_current_allocs
    FROM public.settlement_allocations a
   WHERE a.merchant_id = NEW.merchant_id AND a.settlement_batch_id = NEW.settlement_batch_id;
  IF v_listed_allocs <> v_current_allocs THEN
    RAISE EXCEPTION 'CL-16: calculation % lists % allocations but batch % currently has %',
      NEW.id, v_listed_allocs, NEW.settlement_batch_id, v_current_allocs;
  END IF;
  -- Net allocated total from the listed allocations must equal the snapshot.
  SELECT COALESCE(sum(CASE WHEN a.kind = 'reversal' THEN -a.allocated_poisha ELSE a.allocated_poisha END), 0)
    INTO v_net_from_inputs
    FROM public.settlement_allocations a
   WHERE a.merchant_id = NEW.merchant_id
     AND a.id IN (
       SELECT i.settlement_allocation_id
         FROM public.settlement_variance_calculation_inputs i
        WHERE i.merchant_id = NEW.merchant_id AND i.calculation_id = NEW.id
          AND i.input_kind = 'allocation');
  IF v_net_from_inputs <> NEW.allocated_bank_confirmed_total_poisha THEN
    RAISE EXCEPTION 'CL-16: calculation % snapshots allocated total % but its listed allocations net to %',
      NEW.id, NEW.allocated_bank_confirmed_total_poisha, v_net_from_inputs;
  END IF;
  RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- 9. CL-10 over-allocation backstop (deferred constraint trigger)
--    The net allocated total from one receipt (allocations minus their
--    reversals) must never exceed that receipt's amount. Enforced in the
--    allocation transaction with a row lock on the receipt; this trigger
--    is the mandatory commit-time backstop (CL-10 makes it mandatory,
--    not "reviewed as part of implementation").
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fn_allocation_overage_check()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public, pg_catalog
AS $$
DECLARE
  v_amount bigint;
  v_net bigint;
BEGIN
  IF NEW.merchant_id IS NULL THEN
    RAISE EXCEPTION 'CL-10: allocation % has no merchant_id', NEW.id;
  END IF;
  SELECT r.amount_poisha INTO v_amount
    FROM public.bank_receipts r
   WHERE r.merchant_id = NEW.merchant_id AND r.id = NEW.bank_receipt_id;
  IF v_amount IS NULL THEN
    RAISE EXCEPTION 'CL-10: receipt % not found for allocation %', NEW.bank_receipt_id, NEW.id;
  END IF;
  SELECT COALESCE(sum(CASE WHEN a.kind = 'reversal' THEN -a.allocated_poisha ELSE a.allocated_poisha END), 0)
    INTO v_net
    FROM public.settlement_allocations a
   WHERE a.merchant_id = NEW.merchant_id AND a.bank_receipt_id = NEW.bank_receipt_id;
  IF v_net > v_amount THEN
    RAISE EXCEPTION 'CL-10: net allocation % from receipt % exceeds its amount %', v_net, NEW.bank_receipt_id, v_amount;
  END IF;
  RETURN NEW;
END;
$$;

-- ============================================================================
-- Trigger attachments. BEFORE triggers fire immediately; CONSTRAINT
-- TRIGGERs are DEFERRABLE INITIALLY DEFERRED and fire at commit.
-- ============================================================================

-- Append-only guard (BEFORE UPDATE OR DELETE). Written out explicitly,
-- one statement per table -- no dynamic SQL anywhere in this file (CL-26).
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.courier_rule_evidence;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.courier_rule_evidence FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.file_objects;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.file_objects FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.import_rows;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.import_rows FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.import_errors;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.import_errors FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.statement_lines;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.statement_lines FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.shipment_match_candidates;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.shipment_match_candidates FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.shipment_match_decisions;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.shipment_match_decisions FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.reconciliation_results;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.reconciliation_results FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.reconciliation_fee_lines;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.reconciliation_fee_lines FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.reconciliation_findings;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.reconciliation_findings FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.settlement_allocations;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.settlement_allocations FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.settlement_variance_calculations;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.settlement_variance_calculations FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.settlement_variance_calculation_inputs;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.settlement_variance_calculation_inputs FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();
DROP TRIGGER IF EXISTS trg_append_only_guard ON public.audit_logs;
CREATE TRIGGER trg_append_only_guard BEFORE UPDATE OR DELETE ON public.audit_logs FOR EACH ROW EXECUTE FUNCTION public.fn_append_only_guard();

-- Reconciliation-run guard.
DROP TRIGGER IF EXISTS trg_reconciliation_run_guard ON public.reconciliation_runs;
CREATE TRIGGER trg_reconciliation_run_guard
  BEFORE UPDATE OR DELETE ON public.reconciliation_runs
  FOR EACH ROW EXECUTE FUNCTION public.fn_reconciliation_run_guard();

-- Rule-version guard.
DROP TRIGGER IF EXISTS trg_rule_version_guard ON public.courier_rule_versions;
CREATE TRIGGER trg_rule_version_guard
  BEFORE UPDATE OR DELETE ON public.courier_rule_versions
  FOR EACH ROW EXECUTE FUNCTION public.fn_rule_version_guard();

-- CL-01 overall-status derivation (deferred to commit).
DROP TRIGGER IF EXISTS trg_result_status_check ON public.reconciliation_results;
CREATE CONSTRAINT TRIGGER trg_result_status_check
  AFTER INSERT OR UPDATE ON public.reconciliation_results
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION public.fn_result_status_check();

-- CL-13 chain check, delta-scoped (deferred to commit).
DROP TRIGGER IF EXISTS trg_match_decision_chain_check ON public.shipment_match_decisions;
CREATE CONSTRAINT TRIGGER trg_match_decision_chain_check
  AFTER INSERT ON public.shipment_match_decisions
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION public.fn_match_decision_chain_check();

-- CL-03 line-level: at most one currently-confirmed candidate per line.
DROP TRIGGER IF EXISTS trg_confirmed_candidate_line_check ON public.shipment_match_decisions;
CREATE CONSTRAINT TRIGGER trg_confirmed_candidate_line_check
  AFTER INSERT ON public.shipment_match_decisions
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION public.fn_confirmed_candidate_line_check();

-- CL-16 variance-calculation backstop (deferred to commit).
DROP TRIGGER IF EXISTS trg_variance_calculation_check ON public.settlement_variance_calculations;
CREATE CONSTRAINT TRIGGER trg_variance_calculation_check
  AFTER INSERT ON public.settlement_variance_calculations
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION public.fn_variance_calculation_check();

-- CL-10 over-allocation backstop (deferred to commit).
DROP TRIGGER IF EXISTS trg_allocation_overage_check ON public.settlement_allocations;
CREATE CONSTRAINT TRIGGER trg_allocation_overage_check
  AFTER INSERT ON public.settlement_allocations
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION public.fn_allocation_overage_check();

-- ============================================================================
-- End of triggers.sql. UNVERIFIED against live PostgreSQL (see README.md).
-- ============================================================================
