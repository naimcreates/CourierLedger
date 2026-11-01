# CourierLedger M1 — domain engine

Pure-Python COD reconciliation engine. Financial authority: Specification
V2 + Amendments 2.1, 2.2, 2.2.1 plus the P0 patch. Ported from the P0
disposable prototype engine (`prototype-p0/app/engine.py`); see the module
docstring of `engine.py` for the port notes. No FastAPI, SQLAlchemy, CSV,
frontend, or database imports — standard library only (`re`,
`dataclasses`).

## Money

- All money is integer **poisha** (BIGINT semantics). No float arithmetic
  for money anywhere.
- Half-up rounding per fee line, in integer arithmetic:
  `round_half_up_fee(base, rate_bp) = (base * rate_bp + 5000) // 10000`.
  Example: 1050p at 100bp (1%) -> 11p.
- `NULL` (Python `None`) is never silently replaced by verified 0. Only an
  explicit zero is verified 0.

## Variables (per shipment)

- `E` — expected collectible COD, fixed by the **confirmed** outcome:
  delivered -> amount to collect; partially delivered -> documented
  partial amount only, else NULL; returned / cancelled / superseded ->
  verified 0; unknown / missing outcome -> NULL.
- `R` — courier-reported collected COD.
- `D_e` / `D_r` / `D_c` — expected / reported / contractual delivery fee,
  kept separate. `D_c` is contractual **only** with an explicit
  confirmation flag AND a non-empty evidence reference; a `D_c` value
  alone is not confirmation. Unconfirmed `D_c` -> `DQ_MISSING_CONTRACT`,
  fees and payouts NULL, no fallback to `D_e` or `D_r`.
- `F_e` / `F_R` — COD fee on the expected basis / reported basis.
  `cod_minus_delivery_fee` bases the fee on `max(amount - D_c, 0)`.
- `X_e` / `X_R` / `X_r` — expected deductions, recomputed deductions on
  the reported basis, reported deductions.
- `P_e` / `P_cond` / `P_r` — expected payout, payout conditional on
  reported collection, reported payout.

## Identities (asserted in code and in tests whenever all terms exist)

