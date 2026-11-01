// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SettlementBatchesPage, SettlementBatchDetailPage } from './pages/SettlementBatches'
import { BankReceiptsPage } from './pages/BankReceipts'

const WS_ID = '11111111-1111-1111-1111-111111111111'
const BATCH_ID = '22222222-2222-2222-2222-222222222222'
const BATCH2_ID = '33333333-3333-3333-3333-333333333333'
const ACCOUNT_ID = '44444444-4444-4444-4444-444444444444'
const RUN_ID = '55555555-5555-5555-5555-555555555555'
const RECEIPT_ID = '66666666-6666-6666-6666-666666666666'

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

const MOCK_ACCOUNTS = [
  {
    id: ACCOUNT_ID,
    courier_code: 'steadfast',
    merchant_account_ref: 'SF-TEST',
    display_label: 'Steadfast — Test',
    status: 'active',
  },
]

const MOCK_RUNS = {
  runs: [
    {
      run_id: RUN_ID,
      merchant_id: WS_ID,
      import_batch_id: '77777777-7777-7777-7777-777777777777',
      status: 'completed',
      tolerance_poisha: 0,
      result_counts: {
        matched: 1,
        open: 0,
        mismatch: 0,
        needs_review: 0,
        matched_with_tolerance: 0,
      },
      created_at: '2026-10-31T14:00:00Z',
      completed_at: '2026-10-31T14:00:05Z',
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
}

const MOCK_BATCHES = [
  {
    batch_id: BATCH_ID,
    merchant_courier_account_id: ACCOUNT_ID,
    run_id: RUN_ID,
    period_start: '2026-10-01',
    period_end: '2026-10-15',
    expected_batch_payout_poisha: 100000,
    allocated_total_poisha: 60000,
    allocation_status: 'partially_allocated',
    bank_status: 'partial_receipt',
    current_bank_variance_poisha: -5000,
    created_at: '2026-10-31T10:00:00Z',
  },
  {
    batch_id: BATCH2_ID,
    merchant_courier_account_id: ACCOUNT_ID,
    run_id: null,
    period_start: '2026-10-16',
    period_end: '2026-10-31',
    expected_batch_payout_poisha: null,
    allocated_total_poisha: null,
    allocation_status: 'unallocated',
    bank_status: 'no_receipt',
    current_bank_variance_poisha: null,
    created_at: '2026-10-31T11:00:00Z',
  },
]

/**
 * GET /settlement-batches/{id} returns the FLAT batch dict (batch
 * fields at top level, receipts / allocations / net total / variance
 * history beside them) — no {batch, ...} wrapper, and the net field is
 * net_allocated_total_poisha (M9-BUG-2).
 */
const MOCK_BATCH_DETAIL = {
  ...MOCK_BATCHES[0],
  receipts: [
    {
      receipt_id: RECEIPT_ID,
      amount_poisha: 60000,
      received_on: '2026-10-20',
      receipt_ref_label: 'Bank transfer Oct',
      is_complete: false,
      created_at: '2026-10-31T10:30:00Z',
    },
  ],
  allocations: [
    {
      allocation_id: '88888888-8888-8888-8888-888888888888',
      bank_receipt_id: RECEIPT_ID,
      allocated_poisha: 60000,
      kind: 'allocation',
      reverses_allocation_id: null,
      created_at: '2026-10-31T10:35:00Z',
    },
  ],
  net_allocated_total_poisha: 60000,
  variance_calculations: [],
  current_bank_variance_poisha: null,
  variance_status: 'not_fully_allocated',
  current_calculation_seq: null,
}

const MOCK_BANK_RECEIPTS = [
  {
    receipt_id: RECEIPT_ID,
    settlement_batch_id: BATCH_ID,
    amount_poisha: 60000,
    received_on: '2026-10-20',
    receipt_ref_label: 'Bank transfer Oct',
    is_complete: false,
    created_at: '2026-10-31T10:30:00Z',
  },
  {
    receipt_id: '99999999-9999-9999-9999-999999999999',
    settlement_batch_id: BATCH_ID,
    amount_poisha: 40000,
    received_on: '2026-10-22',
    receipt_ref_label: 'Bank transfer Oct 2',
    is_complete: true,
    created_at: '2026-10-31T10:40:00Z',
  },
]

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function renderWithClient(ui: React.ReactElement) {
  return render(<QueryClientProvider client={makeClient()}>{ui}</QueryClientProvider>)
}

function installFetch(handler: (url: string, init?: RequestInit) => Promise<unknown>) {
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const payload = await handler(url, init)
    return { ok: true, status: 200, json: async () => payload }
  }) as unknown as typeof fetch
}

