// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SettingsPage } from './pages/Settings'

const WS_ID = '11111111-1111-1111-1111-111111111111'

function mockWorkspaces(role: string) {
  return [
    {
      id: WS_ID,
      name: 'Test Shop — Dhaka',
      trading_name: null,
      status: 'active',
      role,
      created_at: '2026-10-31T00:00:00Z',
    },
  ]
}

const MOCK_SETTINGS = {
  workspace_id: WS_ID,
  tolerance_poisha: 500,
  locale: 'en_bn',
  is_default: false,
  updated_by_user_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  updated_at: '2026-11-01T00:00:00Z',
}

const MOCK_DEFAULT_SETTINGS = {
  workspace_id: WS_ID,
  tolerance_poisha: 0,
  locale: 'en_bn',
  is_default: true,
  updated_by_user_id: null,
  updated_at: null,
}

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function renderWithClient(ui: React.ReactElement) {
  return render(<QueryClientProvider client={makeClient()}>{ui}</QueryClientProvider>)
}

interface CapturedRequest {
  url: string
  method: string
  body: unknown
}

type FetchResult = { ok: boolean; status: number; payload: unknown }

/**
 * Install a fetch mock. `handler` decides the response per request;
 * every request is recorded in `captured` for assertions.
 */
function installFetch(handler: (url: string, init?: RequestInit) => FetchResult) {
  const captured: CapturedRequest[] = []
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
    let body: unknown = undefined
    if (typeof init?.body === 'string' && init.body.length > 0) {
      try {
        body = JSON.parse(init.body) as unknown
      } catch {
        body = init.body
      }
    }
    captured.push({ url, method: init?.method ?? 'GET', body })
    const result = handler(url, init)
    return {
      ok: result.ok,
      status: result.status,
      json: async () => result.payload,
    }
  }) as unknown as typeof fetch
  return captured
}

function baseHandler(settingsPayload: unknown, role = 'owner') {
  return (url: string, init?: RequestInit): FetchResult => {
    if (url === '/api/workspaces') {
      return { ok: true, status: 200, payload: mockWorkspaces(role) }
    }
    if (url === '/api/auth/csrf') {
      return { ok: true, status: 200, payload: { csrf_token: 'test-csrf' } }
    }
    if (url === `/api/workspaces/${WS_ID}/settings` && (init?.method ?? 'GET') === 'GET') {
      return { ok: true, status: 200, payload: settingsPayload }
    }
    if (url === `/api/workspaces/${WS_ID}/settings` && init?.method === 'PATCH') {
      // Echo the saved values back as the stored settings.
      const body = JSON.parse(init.body as string) as Record<string, unknown>
      return {
        ok: true,
        status: 200,
        payload: {
          ...MOCK_SETTINGS,
          tolerance_poisha: body.tolerance_poisha ?? MOCK_SETTINGS.tolerance_poisha,
          locale: body.locale ?? MOCK_SETTINGS.locale,
          is_default: false,
        },
      }
    }
    throw new Error(`unexpected fetch URL in test: ${url} ${init?.method ?? 'GET'}`)
  }
}

