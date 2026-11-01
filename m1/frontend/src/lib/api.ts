/**
 * API client for the CourierLedger backend.
 *
 * All amounts are integer poisha. Never invent courier rates or real
 * merchant data here — demo data lives in src/lib/demo.ts and is labelled.
 *
 * Verified against m1/backend/app/api/ (2026-10-31, Milestone 5):
 * - GET /api/health -> {"status":"ok","service":...,"database":...}
 * - GET /api/auth/me, POST /api/auth/login|register|logout (CSRF)
 * - GET /api/auth/csrf -> {"csrf_token": "..."} + cl_csrf cookie
 * - GET /api/workspaces -> WorkspaceOut[] {id,name,trading_name,status,role,created_at}
 * - GET /api/workspaces/{id} -> WorkspaceOut
 * - GET|POST /api/workspaces/{id}/courier-accounts
 * - GET /api/workspaces/{id}/imports (list) and
 *   GET /api/workspaces/{id}/imports/{batch_id} (detail: batch + rows +
 *   truncated + errors)
 * - POST /api/workspaces/{id}/imports (multipart: file, mapping JSON,
 *   merchant_courier_account_id, optional idempotency_key)
 * - POST /api/reconcile {shipments, tolerance_poisha} -> {results, count}
 * - GET /api/rules -> {"fixtures": [...]} (SYNTHETIC FIXTURE only)
 * - POST /api/workspaces/{id}/imports/{batchId}/runs (M6, contract from the
 *   M6 brief — the backend worker is landing it in parallel; see the
 *   "Reconciliation runs (M6)" section at the bottom of this file)
 * - GET /api/workspaces/{id}/runs, GET .../runs/{runId},
 *   GET .../runs/{runId}/results, GET .../runs/{runId}/findings,
 *   GET /api/workspaces/{id}/imports/{batchId}/runs
 *
 * - GET /api/workspaces/{id}/settlement-batches, GET .../settlement-batches/{batchId},
 *   POST .../settlement-batches, POST .../settlement-batches/{batchId}/receipts,
 *   POST .../receipts/{receiptId}/complete, GET /api/workspaces/{id}/bank-receipts,
 *   POST .../settlement-batches/{batchId}/allocations,
 *   POST .../allocations/{allocationId}/reverse,
 *   POST .../settlement-batches/{batchId}/calculate-variance,
 *   GET .../settlement-batches/{batchId}/current-variance (M7, contract from
 *   the M7 brief — the backend worker is landing it in parallel; see the
 *   "Settlement batches & bank receipts (M7)" section at the bottom)
 *
 * - GET /api/workspaces/{id}/orders?limit=&offset=, POST .../orders,
 *   POST /api/workspaces/{id}/orders/{orderId}/shipments,
 *   GET /api/workspaces/{id}/shipments?limit=&offset=&order_id=&tracking_id=,
 *   GET /api/workspaces/{id}/statements?page=&page_size=&import_batch_id=&match_status=,
 *   GET /api/workspaces/{id}/statements/{lineId},
 *   GET /api/workspaces/{id}/reports/summary (M8 — verified live
 *   2026-11-01 against backend/app/api/shipments.py, statements.py and
 *   reports.py; see the "Orders, shipments, statements & reports (M8)"
 *   section at the bottom of this file. The brief's page/page_size
 *   pagination and flat report shape were NOT what the backend built:
 *   orders/shipments paginate with limit/offset, the report summary is
 *   nested with named variance totals that are always numbers (0 when
 *   empty), and the create-shipment body takes a partial-amount
 *   evidence reference, not a partial amount — this file follows the
 *   real backend, not the brief.)
 *
 * - GET|PATCH /api/workspaces/{id}/settings (M9 — verified 2026-11-01
 *   against backend/app/api/settings.py; see the "Workspace settings
 *   (M9)" section at the bottom of this file. GET returns the stored
 *   row, or the effective defaults {tolerance_poisha: 0,
 *   locale: "en_bn", is_default: true} when no row exists yet. PATCH
 *   is Owner/Admin only (CSRF), StrictInt tolerance >= 0, and an
 *   explicit null is rejected with 422 — never a silent reset.)
 *
 * - GET /api/workspaces/{id}/members, POST .../members/invite,
 *   POST .../members/{mid}/accept, PATCH .../members/{mid}/role,
 *   POST .../members/{mid}/revoke (M9 — verified 2026-11-01 against
 *   backend/app/api/members.py; see the "Workspace members (M9)"
 *   section at the bottom of this file. The list is a bare array of
 *   MemberOut; invitations target the roles admin/member/viewer only,
 *   never owner; only the invited user can accept their own
 *   invitation; invite/role/revoke are Owner/Admin only.)
 */

// Empty VITE_API_URL = same-origin, so the Vite dev proxy forwards /api/*
// to http://127.0.0.1:8000. A non-empty value points at a backend directly.
const API_BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? ''

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

let csrfToken: string | null = null

function readCookie(name: string): string | null {
  const match = document.cookie.match(
    new RegExp('(?:^|; )' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '=([^;]*)'),
  )
  return match ? decodeURIComponent(match[1]) : null
}

/** Fetch the CSRF token the backend provides (GET /api/auth/csrf). */
export async function getCsrfToken(): Promise<string | null> {
  if (csrfToken) return csrfToken
  try {
    const res = await fetch(`${API_BASE}/api/auth/csrf`, {
      credentials: 'include',
    })
    if (res.ok) {
      const data = (await res.json()) as Record<string, unknown>
      const token =
        typeof data.csrf_token === 'string'
          ? data.csrf_token
          : typeof data.csrfToken === 'string'
            ? data.csrfToken
            : null
      if (token) {
        csrfToken = token
        return token
      }
    }
  } catch {
    // Backend not reachable yet — fall through to the cookie check.
  }
  csrfToken = readCookie('csrf_token') ?? readCookie('XSRF-TOKEN') ?? readCookie('cl_csrf')
  return csrfToken
}

