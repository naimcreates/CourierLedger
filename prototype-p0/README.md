# CourierLedger P0 — Disposable Prototype

**Prototype, not production.** Synthetic data only, fictional courier
fixtures only, local use only. Do not proceed to production M1 without
explicit approval.

Implements Specification V2 + Amendments 2.1, 2.2 and 2.2.1 (2.2.1 is the
latest financial-logic authority). All amounts are integer poisha; no
floating-point arithmetic is used for money; each fee line is rounded
half-up once, in integer arithmetic.

## Phase 0 — environment (verified 2026-10-31)

- Python 3.12.3, Node v24.22.1, git 2.43.0 present.
- `docker` and `psql` are **not** available. A file `~/.postgres-ready`
  exists but its meaning is not established — it is **not** treated as a
  PostgreSQL environment.
- fastapi / pytest were not installed globally; they install cleanly into
  a venv (fastapi 0.162.0, pytest 9.2.0).
- No existing code existed in the goal workspace (only `GOAL.md`), so
  nothing was overwritten.

## Architecture (smallest practical, easy to discard)

```
prototype-p0/
  README.md            this file
  requirements.txt     fastapi, uvicorn, python-multipart, pytest, httpx
  .venv/               local virtualenv (created by you, not committed)
  runs.sqlite3         created at first run — immutable reconciliation runs
  app/
    engine.py          pure-Python financial engine (no I/O, no UI)
    csvio.py           CSV parsing + column mapping
    store.py           SQLite run store (insert-only; runs are immutable)
    main.py            FastAPI app: dashboard + JSON/CSV API
  static/index.html    dashboard (static HTML/CSS/JS, no build step, no CDN)
  data/demo.csv        14 synthetic shipments (V2.2.1 worked examples + more)
  data/edge_cases.csv  10 boundary / edge-case shipments
  tests/               pytest: AT-1..AT-12 + boundary, CSV, API tests
```

Why no React/Vite for P0: no frontend existed, and a build toolchain is
discardable weight for a disposable prototype whose point is engine
correctness. The engine (`app/engine.py`) is independent of the UI; the
static dashboard is a thin client that can be replaced by React in M1.
SQLite (file) is used instead of PostgreSQL because no PostgreSQL is
available here; it stores immutable runs only — it is not a production
data model. No paid services, no live courier APIs, no bank CSV.

## Run instructions

```bash
cd prototype-p0
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -v        # test suite
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000/
```

Stop the server with Ctrl-C when done; do not leave it running.

## API

- `GET /` — dashboard
- `GET /api/health` — `{"status": "ok", ...}`
- `GET /api/rules` — the three fictional fixtures
- `GET /api/demo` — reconcile `data/demo.csv`, returns a new run
- `POST /api/reconcile` — JSON `{"shipments": [...], "tolerance_poisha": 0}`
- `POST /api/reconcile-csv` — multipart CSV upload + optional `mapping` JSON
- `GET /api/runs` / `GET /api/runs/{run_id}` — immutable run history
- `GET /api/shipments/{run_id}/{tracking_id}` — one shipment breakdown

Every reconcile call creates a **new** run id in SQLite. Re-runs never
mutate an earlier run.

## CSV schema

Header names are matched case-insensitively; the UI also lets you map
headers manually. Money is integer **poisha**; a non-integer money value
is invalid (never silently zero).

| Field | Meaning |
|---|---|
| merchant_order_id, tracking_id | identifiers (duplicates flagged, never merged) |
| courier_fixture | `Fixture Alpha` / `Fixture Beta` / `Fixture Gamma` (synthetic only) |
| outcome | delivered / partially_delivered / returned / cancelled / superseded / unknown |
| amount_to_collect_poisha | amount to collect for a delivered shipment |
| documented_partial_poisha | documented partial amount (partial delivery only) |
| reported_collected_poisha | R — courier-reported collected COD |
| expected_delivery_fee_poisha | D_e |
| reported_delivery_fee_poisha | D_r |
| contractual_delivery_fee_poisha | D_c (`cod_minus_delivery_fee` only) |
| contractual_delivery_fee_confirmed | explicit flag only — yes/no; a D_c value alone is NOT confirmation |
| contractual_delivery_fee_evidence | evidence reference for D_c confirmation (required; empty = not contractual) |
| cod_fee_base | full_cod / cod_minus_delivery_fee / reported_collected |
| cod_fee_rate_bp | rate in basis points (100bp = 1.00%) |
| other_fees_poisha | O — other expected outcome fees |
| return_fee_poisha | return fee (explicit 0 is verified 0; blank is NULL) |
| excluded_tax_poisha | T_e; reported_excluded_tax_poisha = T_r |
| reported_deductions_total_poisha | X_r total as stated by the courier |
| reported_cod_fee_poisha, reported_other_fee_poisha, reported_return_fee_poisha | fee lines (blank/invalid/absent are NULL, never zero) |
| reported_payout_poisha | P_r (blank for a returned shipment — per-line payout is NULL) |
| vat_mode, vat_formula, vat_rate_bp | included / excluded / not_applicable / unknown; rate_on_net / rate_on_gross |
| note | free text |