function baseHandler(url: string, init?: RequestInit): Promise<unknown> {
  if (url === '/api/workspaces') return Promise.resolve(MOCK_WORKSPACES)
  if (url === '/api/auth/csrf') return Promise.resolve({ csrf_token: 'test-csrf' })
  if (url === `/api/workspaces/${WS_ID}/courier-accounts`) {
    return Promise.resolve(MOCK_ACCOUNTS)
  }
  if (url === `/api/workspaces/${WS_ID}/runs?limit=20&offset=0`) {
    return Promise.resolve(MOCK_RUNS)
  }
  if (url === `/api/workspaces/${WS_ID}/settlement-batches`) {
    // GET /settlement-batches returns the paginated envelope, not a
    // bare array (M9-BUG-3).
    return Promise.resolve({
      settlement_batches: MOCK_BATCHES,
      total: 2,
      limit: 50,
      offset: 0,
    })
  }
  throw new Error(`unexpected fetch URL in test: ${url} ${init?.method ?? 'GET'}`)
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('SettlementBatchesPage — batch list (M7)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('renders batches from the mocked API data, with Bangla status labels and formatted money', async () => {
    installFetch((url, init) => baseHandler(url, init))
    renderWithClient(
      <MemoryRouter>
        <SettlementBatchesPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Batches (2)')).toBeInTheDocument()
    })
    // Batch 1: partially allocated + partial receipt, expected ৳1,000.00,
    // allocated ৳600.00, non-null variance −৳50.00.
    expect(screen.getByText('partially_allocated · আংশিক বরাদ্দ')).toBeInTheDocument()
    expect(screen.getByText('partial_receipt · আংশিক রসিদ')).toBeInTheDocument()
    expect(screen.getByText('৳1,000.00')).toBeInTheDocument()
    expect(screen.getByText('৳600.00')).toBeInTheDocument()
    expect(screen.getByText('৳-50.00')).toBeInTheDocument()
    // Batch 2: unallocated + no receipt, unknown expected payout (—),
    // null variance shown as "not calculated yet", never a number.
    expect(screen.getByText('unallocated · বরাদ্দ হয়নি')).toBeInTheDocument()
    expect(screen.getByText('no_receipt · রসিদ নেই')).toBeInTheDocument()
    expect(screen.getAllByText('not calculated yet')).toHaveLength(1)
    // Period is shown for both batches.
    expect(screen.getByText('2026-10-01 → 2026-10-15')).toBeInTheDocument()
    expect(screen.getByText('2026-10-16 → 2026-10-31')).toBeInTheDocument()
    // The "never automatically a loss" note is on the page.
    expect(screen.getByText(/never automatically labelled/)).toBeInTheDocument()
  })

  it('says plainly when there are no batches yet', async () => {
    installFetch((url, init) => {
      if (url === `/api/workspaces/${WS_ID}/settlement-batches`) {
        return Promise.resolve({ settlement_batches: [], total: 0, limit: 50, offset: 0 })
      }
      return baseHandler(url, init)
    })
    renderWithClient(
      <MemoryRouter>
        <SettlementBatchesPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('No settlement batches yet.')).toBeInTheDocument()
    })
  })
})