/** Forget a cached CSRF token (e.g. after a 403) so the next call re-fetches. */
export function clearCsrfToken(): void {
  csrfToken = null
}

export interface ApiOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
}

/** Typed fetch wrapper: JSON in/out, HttpOnly-cookie session, CSRF header.
 *
 * Phase 1 (C-6.2): on a 403 for a write, the cached CSRF token is
 * cleared and the request is retried exactly once with a freshly
 * fetched token (a stale token after login/logout was the common
 * cause). On a 401 the cached token is cleared and a
 * `cl:unauthorized` event is dispatched — App.tsx listens and
 * redirects to /login after clearing the React Query cache. The
 * ApiError is still thrown, so existing callers behave as before.
 */
export async function apiFetch<T>(path: string, options: ApiOptions = {}): Promise<T> {
  const { body, headers, method = 'GET', ...rest } = options
  const isWrite = method.toUpperCase() !== 'GET' && method.toUpperCase() !== 'HEAD'

  async function doFetch(retried: boolean): Promise<T> {
    const finalHeaders: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(headers as Record<string, string> | undefined),
    }
    if (isWrite) {
      const token = await getCsrfToken()
      if (token) finalHeaders['X-CSRF-Token'] = token
    }
    const res = await fetch(`${API_BASE}${path}`, {
      ...rest,
      method,
      headers: finalHeaders,
      credentials: 'include',
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    if (res.status === 401) {
      clearCsrfToken()
      window.dispatchEvent(new CustomEvent('cl:unauthorized'))
      throw new ApiError(res.status, await readErrorMessage(res))
    }
    if (res.status === 403 && isWrite && !retried) {
      clearCsrfToken()
      return doFetch(true)
    }
    if (!res.ok) {
      throw new ApiError(res.status, await readErrorMessage(res))
    }
    if (res.status === 204) return undefined as T
    return (await res.json()) as T
  }

  return doFetch(false)
}

/**
 * Read the backend's error message from a failed response. FastAPI
 * request-validation failures (422) carry `detail` as a LIST of
 * {loc, msg} items, not a string — surface the first item's message
 * (with its field name) instead of a bare "Request failed (422)".
 * Member/settlement 422s carry detail as {message, reason} or a
 * plain string, which are surfaced as-is.
 */
async function readErrorMessage(res: Response): Promise<string> {
  const fallback = `Request failed (${res.status})`
  try {
    const data = (await res.json()) as Record<string, unknown>
    if (typeof data.detail === 'string') return data.detail
    if (Array.isArray(data.detail) && data.detail.length > 0) {
      const first = data.detail[0] as Record<string, unknown>
      if (typeof first.msg === 'string') {
        const field = Array.isArray(first.loc)
          ? first.loc.filter((p) => p !== 'body').join('.')
          : null
        return field ? `${field}: ${first.msg}` : first.msg
      }
    }
    if (typeof data.message === 'string') return data.message
  } catch {
    // Keep the generic message.
  }
  return fallback
}

/** Multipart fetch (for the CSV upload): no JSON Content-Type header. */
export async function apiFetchForm<T>(path: string, form: FormData): Promise<T> {
  const token = await getCsrfToken()
  const headers: Record<string, string> = {}
  if (token) headers['X-CSRF-Token'] = token
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers,
    credentials: 'include',
    body: form,
  })
  if (!res.ok) {
    throw new ApiError(res.status, await readErrorMessage(res))
  }
  return (await res.json()) as T
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export interface HealthResponse {
  status: string
  version?: string
  [key: string]: unknown
}

export async function getHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>('/api/health')
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export interface CurrentUser {
  id: string
  email: string
  display_name: string | null
  status: string
}

export async function getMe(): Promise<CurrentUser> {
  return apiFetch<CurrentUser>('/api/auth/me')
}

export async function login(email: string, password: string): Promise<unknown> {
  return apiFetch('/api/auth/login', { method: 'POST', body: { email, password } })
}

export async function register(name: string, email: string, password: string): Promise<unknown> {
  return apiFetch('/api/auth/register', {
    method: 'POST',
    body: { email, password, display_name: name },
  })
}

export async function logout(): Promise<void> {
  await apiFetch('/api/auth/logout', { method: 'POST' })
}

// ---------------------------------------------------------------------------
// Workspaces
// ---------------------------------------------------------------------------

export interface Workspace {
  id: string
  name: string
  trading_name?: string | null
  status?: string
  /** The caller's role in this workspace (e.g. "owner"). */
  role?: string
  created_at?: string
}

export async function listWorkspaces(): Promise<Workspace[]> {
  return apiFetch<Workspace[]>('/api/workspaces')
}

export async function getWorkspace(id: string): Promise<Workspace> {
  return apiFetch<Workspace>(`/api/workspaces/${id}`)
}

export async function createWorkspace(name: string): Promise<Workspace> {
  return apiFetch<Workspace>('/api/workspaces', { method: 'POST', body: { name } })
}

// ---------------------------------------------------------------------------
// Courier accounts
// ---------------------------------------------------------------------------

export interface CourierAccount {
  id: string
  courier_code: string
  merchant_account_ref: string | null
  display_label: string | null
  status: string
}

export async function listCourierAccounts(workspaceId: string): Promise<CourierAccount[]> {
  return apiFetch<CourierAccount[]>(`/api/workspaces/${workspaceId}/courier-accounts`)
}

export async function createCourierAccount(
  workspaceId: string,
  body: { courier_code: string; merchant_account_ref?: string | null; display_label?: string | null },
): Promise<CourierAccount> {
  return apiFetch<CourierAccount>(`/api/workspaces/${workspaceId}/courier-accounts`, {
    method: 'POST',
    body,
  })
}

// ---------------------------------------------------------------------------
// Imports (M4)
// ---------------------------------------------------------------------------