## Financial logic (V2.2.1, exact)

`C = R − E`; `Δ = X_R − X_e`; `V_R = X_r − X_R`; `S = P_r − (R − X_r)`;
`V_cond = P_r − P_cond`; `V_total = P_r − P_e`.
Identities asserted in code and tests whenever all inputs are non-NULL:
`V_total = C − Δ − V_R + S`; `V_cond = S − V_R`.

- `full_cod`: F_e / F_R on E / R. `cod_minus_delivery_fee`: F on
  max(E − D_c, 0) / max(R − D_c, 0). D_c is contractual ONLY when the
  confirmation flag is explicitly set AND a non-empty evidence reference
  is supplied; a missing flag, a false flag, or missing evidence all
  produce `DQ_MISSING_CONTRACT`, with F_e, F_R, P_e, P_cond, V_total and
  V_cond NULL — no fallback to D_e or D_r, and a D_c value alone is
  never treated as confirmation.
  `reported_collected`: F_e / P_e are **scenario-only** (`is_scenario`,
  labelled "scenario"); the contractual fee is F_R and the contractual
  comparison is P_r vs P_cond only.
- Expected collectible E by outcome: delivered = amount to collect;
  partially delivered = documented partial only, else NULL; confirmed
  return / cancellation / superseded = verified 0; unknown = NULL.
- Fee-line states: absent column → NULL (`missing_column`); blank row →
  NULL (`blank_value`); explicit zero → verified 0; invalid → NULL
  (`invalid_value`, raw text preserved); valid → integer poisha.
  X_r is the stated total if provided, else the sum of parsed lines
  (NULL if any required line is unparsed); if both exist and differ, a
  statement finding preserves both.
- Included tax is a breakdown only, never subtracted again. Unknown VAT
  mode blocks an exact payout when a non-zero tax is in play, except the
  included-mode / unknown-rate exception (breakdown NULL, gross counts once).
- Findings are a list and are never netted: COLLECTION (C), FEE_TAX
  (V_R), STATEMENT (S), PAYOUT_COND (V_cond), SCENARIO (V_total), plus
  data-quality findings. Three separate statuses: contractual payout
  status (from V_cond only — V_total never sets it), data-quality status,
  overall status. Overall, in label precedence order (findings are
  always preserved): `needs_review` if any data-quality finding or any
  required NULL; else `mismatch` if a FEE_TAX, STATEMENT or PAYOUT_COND
  finding is non-zero outside tolerance (default 0 poisha); else `open`
  if the only non-zero findings outside tolerance are COLLECTION and/or
  SCENARIO — a collection-only discrepancy is open, with contractual
  payout still matched, and is never misrepresented as a contractual
  payout mismatch; else `matched_with_tolerance` if non-zero findings
  exist but all are within tolerance (finding still listed, gross amount
  shown); else `matched`. This clarifies V2.2.1 §4, which read as if any
  non-zero financial finding meant `mismatch`; the P0 patch reserves
  `mismatch` for fee / statement / contractual payout failures.
- Returned shipments: expected outcome may be negative (e.g. −7,000p),
  shown as a batch-level amount owed / to be offset, **not** as a payout
  and **not** as a bank settlement. A return fee does not imply payment
  occurred. Bank CSV is excluded from P0; there is no bank variance.

## Fictional fixtures (synthetic — not real contracts)

- **Fixture Alpha** — `full_cod`, 100bp. **Fixture Beta** —
  `cod_minus_delivery_fee`, 100bp. **Fixture Gamma** —
  `reported_collected`, 100bp.
- These rates are configurable test fixtures. They are **not** Steadfast,
  Pathao or Paperfly terms and must never be presented as such.

## Demo dataset (`data/demo.csv`, 14 shipments)