describe('Settings page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('shows the fetched tolerance (not a documented default) with its Bangla explanation', async () => {
    installFetch(baseHandler(MOCK_SETTINGS))
    renderWithClient(<SettingsPage />)

    await waitFor(() =>
      expect(screen.getByText(/Current tolerance:/)).toBeInTheDocument(),
    )
    expect(screen.getByText(/Current tolerance:/).textContent).toContain('500 poisha')
    expect(screen.getByText(/Current tolerance:/).textContent).toContain('৳5.00')
    // Bangla explanation of what tolerance means.
    expect(screen.getByText(/সহনশীলতা/)).toBeInTheDocument()
    // The old page labelled the value as the documented default; the
    // new page must not present a fetched value that way.
    expect(screen.queryByText(/documented default/)).not.toBeInTheDocument()
    // The form is filled with the fetched value.
    expect((screen.getByLabelText('Tolerance (poisha)') as HTMLInputElement).value).toBe(
      '500',
    )
  })

  it('shows the default note when no settings row exists yet', async () => {
    installFetch(baseHandler(MOCK_DEFAULT_SETTINGS))
    renderWithClient(<SettingsPage />)

    await waitFor(() =>
      expect(screen.getByText(/Current tolerance:/)).toBeInTheDocument(),
    )
    expect(screen.getByText(/Current tolerance:/).textContent).toContain('0 poisha')
    expect(screen.getByText(/Current tolerance:/).textContent).toContain('৳0.00')
    expect(screen.getByText(/no settings saved yet, this is the default/)).toBeInTheDocument()
  })

  it('owner can save a new tolerance; the PATCH body matches the real endpoint shape', async () => {
    const captured = installFetch(baseHandler(MOCK_SETTINGS))
    renderWithClient(<SettingsPage />)

    await waitFor(() =>
      expect(screen.getByText(/Current tolerance:/)).toBeInTheDocument(),
    )
    const input = screen.getByLabelText('Tolerance (poisha)') as HTMLInputElement
    fireEvent.change(input, { target: { value: '750' } })
    fireEvent.click(screen.getByRole('button', { name: /Save settings/ }))

    await waitFor(() =>
      expect(screen.getByText(/Settings saved/)).toBeInTheDocument(),
    )
    const patch = captured.find(
      (r) => r.url === `/api/workspaces/${WS_ID}/settings` && r.method === 'PATCH',
    )
    expect(patch).toBeDefined()
    expect(patch!.body).toEqual({ tolerance_poisha: 750, locale: 'en_bn' })
    expect(screen.getByText(/Settings saved/).textContent).toContain('750 poisha')
    expect(screen.getByText(/Settings saved/).textContent).toContain('৳7.50')
  })

  it('rejects a negative tolerance in the form without calling the backend', async () => {
    const captured = installFetch(baseHandler(MOCK_SETTINGS))
    renderWithClient(<SettingsPage />)

    await waitFor(() =>
      expect(screen.getByText(/Current tolerance:/)).toBeInTheDocument(),
    )
    const input = screen.getByLabelText('Tolerance (poisha)') as HTMLInputElement
    fireEvent.change(input, { target: { value: '-5' } })
    fireEvent.click(screen.getByRole('button', { name: /Save settings/ }))

    await waitFor(() =>
      expect(screen.getByText('Tolerance cannot be negative')).toBeInTheDocument(),
    )
    expect(
      captured.some(
        (r) => r.url === `/api/workspaces/${WS_ID}/settings` && r.method === 'PATCH',
      ),
    ).toBe(false)
  })

  it('member sees a read-only view: disabled inputs, no save button', async () => {
    installFetch(baseHandler(MOCK_SETTINGS, 'member'))
    renderWithClient(<SettingsPage />)

    await waitFor(() =>
      expect(screen.getByText(/Current tolerance:/)).toBeInTheDocument(),
    )
    expect(screen.getByText(/Only owners and admins can change settings/)).toBeInTheDocument()
    expect(screen.getByText(/Your role in this workspace is/)).toBeInTheDocument()
    expect(screen.getByLabelText('Tolerance (poisha)')).toBeDisabled()
    expect(screen.getByLabelText('Language')).toBeDisabled()
    expect(screen.queryByRole('button', { name: /Save settings/ })).not.toBeInTheDocument()
  })

  it('shows an error when settings fail to load', async () => {
    installFetch((url: string, init?: RequestInit): FetchResult => {
      if (url === '/api/workspaces') {
        return { ok: true, status: 200, payload: mockWorkspaces('owner') }
      }
      if (url === `/api/workspaces/${WS_ID}/settings`) {
        return { ok: false, status: 500, payload: { detail: 'Internal server error.' } }
      }
      throw new Error(`unexpected fetch URL in test: ${url} ${init?.method ?? 'GET'}`)
    })
    renderWithClient(<SettingsPage />)

    await waitFor(() =>
      expect(screen.getByText(/Could not load settings from the backend/)).toBeInTheDocument(),
    )
  })
})