export interface ImportBatchSummary {
  batch_id: string
  merchant_id?: string
  status: string
  row_count: number
  parsed_row_count: number
  error_row_count: number
  file_object_id: string
  mapping_snapshot: Record<string, string> | null
  idempotency_outcome?: string
  created_at: string | null
}

export async function listImports(workspaceId: string): Promise<ImportBatchSummary[]> {
  // Phase 1 (C-1.9): GET /imports is paginated and returns the
  // {imports, total, limit, offset} envelope. Older mocks / cached
  // responses may still return a bare array — accept both.
  const data = await apiFetch<ImportBatchSummary[] | { imports: ImportBatchSummary[] }>(
    `/api/workspaces/${workspaceId}/imports`,
  )
  return Array.isArray(data) ? data : data.imports
}

export type MoneyDqState =
  | 'column_absent'
  | 'blank'
  | 'invalid'
  | 'explicit_zero'
  | 'present_valid'

export interface ImportStatementLine {
  tracking_id_raw: string | null
  tracking_id_normalised: string | null
  merchant_order_ref_raw: string | null
  reported_status_raw: string | null
  statement_date: string | null
  match_status: string
  shipment_id: string | null
  reported_collected_poisha: number | null
  reported_collected_state: MoneyDqState
  reported_payout_poisha: number | null
  reported_payout_state: MoneyDqState
  reported_delivery_fee_poisha: number | null
  reported_delivery_fee_state: MoneyDqState
  reported_cod_fee_poisha: number | null
  reported_cod_fee_state: MoneyDqState
  reported_return_fee_poisha: number | null
  reported_return_fee_state: MoneyDqState
  reported_other_fee_poisha: number | null
  reported_other_fee_state: MoneyDqState
}

export interface ImportRow {
  row_number: number
  parse_status: string
  raw_values: Record<string, unknown> | null
  statement_line: ImportStatementLine | null
}

export interface ImportErrorItem {
  import_row_id: string | null
  field_name: string
  severity: string
  error_code: string
  message: string
  raw_value: string | null
}

export interface ImportBatchDetail {
  batch: ImportBatchSummary
  rows: ImportRow[]
  truncated: boolean
  errors: ImportErrorItem[]
}

export async function getImportDetail(
  workspaceId: string,
  batchId: string,
): Promise<ImportBatchDetail> {
  return apiFetch<ImportBatchDetail>(`/api/workspaces/${workspaceId}/imports/${batchId}`)
}

export async function uploadImport(
  workspaceId: string,
  file: File,
  mapping: Record<string, string>,
  merchantCourierAccountId: string,
): Promise<ImportBatchSummary> {
  const form = new FormData()
  form.append('file', file)
  form.append('mapping', JSON.stringify(mapping))
  form.append('merchant_courier_account_id', merchantCourierAccountId)
  return apiFetchForm<ImportBatchSummary>(`/api/workspaces/${workspaceId}/imports`, form)
}

// ---------------------------------------------------------------------------
// Reconciliation engine (M3) — computes only, persists nothing.
// ---------------------------------------------------------------------------

export interface ReconcileFinding {
  kind: 'data_quality' | 'financial'
  code: string
  message: string
  /** Financial finding amount in poisha (null for data-quality findings). */
  amount_poisha: number | null
  /** Financial finding basis, e.g. reported_vs_expected / reported_basis / contractual. */
  basis: string | null
}

export interface ReconcileResult {
  merchant_order_id: string | null
  tracking_id: string | null
  courier_fixture: string | null
  cod_fee_base: string | null
  cod_fee_rate_bp: number | null
  is_scenario: boolean
  outcome: string | null
  amount_to_collect_poisha: number | null
  E: number | null
  R: number | null
  D_e: number | null
  D_r: number | null
  D_c: number | null
  D_c_confirmed: boolean
  D_c_evidence: string | null
  F_e: number | null
  F_R: number | null
  O: number | null
  T_e: number | null
  T_r: number | null
  X_e: number | null
  X_R: number | null
  X_r: number | null
  X_r_total: number | null
  X_r_lines: number | null
  P_e: number | null
  P_cond: number | null
  P_r: number | null
  /** C = collection variance (reported − expected collected). */
  C: number | null
  delta: number | null
  /** V_R = fee/tax variance on the reported basis. */
  V_R: number | null
  /** S = statement arithmetic residual (reported payout − (reported collected − reported deductions)). */
  S: number | null
  /** V_total = scenario-only total variance (reported_collected base only). */
  V_total: number | null
  /** V_cond = contractual payout variance (decides contractual payout status). */
  V_cond: number | null
  vat_mode: string | null
  vat_breakdown: number | null
  fee_lines: unknown[]
  findings: ReconcileFinding[]
  contractual_payout_status: string
  data_quality_status: string
  overall_status: string
  expected_outcome_poisha: number | null
  batch_amount_owed_poisha: number | null
  note: string | null
}

export interface ReconcileResponse {
  results: ReconcileResult[]
  count: number
}

export async function postReconcile(
  shipments: Record<string, unknown>[],
  tolerancePoisha = 0,
): Promise<ReconcileResponse> {
  return apiFetch<ReconcileResponse>('/api/reconcile', {
    method: 'POST',
    body: { shipments, tolerance_poisha: tolerancePoisha },
  })
}

export interface ReconcileFixture {
  courier_fixture: string
  cod_fee_base: string
  cod_fee_rate_bp: number
  label: string
}

export async function getRules(): Promise<{ fixtures: ReconcileFixture[] }> {
  return apiFetch<{ fixtures: ReconcileFixture[] }>('/api/rules')
}

