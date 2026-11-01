// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ReconciliationPage } from './pages/Reconciliation'
import { RunDetailPage } from './pages/Runs'
import { ImportDetailPage } from './pages/ImportDetail'

const WS_ID = '11111111-1111-1111-1111-111111111111'
const BATCH_ID = '33333333-3333-3333-3333-333333333333'
const RUN_ID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
const RUN2_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
const SHIPMENT_ID = 'cccccccc-cccc-cccc-cccc-cccccccccccc'

const MOCK_WORKSPACES = [
  {
    id: WS_ID,
    name: 'Test Shop — Dhaka',
    trading_name: null,
    status: 'active',
    role: 'owner',
    created_at: '2026-10-31T00:00:00Z',
  },
]

const MOCK_RUNS = {
  runs: [
    {
      run_id: RUN_ID,
      merchant_id: WS_ID,
      import_batch_id: BATCH_ID,
      status: 'completed',
      tolerance_poisha: 0,
      result_counts: {
        matched: 1,
        open: 1,
        mismatch: 0,
        needs_review: 0,
        matched_with_tolerance: 0,
      },
      created_at: '2026-10-31T14:00:00Z',
      completed_at: '2026-10-31T14:00:05Z',
    },
    {
      run_id: RUN2_ID,
      merchant_id: WS_ID,
      import_batch_id: BATCH_ID,
      status: 'completed',
      tolerance_poisha: 0,
      result_counts: {
        matched: 0,
        open: 0,
        mismatch: 1,
        needs_review: 0,
        matched_with_tolerance: 0,
      },
      created_at: '2026-10-31T13:00:00Z',
      completed_at: '2026-10-31T13:00:05Z',
    },
  ],
  total: 2,
  limit: 20,
  offset: 0,
}

const MOCK_FINDING = {
  category: 'collection',
  finding_type: 'financial',
  finding_code: 'COLLECTION_SHORTFALL',
  amount_poisha: -5000,
  is_within_tolerance: false,
  explanation_en: 'Reported collection is ৳50.00 less than expected.',
  explanation_bn: 'রিপোর্ট করা সংগ্রহ প্রত্যাশার চেয়ে ৳৫০.০০ কম।',
}

const MOCK_RESULT_WITH_TRACKING = {
  result_id: 'dddddddd-dddd-dddd-dddd-dddddddddddd',
  shipment_id: SHIPMENT_ID,
  statement_line_id: 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee',
  tracking_id: 'SF-1001',
  expected_collectible_poisha: 100000,
  reported_collected_poisha: 95000,
  c_poisha: -5000,
  x_e_poisha: 8000,
  x_r_poisha: 7950,
  x_upper_r_poisha: 7950,
  delta_poisha: -50,
  v_r_poisha: 0,
  p_e_poisha: 92000,
  p_cond_poisha: 87050,
  p_r_poisha: 87050,
  s_poisha: 0,
  v_cond_poisha: 0,
  v_total_poisha: -4950,
  contractual_payout_status: 'matched',
  data_quality_status: 'ok',
  overall_status: 'open',
  fee_lines: [
    {
      fee_code: 'delivery_fee',
      component_seq: 1,
      expected_basis_poisha: 7000,
      expected_basis_state: 'present_valid',
      reported_basis_poisha: 7000,
      reported_basis_state: 'present_valid',
      reported_actual_poisha: 7000,
      reported_actual_state: 'present_valid',
      included_tax_poisha: 0,
    },
  ],
  findings: [MOCK_FINDING],
}

/** Same shape, but the backend provides no tracking_id for this result. */
const MOCK_RESULT_NO_TRACKING = {
  ...MOCK_RESULT_WITH_TRACKING,
  result_id: 'ffffffff-ffff-ffff-ffff-ffffffffffff',
  tracking_id: undefined,
  expected_collectible_poisha: 50000,
  reported_collected_poisha: 50000,
  c_poisha: 0,
  v_r_poisha: 0,
  s_poisha: 0,
  v_cond_poisha: 0,
  v_total_poisha: 0,
  delta_poisha: 0,
  p_e_poisha: 46000,
  p_cond_poisha: 46000,
  p_r_poisha: 46000,
  overall_status: 'matched',
  fee_lines: [],
  findings: [],
}

/**
 * GET /runs/{id} returns the FLAT run dict (run fields at top level,
 * results embedded) — no {run, results, total} wrapper (M9-BUG-1).
 */
