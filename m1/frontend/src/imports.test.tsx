// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ImportsPage } from './pages/Imports'

const WS_ID = '11111111-1111-1111-1111-111111111111'

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
    id: '22222222-2222-2222-2222-222222222222',
    courier_code: 'steadfast',
    merchant_account_ref: 'SF-TEST-1',
    display_label: 'Steadfast — main account',
    status: 'active',
  },
]

/** Shape matches GET /api/workspaces/{id}/imports (M4). */
const MOCK_IMPORTS = [
  {
    batch_id: '33333333-3333-3333-3333-333333333333',
    status: 'parsed',
    row_count: 12,
    parsed_row_count: 10,
    error_row_count: 6,
    file_object_id: '44444444-4444-4444-4444-444444444444',
    mapping_snapshot: { tracking_id: 'Tracking ID' },
    created_at: '2026-10-31T12:00:00Z',
  },
  {
    batch_id: '55555555-5555-5555-5555-555555555555',
    status: 'parsed',
    row_count: 3,
    parsed_row_count: 3,
    error_row_count: 0,
    file_object_id: '66666666-6666-6666-6666-666666666666',
    mapping_snapshot: { tracking_id: 'Tracking ID' },
    created_at: '2026-10-31T13:00:00Z',
  },
]

function mockApiFetch() {
  return vi.fn(async (url: string) => {
    let payload: unknown = null
    if (url === '/api/workspaces') payload = MOCK_WORKSPACES
    else if (url === `/api/workspaces/${WS_ID}/imports`) payload = MOCK_IMPORTS
    else if (url === `/api/workspaces/${WS_ID}/courier-accounts`) payload = MOCK_ACCOUNTS
    else throw new Error(`unexpected fetch URL in test: ${url}`)
    return {
      ok: true,
      status: 200,
      json: async () => payload,
    }
  })
}

function renderImportsPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ImportsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ImportsPage', () => {
  beforeEach(() => {
    globalThis.fetch = mockApiFetch() as unknown as typeof fetch
    window.localStorage.clear()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders real import batches from the API shape', async () => {
    renderImportsPage()
    // Workspace picker shows the real workspace name.
    await waitFor(() => {
      expect(screen.getByText('Test Shop — Dhaka')).toBeInTheDocument()
    })
    // Both batches render (short batch id + counts).
    await waitFor(() => {
      expect(screen.getByText('33333333…')).toBeInTheDocument()
    })
    expect(screen.getByText('55555555…')).toBeInTheDocument()
    // Row counts: first batch 12 rows / 10 parsed / 6 error rows.
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText('10')).toBeInTheDocument()
    // The courier account appears in the upload form's select.
    expect(screen.getByText('steadfast — Steadfast — main account (SF-TEST-1)')).toBeInTheDocument()
    // No "no account" warning, and the note points at the detail page for runs.
    expect(screen.queryByText(/No courier account exists/)).not.toBeInTheDocument()
    expect(
      screen.getByText(/To reconcile an import, open its detail page/),
    ).toBeInTheDocument()
  })

  it('says so when a workspace has no courier account', async () => {
    globalThis.fetch = vi.fn(async (url: string) => {
      let payload: unknown = null
      if (url === '/api/workspaces') payload = MOCK_WORKSPACES
      else if (url === `/api/workspaces/${WS_ID}/imports`) payload = []
      else if (url === `/api/workspaces/${WS_ID}/courier-accounts`) payload = []
      else throw new Error(`unexpected fetch URL in test: ${url}`)
      return { ok: true, status: 200, json: async () => payload }
    }) as unknown as typeof fetch
    renderImportsPage()
    await waitFor(() => {
      expect(screen.getByText(/No courier account exists for Test Shop — Dhaka yet/)).toBeInTheDocument()
    })
    expect(screen.getByText('No imports yet for this workspace — upload a statement CSV above.')).toBeInTheDocument()
  })
})