// ---------------------------------------------------------------------------
// Reconciliation runs (M6) — persisted runs created from an import.
// Verified 2026-11-01 against backend/app/api/runs.py and
// app/services/run_service.py (M9 contract fixes):
// - GET /runs/{id} returns the FLAT run dict (status + result_counts
//   at top level, results embedded) — no {run, results, total} wrapper.
// - Every run payload carries `result_counts` (all five statuses),
//   never `counts_by_status`; there is no `result_count` field.
// - POST .../imports/{batchId}/runs returns the same flat summary
//   shape with `result_counts` + `excluded_counts`.
// Amounts are integer poisha. A run's five variances (C, V_R, S, V_cond,
// V_total) are separate fields and are never summed anywhere.
// ---------------------------------------------------------------------------

export interface CountsByStatus {
  matched: number
  open: number
  mismatch: number
  needs_review: number
  matched_with_tolerance: number
}

export interface RunSummary {
  run_id: string
  merchant_id?: string
  import_batch_id: string
  merchant_courier_account_id?: string
  rule_version_id?: string | null
  rule_version_no?: number | null
  rule_content_hash?: string | null
  rule_snapshot?: unknown
  status: string
  tolerance_poisha: number
  engine_version?: string | null
  run_hash?: string | null
  created_by_user_id?: string | null
  /**
   * Counts of results by overall status — the field the backend
   * actually returns (`result_counts`, all five statuses, 0 when
   * absent). There is no `counts_by_status` on any run payload.
   */
  result_counts: CountsByStatus
  excluded_counts?: Record<string, number>
  created_at: string | null
  completed_at?: string | null
}

export interface RunFeeLine {
  fee_code: string
  component_seq?: number | null
  expected_basis_poisha?: number | null
  expected_basis_state?: string | null
  reported_basis_poisha?: number | null
  reported_basis_state?: string | null
  reported_actual_poisha?: number | null
  reported_actual_state?: string | null
  included_tax_poisha?: number | null
}

export interface RunFinding {
  category?: string | null
  finding_type?: string | null
  finding_code: string
  amount_poisha?: number | null
  is_within_tolerance?: boolean | null
  explanation_en?: string | null
  explanation_bn?: string | null
}

export interface PersistedRunResult {
  result_id: string
  shipment_id: string | null
  statement_line_id?: string | null
  /** May be absent: tracking_id is not a column of reconciliation_results. */
  tracking_id?: string | null
  expected_collectible_poisha: number | null
  reported_collected_poisha: number | null
  c_poisha: number | null
  x_e_poisha?: number | null
  x_r_poisha?: number | null
  x_upper_r_poisha?: number | null
  delta_poisha: number | null
  v_r_poisha: number | null
  p_e_poisha: number | null
  p_cond_poisha: number | null
  p_r_poisha: number | null
  s_poisha: number | null
  v_cond_poisha: number | null
  v_total_poisha: number | null
  contractual_payout_status: string
  data_quality_status: string
  overall_status: string
  fee_lines: RunFeeLine[]
  findings: RunFinding[]
}

/**
 * GET /runs/{run_id} returns the FLAT run dict (the run's own fields
 * at top level — status, result_counts, rule_snapshot, engine_version
 * — plus the embedded results and the two snapshots). There is no
 * `run` wrapper and no `total`: the full result list is embedded.
 * (Verified 2026-11-01 against backend/app/services/run_service.py
 * get_run_detail / _run_dict, and live by the Playwright e2e suite.)
 */
export interface RunDetailResponse extends RunSummary {
  input_snapshot?: unknown
  mapping_snapshot?: unknown
  results: PersistedRunResult[]
}

export interface RunsListResponse {
  runs: RunSummary[]
  total: number
  limit: number
  offset: number
}

export interface RunResultsResponse {
  results: PersistedRunResult[]
  total: number
  limit: number
  offset: number
}

export interface RunFindingsResponse {
  findings: RunFinding[]
  total: number
  limit: number
  offset: number
}

export interface BatchRunsResponse {
  runs: RunSummary[]
  total: number
}

export interface CreateRunBody {
  courier_fixture?: string
  rule_version_id?: string
  tolerance_poisha?: number
  idempotency_key?: string
}

export interface CreateRunResponse {
  run_id: string
  merchant_id?: string
  import_batch_id: string
  status: string
  tolerance_poisha: number
  result_counts: CountsByStatus
  excluded_counts?: Record<string, number>
  created_at?: string | null
  idempotency_outcome?: string
}

export async function createRunForImport(
  workspaceId: string,
  batchId: string,
  body: CreateRunBody,
): Promise<CreateRunResponse> {
  return apiFetch<CreateRunResponse>(
    `/api/workspaces/${workspaceId}/imports/${batchId}/runs`,
    { method: 'POST', body },
  )
}

export async function listRuns(
  workspaceId: string,
  limit = 20,
  offset = 0,
): Promise<RunsListResponse> {
  return apiFetch<RunsListResponse>(
    `/api/workspaces/${workspaceId}/runs?limit=${limit}&offset=${offset}`,
  )
}

export async function getRun(workspaceId: string, runId: string): Promise<RunDetailResponse> {
  return apiFetch<RunDetailResponse>(`/api/workspaces/${workspaceId}/runs/${runId}`)
}

export async function listRunResults(
  workspaceId: string,
  runId: string,
  limit = 20,
  offset = 0,
): Promise<RunResultsResponse> {
  return apiFetch<RunResultsResponse>(
    `/api/workspaces/${workspaceId}/runs/${runId}/results?limit=${limit}&offset=${offset}`,
  )
}

export async function listRunFindings(
  workspaceId: string,
  runId: string,
  limit = 20,
  offset = 0,
): Promise<RunFindingsResponse> {
  return apiFetch<RunFindingsResponse>(
    `/api/workspaces/${workspaceId}/runs/${runId}/findings?limit=${limit}&offset=${offset}`,
  )
}

export async function listBatchRuns(
  workspaceId: string,
  batchId: string,
): Promise<BatchRunsResponse> {
  return apiFetch<BatchRunsResponse>(`/api/workspaces/${workspaceId}/imports/${batchId}/runs`)
}