TRK-0001 matched delivered; TRK-0002 V2.2.1 Ex2 scenario-only
under-collection (contractual matched, overall `open`, V_total −4,950p); TRK-0003 Ex1
(D_e 7,000p vs D_r 9,000p, V_R +2,000p); TRK-0004 Ex4
multi-discrepancy (C −10,000p, V_R +2,000p, S −100p, V_cond −2,100p,
V_total −12,000p); TRK-0005 Ex5 D_c unknown; TRK-0006 Ex6 returned,
explicit zeros, outcome −7,000p; TRK-0007 partial delivery (P_e
48,900p); TRK-0008 blank COD-fee line; TRK-0009 invalid COD-fee value
("1,5x0"); TRK-0010 statement inconsistency (S −2,500p); TRK-DUP-01
duplicate tracking ID (two rows, both preserved); TRK-0013 missing
outcome; TRK-0014 VAT-included delivery fee (net 10,000p, tax 1,500p).

`data/edge_cases.csv` (10 rows): zero rate, reported collection below
D_c, zero COD, large values (10,000,000,000p), partial without a
documented amount, missing expected delivery fee, invalid reported
collection, blank tracking ID, half-up rounding (1,050p at 1% → 11p),
reported-collected fully matched.

## Tests

`tests/test_engine.py` (AT-1..AT-12, boundary, duplicate, outcome,
regression, VAT, plus P0-patch tests: overall `open` vs `mismatch`,
explicit D_c confirmation/evidence, determinism), `tests/test_csv.py` (parsing, mapping, fee-line
states, demo/edge datasets), `tests/test_api.py` (health, rules, demo,
JSON/CSV reconcile, run immutability, no edit/delete endpoints, dashboard). Run with
`.venv/bin/python -m pytest tests/ -v`. Results are recorded in the
delivery report; no pass is claimed without running them.

## P0 patch — logic consistency & safety (2026-10-31)

1. **Overall status:** `open` added. Collection-only discrepancies are
   `open`, not `mismatch`; `mismatch` is reserved for fee, statement or
   contractual payout findings. Contractual payout status is unchanged
   (V_cond only). All findings and gross amounts are still shown.
2. **D_c confirmation:** explicit flag AND non-empty evidence are both
   required. A D_c value alone, a missing flag, a false flag, or missing
   evidence all give `DQ_MISSING_CONTRACT` and NULL fees/payouts, with no
   fallback to D_e or D_r. Demo/edge Beta rows carry the synthetic
   evidence reference `fixture-rate-card-2026-10-31`.
3. **Immutable runs:** `app/store.py` is insert-only — `save_run` does a
   single INSERT, there is no UPDATE/DELETE code and no API endpoint for
   editing or deleting runs (PUT/PATCH/DELETE return 405). Tests prove an
   earlier run's results are byte-for-byte unchanged after later runs,
   and that identical inputs with the same rule version give identical
   results (apart from run id / timestamp). **This is application-level
   immutability only:** anyone with direct SQLite file access could still
   modify `runs.sqlite3`. Production requires database-level controls —
   append-only table, a trigger or constraint rejecting UPDATE/DELETE,
   least-privilege database credentials, and audit logging — none of which
   P0 provides.

## Known limitations

- SQLite file, not PostgreSQL; no multi-user or auth (local prototype).
- No React frontend; static dashboard is a thin client.
- CSV is the only import path; no courier APIs and no bank CSV by design.
- D_c confirmation is explicit-only: flag + evidence are both required;
  a D_c value alone is not confirmation (see patch note above).
- P0 run immutability is application-level only (insert-only code, no
  edit/delete endpoints). Direct SQLite file access could still change
  history; production needs database-level append-only controls.
- Tolerance is per-run (default 0 poisha); there is no per-workspace
  settings UI yet.
- A returned shipment has no per-line payout, so its overall status is
  `needs_review` (required payout comparisons are NULL) even though its
  expected outcome (−7,000p in Ex6) is correctly computed — the batch
  amount owed is shown separately.
- Weather / market / real courier behaviour are out of scope; nothing
  here validates a real contract.

## Deliberately excluded from P0

Live courier APIs, real courier booking, real merchant financial data,
bank CSV integration, automated dispute filing, AI fraud prediction,
profitability calculations, accounting or tax filing, production
authentication or multi-tenant infrastructure, production deployment,
any feature outside the approved MVP.

## Recommendation — readiness for the 14-day market validation

The prototype demonstrates the reconciliation logic on synthetic data
and is suitable as the disposable validation artefact **provided the
acceptance tests pass** (see delivery report for the actual run). It is
not evidence about real couriers: market validation still needs
merchant-confirmed Steadfast/Pathao evidence (rate cards, redacted CSV
headers, statement semantics, settlement batch key, VAT confirmation),
seller interviews, and the Day 7 / Day 14 gates. Do not use P0 results
to make any claim about a real courier's rates or payouts.