const MOCK_RUN_DETAIL = {
  ...MOCK_RUNS.runs[0],
  rule_snapshot: { courier_fixture: 'Fixture Alpha' },
  engine_version: 'm1-engine-1',
  input_snapshot: null,
  mapping_snapshot: null,
  results: [],
}

const MOCK_IMPORT_DETAIL = {
  batch: {
    batch_id: BATCH_ID,
    status: 'parsed',
    row_count: 0,
    parsed_row_count: 0,
    error_row_count: 0,
    file_object_id: '44444444-4444-4444-4444-444444444444',
    mapping_snapshot: { tracking_id: 'Tracking ID' },
    created_at: '2026-10-31T12:00:00Z',
  },
  rows: [],
  truncated: false,
  errors: [],
}

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function renderWithClient(ui: React.ReactElement) {
  return render(<QueryClientProvider client={makeClient()}>{ui}</QueryClientProvider>)
}

type FetchHandler = (url: string, init?: RequestInit) => Promise<unknown>

function installFetch(handler: FetchHandler) {
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const payload = await handler(url, init)
    return { ok: true, status: 200, json: async () => payload }
  }) as unknown as typeof fetch
}

/** Shared routes for every M6 test: workspaces, rules, demo reconcile. */
function baseHandler(url: string, init?: RequestInit): Promise<unknown> {
  if (url === '/api/workspaces') return Promise.resolve(MOCK_WORKSPACES)
  if (url === '/api/auth/csrf') return Promise.resolve({ csrf_token: 'test-csrf' })
  if (url === '/api/rules') {
    return Promise.resolve({
      fixtures: [
        { courier_fixture: 'Fixture Alpha', cod_fee_base: 'full_cod', cod_fee_rate_bp: 100, label: 'Alpha' },
        { courier_fixture: 'Fixture Beta', cod_fee_base: 'full_cod', cod_fee_rate_bp: 150, label: 'Beta' },
        { courier_fixture: 'Fixture Gamma', cod_fee_base: 'full_cod', cod_fee_rate_bp: 200, label: 'Gamma' },
      ],
    })
  }
  if (url === '/api/reconcile' && init?.method === 'POST') {
    return Promise.resolve({ results: [], count: 0 })
  }
  throw new Error(`unexpected fetch URL in test: ${url} ${init?.method ?? 'GET'}`)
}

// This repo has no auto-cleanup between tests: every render must be
// removed explicitly, or later tests see earlier tests' DOM.
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ReconciliationPage — persisted runs (M6)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders the runs list from the mocked list payload, with counts and Bangla labels', async () => {
    installFetch((url, init) => {
      if (url === `/api/workspaces/${WS_ID}/runs?limit=20&offset=0`) return Promise.resolve(MOCK_RUNS)
      return baseHandler(url, init)
    })
    renderWithClient(
      <MemoryRouter>
        <ReconciliationPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('aaaaaaaa…')).toBeInTheDocument()
    })
    expect(screen.getByText('bbbbbbbb…')).toBeInTheDocument()
    // Counts by status for run 1: matched 1, open 1, the rest 0.
    expect(
      screen.getByText('matched 1 · mismatch 0 · needs_review 0 · open 1 · matched_with_tolerance 0'),
    ).toBeInTheDocument()
    // Counts by status for run 2: mismatch 1 only.
    expect(
      screen.getByText('matched 0 · mismatch 1 · needs_review 0 · open 0 · matched_with_tolerance 0'),
    ).toBeInTheDocument()
    // Bangla status explanations are shown under the runs table.
    expect(screen.getAllByText(/মিলেছে/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/খোলা/).length).toBeGreaterThan(0)
    // The synthetic demo section is kept below the real runs.
    expect(screen.getAllByText('SYNTHETIC DEMO').length).toBeGreaterThan(0)
    expect(screen.getByText('Demo reconciliation run')).toBeInTheDocument()
  })

  it('says plainly when there are no runs yet', async () => {
    installFetch((url, init) => {
      if (url === `/api/workspaces/${WS_ID}/runs?limit=20&offset=0`) {
        return Promise.resolve({ runs: [], total: 0, limit: 20, offset: 0 })
      }
      return baseHandler(url, init)
    })
    renderWithClient(
      <MemoryRouter>
        <ReconciliationPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(
        screen.getByText('No reconciliation runs yet — create one from an import.'),
      ).toBeInTheDocument()
    })
  })
})