// ---------------------------------------------------------------------------
// Settlement batches & bank receipts (M7)
// Verified 2026-11-01 against backend/app/api/settlements.py and
// app/services/settlement_service.py (M9 contract fixes):
// - GET /settlement-batches returns the paginated envelope
//   {settlement_batches, total, limit, offset}, not a bare array.
// - GET /settlement-batches/{id} returns the FLAT batch dict (no
//   `batch` wrapper; net field is `net_allocated_total_poisha`).
// - GET /bank-receipts returns the paginated envelope
//   {receipts, total, limit, offset}, not a bare array.
// Amounts are integer poisha. Bank-confirmed receipts are kept separate
// from courier-reported payouts — a variance is shown for review, never
// automatically labelled a loss.
// ---------------------------------------------------------------------------

export type AllocationStatus = 'unallocated' | 'partially_allocated' | 'fully_allocated'

export type BankStatus =
  | 'no_receipt'
  | 'partial_receipt'
  | 'receipt_complete_unallocated'
  | 'fully_reconciled'

export interface SettlementBatch {
  batch_id: string
  merchant_courier_account_id: string
  run_id: string | null
  period_start: string | null
  period_end: string | null
  expected_batch_payout_poisha: number | null
  allocated_total_poisha: number | null
  allocation_status: AllocationStatus
  bank_status: BankStatus
  current_bank_variance_poisha: number | null
  created_at: string | null
}

export interface SettlementBatchReceipt {
  receipt_id: string
  amount_poisha: number
  received_on: string | null
  receipt_ref_label: string | null
  is_complete: boolean
  created_at: string | null
}

export interface SettlementAllocation {
  allocation_id: string
  bank_receipt_id: string
  allocated_poisha: number
  kind: 'allocation' | 'reversal'
  reverses_allocation_id: string | null
  created_at: string | null
}

export interface VarianceCalculation {
  calculation_seq: number
  expected_batch_payout_poisha: number | null
  allocated_bank_confirmed_total_poisha: number | null
  bank_variance_poisha: number | null
  computed_at: string | null
}

export type VarianceStatus =
  | 'ok'
  | 'no_calculation'
  | 'not_fully_allocated'
  | 'receipt_incomplete'
  | 'expected_changed'
  | 'input_set_changed'

/**
 * GET /settlement-batches/{id} returns the FLAT batch dict (the
 * batch's own fields at top level — no `batch` wrapper — plus
 * receipts, allocations, the net total, the variance history, the
 * current variance and its status). The net field is
 * `net_allocated_total_poisha` (allocations minus reversals), not
 * `net_allocated_poisha`. (Verified 2026-11-01 against
 * backend/app/services/settlement_service.py get_batch_detail /
 * _batch_dict, and live by the Playwright e2e suite.)
 */
export interface SettlementBatchDetail extends SettlementBatch {
  receipts: SettlementBatchReceipt[]
  allocations: SettlementAllocation[]
  net_allocated_total_poisha: number | null
  variance_calculations: VarianceCalculation[]
  variance_status: VarianceStatus
  current_calculation_seq: number | null
}

/** GET /settlement-batches returns this paginated envelope, not a bare array. */
export interface SettlementBatchesListResponse {
  settlement_batches: SettlementBatch[]
  total: number
  limit: number
  offset: number
}

/** GET /bank-receipts returns this paginated envelope, not a bare array. */
export interface BankReceiptsListResponse {
  receipts: BankReceipt[]
  total: number
  limit: number
  offset: number
}

export interface BankReceipt {
  receipt_id: string
  settlement_batch_id: string | null
  amount_poisha: number
  received_on: string | null
  receipt_ref_label: string | null
  is_complete: boolean
  created_at: string | null
}

export interface CreateSettlementBatchBody {
  merchant_courier_account_id: string
  run_id?: string | null
  period_start?: string | null
  period_end?: string | null
  expected_batch_payout_poisha?: number | null
}

export interface CreateReceiptBody {
  amount_poisha: number
  received_on?: string | null
  receipt_ref_label?: string | null
  evidence_note?: string | null
}

export interface CalculateVarianceResponse {
  calculation_seq: number
  expected_batch_payout_poisha: number | null
  allocated_bank_confirmed_total_poisha: number | null
  bank_variance_poisha: number | null
}

export interface CurrentVarianceResponse {
  bank_variance_poisha: number | null
  calculation_seq: number | null
  variance_status: VarianceStatus
}

export async function listSettlementBatches(
  workspaceId: string,
): Promise<SettlementBatchesListResponse> {
  return apiFetch<SettlementBatchesListResponse>(
    `/api/workspaces/${workspaceId}/settlement-batches`,
  )
}

export async function getSettlementBatch(
  workspaceId: string,
  batchId: string,
): Promise<SettlementBatchDetail> {
  return apiFetch<SettlementBatchDetail>(
    `/api/workspaces/${workspaceId}/settlement-batches/${batchId}`,
  )
}

export async function createSettlementBatch(
  workspaceId: string,
  body: CreateSettlementBatchBody,
): Promise<SettlementBatch> {
  return apiFetch<SettlementBatch>(`/api/workspaces/${workspaceId}/settlement-batches`, {
    method: 'POST',
    body,
  })
}

export async function addBankReceipt(
  workspaceId: string,
  batchId: string,
  body: CreateReceiptBody,
): Promise<SettlementBatchReceipt> {
  return apiFetch<SettlementBatchReceipt>(
    `/api/workspaces/${workspaceId}/settlement-batches/${batchId}/receipts`,
    { method: 'POST', body },
  )
}

export async function completeBankReceipt(
  workspaceId: string,
  batchId: string,
  receiptId: string,
): Promise<SettlementBatchReceipt> {
  return apiFetch<SettlementBatchReceipt>(
    `/api/workspaces/${workspaceId}/settlement-batches/${batchId}/receipts/${receiptId}/complete`,
    { method: 'POST' },
  )
}

