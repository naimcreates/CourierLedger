Section: Cover and reading rule
Claim: v0.5 supersedes only what it names in v0.1-v0.4; no code, no migrations, no test claimed passed; M1 gated; 31 tables unchanged.
Source: verbatim request; v0.4 cover conventions.

Section: Amendment at a glance
Claim: Four findings CL-23 to CL-26; no new tables, columns or statuses; design resolution is not implementation verification.
Source: verbatim request CL-23..CL-26.

Section: CL-23 Legacy fixture loading mechanism
Claim: Load-before-trigger is the sole permitted fixture route; the v0.4 replica-mode route is removed because its privilege statement was imprecise (session_replication_role is superuser-only; DISABLE TRIGGER is owner DDL affecting all sessions, not a session setting).
Source: v0.4 CL-18 section 2 (two routes) and section 4 (fixture discipline); PostgreSQL privilege behaviour.

Section: CL-24 Expired-worker completion and failure fencing
Claim: Completion and worker-reported failure now require status + claim_token + lease_expires_at > database now(); zero rows means ownership lost and the work transaction rolls back; reclaim and sweeper unchanged; token rotation mandatory.
Source: v0.4 CL-19 predicate table (completion/failure rows lack a lease condition) versus its own renewal rule and "expired lease is not ownership" statement; v0.4 T-27(r) wording superseded in part.

Section: CL-25 Session renewal and the shared-user lock protocol
Claim: Every session-liveness-creating operation (creation, renewal/refresh, expiry extension, reactivation, rotation that issues a replacement) takes the CL-20 per-user advisory lock; pure revocation and deletion do not, because they can only remove liveness; Stage 2 rechecks live sessions under the lock.
Source: v0.4 CL-20 protocol list (session creation included, renewal silent); OD-16/OD-17 stay open.

Section: CL-26 SECURITY DEFINER search_path hardening
Claim: A seventh rule joins the CL-22 pattern: function-level SET search_path naming only trusted schemas, no CREATE for untrusted roles (including PUBLIC) in those schemas, full schema qualification, no dynamic SQL, and explicit revocation of the default PUBLIC EXECUTE grant.
Source: v0.4 CL-22 Rules 1-6; PostgreSQL defaults (EXECUTE granted to PUBLIC; caller search_path irrelevant once the function fixes its own).

Section: Consolidated test changes, traceability delta, change log
Claim: T-26(e) revised again, T-26(h) reaffirmed; T-27(r) narrowed and T-27(u)-(z) added; T-30(m)-(p) added; T-32(g)-(h) added; all design-stage.
Source: v0.4 consolidated table (T-26..T-32 in use).

Section: Remaining blockers and schema-freeze statement
Claim: Design remains ready to freeze as a review baseline as corrected by v0.5; freeze is not verification and not implementation authorisation; B-3 verification list grows by the CL-23..CL-26 mechanisms.
Source: v0.4 blockers B-1..B-4, OD-11/12/16/17, CL-11 deployment check.