- `V_total = C - Δ - V_R + S`, where `C = R - E` (collection variance),
  `Δ = X_R - X_e` (deduction change from the fee-basis change),
  `V_R = X_r - X_R` (fee/tax variance on the reported basis),
  `S = P_r - (R - X_r)` (statement's own arithmetic inconsistency).
- `V_cond = S - V_R`, where `V_cond = P_r - P_cond`.
- `S` is the statement's own inconsistency and is never forced to zero.

## Fee bases

- `full_cod`: fee on the full amount; `F_e`/`F_R` on `E`/`R`.
- `cod_minus_delivery_fee`: fee on `amount - D_c` (see `D_c` above).
- `reported_collected`: same arithmetic as `full_cod`, but `F_e`/`P_e`
  and `V_total` are **scenario-only** — a scenario-only `V_total` never
  triggers a contractual payout mismatch, which is decided by `V_cond`
  alone.

## Fee-line states (V2.2.1 §3)

Each fee line is `missing_column` / `blank_value` / `invalid_value` /
`valid` / `explicit_zero`. Missing, blank, and invalid are distinct
data-quality states whose value is NULL, never verified zero; the raw text
of an invalid value is preserved. A delivery fee line fills `D_r` only
when `D_r` was not supplied (preserved P0 behaviour).

## Included tax (V2.1)

An included-tax breakdown (`rate_on_net` or `rate_on_gross`) is a
breakdown only — the gross fee still counts exactly once and is never
subtracted again. Unknown formula or rate -> breakdown NULL, payout still
calculable (the included-mode / unknown-rate exception). Unknown VAT mode
with a non-zero tax in play blocks an exact payout (`DQ_UNKNOWN_VAT`).

## Status model — three separate statuses, five overall values

- `data_quality_status`: `complete` or `needs_review` (any DQ finding, or
  any required value NULL).
- `contractual_payout_status`: `matched` / `mismatch` / `unknown`,
  decided by `V_cond` alone.
- `overall_status` (label precedence `needs_review` > `mismatch` >
  `open` > `matched_with_tolerance` > `matched`):
  - `needs_review` — any data-quality finding or required NULL.
  - `mismatch` — a `FEE_TAX`, `STATEMENT`, or `PAYOUT_COND` finding is
    non-zero outside tolerance.
  - `open` — the only non-zero findings outside tolerance are
    `COLLECTION` and/or `SCENARIO`. Contractual payout stays matched.
  - `matched_with_tolerance` — non-zero findings exist but all are
    within tolerance; each finding is still listed, gross amount shown.
  - `matched` — no non-zero financial finding, no DQ finding, no NULL.
- Findings are an independent list and are never netted; none hides
  another. An unexplained difference is never automatically a confirmed
  loss. Duplicate tracking IDs are flagged (`DQ_DUPLICATE_TRACKING`) and
  preserved, never merged or summed. Returned shipments have NULL
  per-line payout; a negative expected outcome is an amount owed at batch
  level (`batch_amount_owed_poisha`), not a bank settlement.

## Fixtures — synthetic only

`Fixture Alpha` (`full_cod`), `Fixture Beta`
(`cod_minus_delivery_fee`), `Fixture Gamma` (`reported_collected`), all
100bp. Every fixture is labelled `SYNTHETIC FIXTURE — not a real courier
contract`. They are **not** Steadfast / Pathao / Paperfly terms. Never
invent courier rates; real rates come only from merchant-confirmed
evidence.

## Port notes

- Ported from `prototype-p0/app/engine.py` (548 lines) to
  `app/domain/engine.py`; import path only, except one genuine bug fix
  (documented in the engine docstring and covered by
  `tests/test_engine.py::test_m1_invalid_cod_fee_rate_is_null_not_a_crash`):
  in P0 a non-integer `cod_fee_rate_bp` raised `TypeError` because it was
  read before the cleaning loop; in M1 it is cleaned like every other
  money/rate field (invalid -> NULL + `DQ_INVALID_VALUE`, no fee, no
  silent fixture fallback). No formula, status, or worked-example value
  changed.
- No P0 behaviour contradicting V2.2.1 as summarised in `~/MEMORY.md`
  was found. The one deliberate deviation from V2.2.1's original status
  text is the already-documented P0 patch: a collection-only discrepancy
  is `open`, and `mismatch` is reserved for fee / statement /
  contractual-payout findings.
- The P0 run-store immutability test (`AT-12` store part) is not ported:
  M1 Milestone 3 has no run store yet. It is replaced by an
  input-immutability test (`test_m1_input_dict_not_mutated`) and the
  determinism test. The `/api/reconcile` endpoint returns computed
  results only and persists nothing.
- `prototype-p0/` is read-only and was not modified.

## Tests

Run from `m1/backend` (venv `.venv`): `.venv/bin/python -m pytest -q`

- `tests/test_engine.py` — **34 tests passed**, 0 failed. Ports P0
  AT-1–AT-12 (V2.2.1 Ex1, Ex2, Ex4, Ex5, Ex6; V2.2 Ex A–D decomposition
  family) plus all P0 patch tests (`open` vs `mismatch`, `D_c` explicit
  confirmation + evidence, determinism, fee-line states, VAT included,
  duplicate tracking, returned/partial outcomes, half-up rounding
  1050p at 1% -> 11p, identities `V_total = C - Δ - V_R + S` and
  `V_cond = S - V_R`), plus M1 additions (invalid-rate fix, VAT
  `rate_on_gross`, unknown-VAT blocking, fixture labelling).
- `tests/test_api_reconcile.py` — **4 tests passed**, 0 failed (HTTP
  smoke tests for `POST /api/reconcile` and `GET /api/rules`).
- Total: **38 passed, 0 failed** (verified 2026-10-31).