export async function listBankReceipts(
  workspaceId: string,
): Promise<BankReceiptsListResponse> {
  return apiFetch<BankReceiptsListResponse>(`/api/workspaces/${workspaceId}/bank-receipts`)
}

export async function createAllocation(
  workspaceId: string,
  batchId: string,
  body: { bank_receipt_id: string; allocated_poisha: number },
): Promise<SettlementAllocation> {
  return apiFetch<SettlementAllocation>(
    `/api/workspaces/${workspaceId}/settlement-batches/${batchId}/allocations`,
    { method: 'POST', body },
  )
}

export async function reverseAllocation(
  workspaceId: string,
  batchId: string,
  allocationId: string,
): Promise<SettlementAllocation> {
  return apiFetch<SettlementAllocation>(
    `/api/workspaces/${workspaceId}/settlement-batches/${batchId}/allocations/${allocationId}/reverse`,
    { method: 'POST' },
  )
}

export async function calculateVariance(
  workspaceId: string,
  batchId: string,
): Promise<CalculateVarianceResponse> {
  return apiFetch<CalculateVarianceResponse>(
    `/api/workspaces/${workspaceId}/settlement-batches/${batchId}/calculate-variance`,
    { method: 'POST' },
  )
}

export async function getCurrentVariance(
  workspaceId: string,
  batchId: string,
): Promise<CurrentVarianceResponse> {
  return apiFetch<CurrentVarianceResponse>(
    `/api/workspaces/${workspaceId}/settlement-batches/${batchId}/current-variance`,
  )
}

// ---------------------------------------------------------------------------
// Orders, shipments, courier statements & reports (M8)
// Verified live 2026-11-01 against backend/app/api/shipments.py,
// statements.py and reports.py (the backend workers landed them during
// this task). Where the brief's contract differed from the real
// backend, this section follows the REAL backend:
// - Orders and shipments paginate with limit/offset, NOT page/page_size.
//   Envelopes are {orders|shipments, total, limit, offset}.
// - Order COD may be NULL (unknown); the create-order body sends it
//   only when the merchant confirms it.
// - A shipment has NO status / merchant_order_ref / amount_to_collect
//   fields in the list payload. It carries tracking_id (raw) +
//   tracking_id_normalised, outcome, outcome_source, is_cod_bearing,
//   is_superseded, expected_collectible_cod_poisha (stored, server-
//   derived) + expected_collectible_derived_poisha, and needs_review +
//   needs_review_reason computed at read time.
// - The create-shipment body takes partial_amount_evidence_ref (a
//   text reference), NOT partial_amount_poisha — sending the latter
//   is a 422 (extra="forbid"). amount_to_collect_poisha is rejected
//   (422) for returned / cancelled / in_transit / unknown outcomes.
// - Statement lines nest their linked shipment, courier account and
//   import batch as objects; there is a detail endpoint
//   GET /statements/{lineId}.
// - The report summary is NESTED (reconciliation_runs /
//   reconciliation_results / settlement_batches / bank_receipts /
//   import_batches), and each variance total is an object
//   {total_poisha, non_null_count, null_count} that is ALWAYS a
//   number — 0 when there are no results, never null.
// Amounts are integer poisha. Expected collectible COD may be NULL
// (unknown — never shown as 0). The five variance totals in the report
// summary are separate fields and are never summed anywhere.
// ---------------------------------------------------------------------------

export interface Order {
  order_id: string
  merchant_id?: string
  merchant_order_ref: string
  /** NULL when the order COD is not confirmed — never assumed. */
  order_cod_poisha: number | null
  currency?: string
  ordered_at?: string | null
  source?: string
  created_at: string | null
  updated_at?: string | null
}

export interface OrdersListResponse {
  orders: Order[]
  total: number
  limit: number
  offset: number
}

export interface CreateOrderBody {
  merchant_order_ref: string
  order_cod_poisha: number | null
}

export async function listOrders(
  workspaceId: string,
  limit = 20,
  offset = 0,
): Promise<OrdersListResponse> {
  return apiFetch<OrdersListResponse>(
    `/api/workspaces/${workspaceId}/orders?limit=${limit}&offset=${offset}`,
  )
}

export async function createOrder(
  workspaceId: string,
  body: CreateOrderBody,
): Promise<Order> {
  return apiFetch<Order>(`/api/workspaces/${workspaceId}/orders`, {
    method: 'POST',
    body,
  })
}

export type ShipmentOutcome =
  | 'delivered'
  | 'partially_delivered'
  | 'returned'
  | 'cancelled'
  | 'unknown'
  | 'in_transit'

export type OutcomeSource =
  | 'merchant_confirmed'
  | 'statement'
  | 'system_inferred_label_only'

/** Machine-readable reason the backend computed needs_review for. */
export type NeedsReviewReason =
  | 'outcome_not_confirmed'
  | 'order_cod_unknown'
  | 'split_amount_differs_from_order_total'
  | 'partial_amount_not_documented'

export interface Shipment {
  shipment_id: string
  merchant_id?: string
  order_id: string
  merchant_courier_account_id: string | null
  /** Raw tracking id as entered (may be null when blank). */
  tracking_id: string | null
  tracking_id_normalised: string | null
  outcome: string
  outcome_source: string | null
  /**
   * Expected collectible COD in poisha, derived server-side from the
   * confirmed outcome. NULL means unknown (outcome unknown /
   * in-transit, partial without documented amount, or order COD
   * unknown) — never 0. Verified 0 only for a confirmed return /
   * cancellation / superseded shipment.
   */
  expected_collectible_cod_poisha: number | null
  expected_collectible_derived_poisha: number | null
  partial_amount_evidence_ref: string | null
  is_cod_bearing: boolean
  is_superseded: boolean
  superseded_by_shipment_id: string | null
  superseded_reason: string | null
  shipped_on: string | null
  /** Computed at read time — true exactly when the collectible could
   * not be fixed from confirmed information. */
  needs_review: boolean
  needs_review_reason: NeedsReviewReason | null
  created_at?: string | null
  updated_at?: string | null
}