describe('SettlementBatchesPage — create-batch form validation (M7)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  function renderList(fetchImpl: (url: string, init?: RequestInit) => Promise<unknown>) {
    installFetch(fetchImpl)
    return renderWithClient(
      <MemoryRouter initialEntries={['/settlement-batches']}>
        <Routes>
          <Route path="/settlement-batches" element={<SettlementBatchesPage />} />
          <Route path="/settlement-batches/:batchId" element={<div>Batch detail page</div>} />
        </Routes>
      </MemoryRouter>,
    )
  }

  it('rejects a negative expected payout and sends nothing to the backend', async () => {
    const posted: Array<{ url: string; body: Record<string, unknown> }> = []
    renderList((url, init) => {
      if (url === `/api/workspaces/${WS_ID}/settlement-batches` && init?.method === 'POST') {
        posted.push({ url, body: JSON.parse(init.body as string) })
        return Promise.resolve(MOCK_BATCHES[0])
      }
      return baseHandler(url, init)
    })
    await waitFor(() => {
      expect(screen.getByText('Batches (2)')).toBeInTheDocument()
    })
    const accountSelect = screen.getByLabelText('Courier account') as HTMLSelectElement
    fireEvent.change(accountSelect, { target: { value: ACCOUNT_ID } })
    const payout = screen.getByLabelText('Expected payout ৳ (optional)') as HTMLInputElement
    fireEvent.change(payout, { target: { value: '-100' } })
    fireEvent.click(screen.getByText('Create batch'))
    await waitFor(() => {
      expect(screen.getByText('Amount must be 0 or more.')).toBeInTheDocument()
    })
    expect(posted).toHaveLength(0)
    expect(screen.queryByText('Batch detail page')).not.toBeInTheDocument()
  })

  it('creates a batch with a valid optional payout, converting taka to poisha', async () => {
    const posted: Array<{ url: string; body: Record<string, unknown> }> = []
    renderList((url, init) => {
      if (url === `/api/workspaces/${WS_ID}/settlement-batches` && init?.method === 'POST') {
        posted.push({ url, body: JSON.parse(init.body as string) })
        return Promise.resolve(MOCK_BATCHES[0])
      }
      return baseHandler(url, init)
    })
    await waitFor(() => {
      expect(screen.getByText('Batches (2)')).toBeInTheDocument()
    })
    fireEvent.change(screen.getByLabelText('Courier account'), {
      target: { value: ACCOUNT_ID },
    })
    fireEvent.change(screen.getByLabelText('Reconciliation run (optional)'), {
      target: { value: RUN_ID },
    })
    fireEvent.change(screen.getByLabelText('Period start (optional)'), {
      target: { value: '2026-10-01' },
    })
    fireEvent.change(screen.getByLabelText('Period end (optional)'), {
      target: { value: '2026-10-15' },
    })
    fireEvent.change(screen.getByLabelText('Expected payout ৳ (optional)'), {
      target: { value: '1250.50' },
    })
    fireEvent.click(screen.getByText('Create batch'))
    await waitFor(() => {
      expect(screen.getByText('Batch detail page')).toBeInTheDocument()
    })
    expect(posted).toHaveLength(1)
    expect(posted[0].body.merchant_courier_account_id).toBe(ACCOUNT_ID)
    expect(posted[0].body.run_id).toBe(RUN_ID)
    expect(posted[0].body.period_start).toBe('2026-10-01')
    expect(posted[0].body.period_end).toBe('2026-10-15')
    // ৳1,250.50 -> 125050 poisha.
    expect(posted[0].body.expected_batch_payout_poisha).toBe(125050)
  })

  it('requires a courier account', async () => {
    renderList((url, init) => baseHandler(url, init))
    await waitFor(() => {
      expect(screen.getByText('Batches (2)')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('Create batch'))
    await waitFor(() => {
      expect(screen.getByText('Choose a courier account for this batch.')).toBeInTheDocument()
    })
  })
})