describe('RunDetailPage — persisted run detail (M6)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders the five variances separately, findings and fee lines', async () => {
    installFetch((url, init) => {
      if (url === `/api/workspaces/${WS_ID}/runs/${RUN_ID}`) return Promise.resolve(MOCK_RUN_DETAIL)
      if (url === `/api/workspaces/${WS_ID}/runs/${RUN_ID}/results?limit=20&offset=0`) {
        return Promise.resolve({
          results: [MOCK_RESULT_WITH_TRACKING, MOCK_RESULT_NO_TRACKING],
          total: 2,
          limit: 20,
          offset: 0,
        })
      }
      if (url === `/api/workspaces/${WS_ID}/runs/${RUN_ID}/findings?limit=20&offset=0`) {
        return Promise.resolve({ findings: [MOCK_FINDING], total: 1, limit: 20, offset: 0 })
      }
      if (url === `/api/workspaces/${WS_ID}/shipments/${SHIPMENT_ID}`) {
        // The run payload carries no tracking id, so the page resolves
        // it through GET /shipments/{id}.
        return Promise.resolve({
          shipment_id: SHIPMENT_ID,
          tracking_id: 'SF-2002',
          tracking_id_normalised: 'SF-2002',
          outcome: 'delivered',
          expected_collectible_cod_poisha: 50000,
          is_cod_bearing: true,
        })
      }
      return baseHandler(url, init)
    })
    renderWithClient(
      <MemoryRouter initialEntries={[`/runs/${RUN_ID}`]}>
        <Routes>
          <Route path="/runs/:runId" element={<RunDetailPage />} />
        </Routes>
      </MemoryRouter>,
    )
    // Header + counts.
    await waitFor(() => {
      expect(screen.getByText('completed')).toBeInTheDocument()
    })
    expect(screen.getByText(/Engine m1-engine-1/)).toBeInTheDocument()
    // Result with tracking: the five variances are shown as separate
    // cells — C −৳50.00 and the scenario-only V_total −৳49.50, never summed.
    await waitFor(() => {
      expect(screen.getByText('SF-1001')).toBeInTheDocument()
    })
    expect(screen.getAllByText('৳-50.00').length).toBeGreaterThan(0)
    expect(screen.getByText('৳-49.50')).toBeInTheDocument()
    expect(screen.getAllByText('open · খোলা').length).toBeGreaterThan(0)
    // Result without a tracking id in the run payload resolves it
    // through GET /shipments/{id} — SF-2002, not the shipment id.
    expect(screen.getByText('SF-2002')).toBeInTheDocument()
    expect(screen.queryByText('cccccccc…')).not.toBeInTheDocument()
    expect(screen.getAllByText('matched · মিলেছে').length).toBeGreaterThan(0)
    // Findings list (paginated endpoint) shows both explanations.
    await waitFor(() => {
      expect(
        screen.getByText('Reported collection is ৳50.00 less than expected.'),
      ).toBeInTheDocument()
    })
    expect(screen.getByText('রিপোর্ট করা সংগ্রহ প্রত্যাশার চেয়ে ৳৫০.০০ কম।')).toBeInTheDocument()
    // Expanding the tracked result shows its fee lines and findings.
    fireEvent.click(screen.getByText('SF-1001'))
    await waitFor(() => {
      expect(screen.getByText('Result detail — SF-1001')).toBeInTheDocument()
    })
    expect(screen.getByText('delivery_fee')).toBeInTheDocument()
    // The same finding appears in the run findings list and in the
    // expanded result detail — once in each place.
    expect(screen.getAllByText('COLLECTION_SHORTFALL')).toHaveLength(2)
    expect(screen.getByText('C — collection variance')).toBeInTheDocument()
    expect(screen.getByText('V_total — scenario total variance')).toBeInTheDocument()
  })
})