export interface ShipmentsListResponse {
  shipments: Shipment[]
  total: number
  limit: number
  offset: number
}

export interface CreateShipmentBody {
  tracking_id: string
  outcome: ShipmentOutcome
  outcome_source?: OutcomeSource
  is_cod_bearing?: boolean
  /**
   * Only for delivered / partially_delivered. The backend rejects it
   * (422) for returned / cancelled / in_transit / unknown — the
   * form must not send it for those outcomes. For a partial delivery
   * the amount only counts together with partial_amount_evidence_ref.
   */
  amount_to_collect_poisha?: number | null
  /** Text reference (e.g. "courier message 12"), not an amount. */
  partial_amount_evidence_ref?: string | null
  merchant_courier_account_id?: string | null
  shipped_on?: string | null
}

export async function createShipmentForOrder(
  workspaceId: string,
  orderId: string,
  body: CreateShipmentBody,
): Promise<Shipment> {
  return apiFetch<Shipment>(
    `/api/workspaces/${workspaceId}/orders/${orderId}/shipments`,
    { method: 'POST', body },
  )
}

export async function getShipment(workspaceId: string, shipmentId: string): Promise<Shipment> {
  return apiFetch<Shipment>(`/api/workspaces/${workspaceId}/shipments/${shipmentId}`)
}

export interface ListShipmentsParams {
  order_id?: string
  tracking_id?: string
  limit?: number
  offset?: number
}

export async function listShipments(
  workspaceId: string,
  params: ListShipmentsParams = {},
): Promise<ShipmentsListResponse> {
  const query = new URLSearchParams()
  if (params.order_id) query.set('order_id', params.order_id)
  if (params.tracking_id) query.set('tracking_id', params.tracking_id)
  query.set('limit', String(params.limit ?? 20))
  query.set('offset', String(params.offset ?? 0))
  return apiFetch<ShipmentsListResponse>(
    `/api/workspaces/${workspaceId}/shipments?${query.toString()}`,
  )
}

export type StatementMatchStatus =
  | 'strong_matched'
  | 'human_confirmed'
  | 'ambiguous'
  | 'duplicate'
  | 'unmatched'

export interface StatementShipment {
  shipment_id: string
  tracking_id_raw: string | null
  tracking_id_normalised: string | null
  outcome: string
  expected_collectible_cod_poisha: number | null
  is_cod_bearing: boolean
}

export interface StatementCourierAccount {
  merchant_courier_account_id: string
  courier_code: string | null
  display_label: string | null
  merchant_account_ref: string | null
  status: string
}

export interface StatementImportBatch {
  batch_id: string
  status: string
  statement_period_start: string | null
  statement_period_end: string | null
  row_count: number
  parsed_row_count: number
  error_row_count: number
  created_at: string | null
}

export interface StatementLine {
  statement_line_id: string
  import_batch_id: string
  import_row_id: string
  row_number: number | null
  /** May be null when the import row is missing. */
  parse_status: string | null
  match_status: string
  tracking_id_raw: string | null
  tracking_id_normalised: string | null
  merchant_order_ref_raw: string | null
  reported_status_raw: string | null
  statement_date: string | null
  reported_collected_poisha: number | null
  reported_collected_state?: MoneyDqState
  reported_payout_poisha: number | null
  reported_payout_state?: MoneyDqState
  reported_delivery_fee_poisha: number | null
  reported_delivery_fee_state?: MoneyDqState
  reported_cod_fee_poisha: number | null
  reported_cod_fee_state?: MoneyDqState
  reported_return_fee_poisha: number | null
  reported_return_fee_state?: MoneyDqState
  reported_other_fee_poisha: number | null
  reported_other_fee_state?: MoneyDqState
  shipment_id: string | null
  /** The linked shipment, when matched — null otherwise. */
  shipment: StatementShipment | null
  courier_account: StatementCourierAccount | null
  import_batch: StatementImportBatch | null
  /** Raw values verbatim from the imported file, when provided. */
  raw_values?: Record<string, unknown> | null
  created_at?: string | null
}

export interface StatementsListResponse {
  statements: StatementLine[]
  total: number
  page: number
  page_size: number
}

export interface ListStatementsParams {
  import_batch_id?: string
  match_status?: string
  page?: number
  page_size?: number
}

export async function listStatements(
  workspaceId: string,
  params: ListStatementsParams = {},
): Promise<StatementsListResponse> {
  const query = new URLSearchParams()
  if (params.import_batch_id) query.set('import_batch_id', params.import_batch_id)
  if (params.match_status) query.set('match_status', params.match_status)
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  return apiFetch<StatementsListResponse>(
    `/api/workspaces/${workspaceId}/statements?${query.toString()}`,
  )
}

export async function getStatement(
  workspaceId: string,
  lineId: string,
): Promise<StatementLine> {
  return apiFetch<StatementLine>(`/api/workspaces/${workspaceId}/statements/${lineId}`)
}

/**
 * Workspace-wide aggregates, nested by area. Counts are 0 when empty.
 * Each variance total sums only that variance's non-NULL values over
 * stored results, with the NULL count beside it — five separate
 * measures, never added together. Totals are ALWAYS numbers (0 when
 * there are no results), never null.
 */
export interface ReportVarianceTotal {
  total_poisha: number
  non_null_count: number
  null_count: number
}

export type ReportVarianceKey =
  | 'collection_c'
  | 'fee_v_r'
  | 'statement_s'
  | 'conditional_payout_v_cond'
  | 'scenario_v_total'