describe('SettlementBatchDetailPage — variance-null reason (M7)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('shows the backend variance_status reason and no number when the variance is null', async () => {
    installFetch((url, init) => {
      if (url === `/api/workspaces/${WS_ID}/settlement-batches/${BATCH_ID}`) {
        return Promise.resolve(MOCK_BATCH_DETAIL)
      }
      return baseHandler(url, init)
    })
    renderWithClient(
      <MemoryRouter initialEntries={[`/settlement-batches/${BATCH_ID}`]}>
        <Routes>
          <Route path="/settlement-batches/:batchId" element={<SettlementBatchDetailPage />} />
        </Routes>
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByTestId('current-variance')).toBeInTheDocument()
    })
    // The null variance is explained, in English and Bangla — and the
    // current-variance card contains no ৳ figure at all.
    const varianceCard = screen.getByTestId('current-variance')
    expect(varianceCard.textContent).toContain('Cannot calculate yet')
    expect(varianceCard.textContent).toContain('হিসাব করা যায়নি — এখনো সম্পূর্ণ বরাদ্দ হয়নি।')
    expect(varianceCard.textContent).not.toContain('৳')
    // The rest of the detail still shows its real figures.
    expect(screen.getByText('৳1,000.00')).toBeInTheDocument()
    expect(screen.getByText('Bank transfer Oct')).toBeInTheDocument()
    expect(screen.getByText('incomplete · অসম্পূর্ণ')).toBeInTheDocument()
    expect(screen.getByText('No variance calculated yet for this batch.')).toBeInTheDocument()
  })

  it('shows the calculated variance when the backend returns one', async () => {
    installFetch((url, init) => {
      if (url === `/api/workspaces/${WS_ID}/settlement-batches/${BATCH_ID}`) {
        return Promise.resolve({
          ...MOCK_BATCH_DETAIL,
          current_bank_variance_poisha: -5000,
          variance_status: 'ok',
          variance_calculations: [
            {
              calculation_seq: 1,
              expected_batch_payout_poisha: 100000,
              allocated_bank_confirmed_total_poisha: 105000,
              bank_variance_poisha: -5000,
              computed_at: '2026-10-31T11:00:00Z',
            },
          ],
        })
      }
      return baseHandler(url, init)
    })
    renderWithClient(
      <MemoryRouter initialEntries={[`/settlement-batches/${BATCH_ID}`]}>
        <Routes>
          <Route path="/settlement-batches/:batchId" element={<SettlementBatchDetailPage />} />
        </Routes>
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByTestId('current-variance').textContent).toContain('৳-50.00')
    })
    expect(screen.getByText(/Variance calculated\./)).toBeInTheDocument()
    expect(screen.getByText('Variance calculation history (1)')).toBeInTheDocument()
  })
})

describe('BankReceiptsPage — receipt list (M7)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('renders receipts from the mocked API data with batch links and Bangla status', async () => {
    installFetch((url, init) => {
      if (url === `/api/workspaces/${WS_ID}/bank-receipts`) {
        // GET /bank-receipts returns the paginated envelope, not a bare
        // array (same drift class as M9-BUG-3).
        return Promise.resolve({
          receipts: MOCK_BANK_RECEIPTS,
          total: 2,
          limit: 50,
          offset: 0,
        })
      }
      return baseHandler(url, init)
    })
    renderWithClient(
      <MemoryRouter>
        <BankReceiptsPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Receipts (2)')).toBeInTheDocument()
    })
    expect(screen.getByText('৳600.00')).toBeInTheDocument()
    expect(screen.getByText('৳400.00')).toBeInTheDocument()
    expect(screen.getByText('Bank transfer Oct')).toBeInTheDocument()
    expect(screen.getByText('Bank transfer Oct 2')).toBeInTheDocument()
    expect(screen.getByText('incomplete · অসম্পূর্ণ')).toBeInTheDocument()
    expect(screen.getByText('complete · সম্পূর্ণ')).toBeInTheDocument()
    // Both receipts link to their settlement batch.
    expect(screen.getAllByText('22222222…')).toHaveLength(2)
    // The separation note is on the page, and no bank-account-number
    // field exists anywhere on the page.
    expect(screen.getByText(/kept separate from courier-reported payouts/)).toBeInTheDocument()
    expect(screen.queryByLabelText(/account number/i)).not.toBeInTheDocument()
  })
})