describe('ImportDetailPage — import to run relationship (M6)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  function renderImportDetail(fetchImpl: (url: string, init?: RequestInit) => Promise<unknown>) {
    installFetch(fetchImpl)
    return renderWithClient(
      <MemoryRouter initialEntries={[`/imports/${BATCH_ID}`]}>
        <Routes>
          <Route path="/imports/:batchId" element={<ImportDetailPage />} />
          <Route path="/runs/:runId" element={<div>Run detail page</div>} />
        </Routes>
      </MemoryRouter>,
    )
  }

  it('shows the run link when a run exists for the import', async () => {
    renderImportDetail((url, init) => {
      if (url === `/api/workspaces/${WS_ID}/imports/${BATCH_ID}`) {
        return Promise.resolve(MOCK_IMPORT_DETAIL)
      }
      if (url === `/api/workspaces/${WS_ID}/imports/${BATCH_ID}/runs`) {
        return Promise.resolve({ runs: [MOCK_RUNS.runs[0]], total: 1 })
      }
      return baseHandler(url, init)
    })
    await waitFor(() => {
      expect(screen.getByText('Reconciliation runs')).toBeInTheDocument()
    })
    await waitFor(() => {
      expect(screen.getByText('aaaaaaaa…')).toBeInTheDocument()
    })
    expect(screen.queryByText('No run yet for this import.')).not.toBeInTheDocument()
    expect(screen.queryByText('Create run')).not.toBeInTheDocument()
  })

  it('shows "No run yet" plus the create control when none exists, and creates a run', async () => {
    const posted: Array<{ url: string; body: Record<string, unknown> }> = []
    renderImportDetail((url, init) => {
      if (url === `/api/workspaces/${WS_ID}/imports/${BATCH_ID}`) {
        return Promise.resolve(MOCK_IMPORT_DETAIL)
      }
      if (url === `/api/workspaces/${WS_ID}/imports/${BATCH_ID}/runs`) {
        if (init?.method === 'POST') {
          posted.push({ url, body: JSON.parse(init.body as string) })
          return Promise.resolve({
            run_id: RUN_ID,
            import_batch_id: BATCH_ID,
            status: 'completed',
            tolerance_poisha: 0,
            result_counts: MOCK_RUNS.runs[0].result_counts,
            created_at: '2026-10-31T14:00:00Z',
            idempotency_outcome: 'created',
          })
        }
        return Promise.resolve({ runs: [], total: 0 })
      }
      return baseHandler(url, init)
    })
    await waitFor(() => {
      expect(screen.getByText('No run yet for this import.')).toBeInTheDocument()
    })
    // Fixture selector defaults to Fixture Alpha; tolerance defaults to 0.
    const fixture = screen.getByLabelText('Rule fixture') as HTMLSelectElement
    expect(fixture.value).toBe('Fixture Alpha')
    expect(Array.from(fixture.options).map((o) => o.value)).toEqual([
      'Fixture Alpha',
      'Fixture Beta',
      'Fixture Gamma',
    ])
    const tolerance = screen.getByLabelText('Tolerance (poisha)') as HTMLInputElement
    expect(tolerance.value).toBe('0')
    fireEvent.click(screen.getByText('Create run'))
    await waitFor(() => {
      expect(screen.getByText('Run detail page')).toBeInTheDocument()
    })
    // The POST went to the real endpoint with the fixture, tolerance and
    // an idempotency key — nothing invented.
    expect(posted).toHaveLength(1)
    expect(posted[0].body.courier_fixture).toBe('Fixture Alpha')
    expect(posted[0].body.tolerance_poisha).toBe(0)
    expect(typeof posted[0].body.idempotency_key).toBe('string')
  })

  it('shows the backend error when run creation fails', async () => {
    globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/auth/csrf') {
        return { ok: true, status: 200, json: async () => ({ csrf_token: 'test-csrf' }) }
      }
      if (url === `/api/workspaces/${WS_ID}/imports/${BATCH_ID}`) {
        return { ok: true, status: 200, json: async () => MOCK_IMPORT_DETAIL }
      }
      if (url === `/api/workspaces/${WS_ID}/imports/${BATCH_ID}/runs`) {
        if (init?.method === 'POST') {
          return {
            ok: false,
            status: 422,
            json: async () => ({ detail: 'No eligible records in this import batch.' }),
          }
        }
        return { ok: true, status: 200, json: async () => ({ runs: [], total: 0 }) }
      }
      const payload = await baseHandler(url, init)
      return { ok: true, status: 200, json: async () => payload }
    }) as unknown as typeof fetch
    renderWithClient(
      <MemoryRouter initialEntries={[`/imports/${BATCH_ID}`]}>
        <Routes>
          <Route path="/imports/:batchId" element={<ImportDetailPage />} />
          <Route path="/runs/:runId" element={<div>Run detail page</div>} />
        </Routes>
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('No run yet for this import.')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('Create run'))
    await waitFor(() => {
      expect(
        screen.getByText('No eligible records in this import batch.'),
      ).toBeInTheDocument()
    })
    expect(screen.queryByText('Run detail page')).not.toBeInTheDocument()
  })
})