export interface ReportSummary {
  workspace_id: string
  reconciliation_runs: {
    total: number
    by_status: Record<string, number>
  }
  reconciliation_results: {
    total: number
    by_overall_status: CountsByStatus
    variances_poisha: Record<ReportVarianceKey, ReportVarianceTotal>
  }
  settlement_batches: {
    total: number
    by_allocation_status: Record<string, number>
    by_bank_status: Record<string, number>
  }
  bank_receipts: {
    total: number
    complete: number
    incomplete: number
  }
  import_batches: {
    total: number
    by_status: Record<string, number>
  }
}

export async function getReportSummary(workspaceId: string): Promise<ReportSummary> {
  return apiFetch<ReportSummary>(`/api/workspaces/${workspaceId}/reports/summary`)
}

// ---------------------------------------------------------------------------
// Workspace settings (M9)
// Verified 2026-11-01 against backend/app/api/settings.py:
// - GET /api/workspaces/{id}/settings returns the stored settings, or
//   the effective defaults when no row exists yet:
//   {tolerance_poisha: 0, locale: "en_bn", is_default: true}.
//   Reading never creates a row. Any active member may read.
// - PATCH .../settings is Owner/Admin only (CSRF). It creates the row
//   when none exists. Only the two fields below exist on the backend
//   model — no other preference is invented here. An explicit null is
//   rejected with 422; send 0 to restore the default tolerance.
// - A tolerance change affects future runs only: each run copies the
//   tolerance it used. Amounts are integer poisha.
// ---------------------------------------------------------------------------

export interface WorkspaceSettings {
  workspace_id: string
  tolerance_poisha: number
  locale: 'en' | 'en_bn'
  /** True when no settings row exists yet and these are the defaults. */
  is_default: boolean
  updated_by_user_id: string | null
  updated_at: string | null
}

export interface UpdateWorkspaceSettingsBody {
  tolerance_poisha?: number
  locale?: 'en' | 'en_bn'
}

export async function getWorkspaceSettings(workspaceId: string): Promise<WorkspaceSettings> {
  return apiFetch<WorkspaceSettings>(`/api/workspaces/${workspaceId}/settings`)
}

export async function updateWorkspaceSettings(
  workspaceId: string,
  body: UpdateWorkspaceSettingsBody,
): Promise<WorkspaceSettings> {
  return apiFetch<WorkspaceSettings>(`/api/workspaces/${workspaceId}/settings`, {
    method: 'PATCH',
    body,
  })
}

// ---------------------------------------------------------------------------
// Workspace members (M9)
// Verified 2026-11-01 against backend/app/api/members.py and
// app/services/member_service.py:
// - GET /api/workspaces/{id}/members returns a BARE ARRAY of MemberOut
//   (any active member may list; non-members get 404).
// - POST .../members/invite {email, role} invites an EXISTING user by
//   email (unknown email -> 422 "unknown_email"); role is one of
//   admin/member/viewer — 'owner' is never invitable (422). A new
//   invitation is 201; re-inviting an already-invited (or revoked)
//   membership is 200. Owner/Admin only (CSRF).
// - POST .../members/{mid}/accept accepts an invitation — only the
//   invited user themselves, and only while status is 'invited'.
// - PATCH .../members/{mid}/role {role} changes a role among
//   admin/member/viewer; the owner role cannot be set or changed here.
//   Owner/Admin only (CSRF).
// - POST .../members/{mid}/revoke sets status='revoked' (idempotent).
//   Owner/Admin only (CSRF). A revoked membership can be restored by
//   inviting the user again.
// - POST .../members/transfer-owner {user_id} transfers ownership to
//   another ACTIVE member (M10 step 2). Only the current owner may
//   call it (403 otherwise); non-members get 404. The old owner
//   becomes an admin, not a plain member. The target is identified
//   by USER id (the members list carries user_id). Non-member,
//   revoked or merely-invited targets are 422; the last-owner guard
//   still holds after a transfer. Returns the NEW owner's MemberOut.
// Statuses: 'invited' | 'active' | 'revoked'.
// ---------------------------------------------------------------------------

export interface WorkspaceMember {
  /** Membership id (not the user id) — used in accept/role/revoke URLs. */
  id: string
  user_id: string
  email: string | null
  display_name: string | null
  role: string
  status: string
  /** Email of the user who sent the invitation, if any. */
  invited_by: string | null
  created_at: string | null
  updated_at: string | null
}

/** Roles an invitation or role change may target — never 'owner'. */
export const INVITABLE_ROLES = ['admin', 'member', 'viewer'] as const

export async function listMembers(workspaceId: string): Promise<WorkspaceMember[]> {
  return apiFetch<WorkspaceMember[]>(`/api/workspaces/${workspaceId}/members`)
}

export async function inviteMember(
  workspaceId: string,
  body: { email: string; role: string },
): Promise<WorkspaceMember> {
  return apiFetch<WorkspaceMember>(`/api/workspaces/${workspaceId}/members/invite`, {
    method: 'POST',
    body,
  })
}

export async function acceptInvitation(
  workspaceId: string,
  membershipId: string,
): Promise<WorkspaceMember> {
  return apiFetch<WorkspaceMember>(
    `/api/workspaces/${workspaceId}/members/${membershipId}/accept`,
    { method: 'POST' },
  )
}

export async function changeMemberRole(
  workspaceId: string,
  membershipId: string,
  role: string,
): Promise<WorkspaceMember> {
  return apiFetch<WorkspaceMember>(
    `/api/workspaces/${workspaceId}/members/${membershipId}/role`,
    { method: 'PATCH', body: { role } },
  )
}

export async function revokeMember(
  workspaceId: string,
  membershipId: string,
): Promise<WorkspaceMember> {
  return apiFetch<WorkspaceMember>(
    `/api/workspaces/${workspaceId}/members/${membershipId}/revoke`,
    { method: 'POST' },
  )
}

export async function transferOwnership(
  workspaceId: string,
  userId: string,
): Promise<WorkspaceMember> {
  return apiFetch<WorkspaceMember>(
    `/api/workspaces/${workspaceId}/members/transfer-owner`,
    { method: 'POST', body: { user_id: userId } },
  )
}
