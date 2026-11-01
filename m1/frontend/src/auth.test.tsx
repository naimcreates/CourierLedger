// @vitest-environment jsdom
import { describe, expect, it, vi, afterEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import { render, screen, waitFor } from '@testing-library/react'
import App from './App'

describe('auth gate', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('redirects an unauthenticated user from the dashboard to the login page', async () => {
    globalThis.fetch = vi.fn(async (url: string) => {
      if (url === '/api/auth/me') {
        return {
          ok: false,
          status: 401,
          json: async () => ({ detail: 'Not authenticated.' }),
        }
      }
      throw new Error(`unexpected fetch URL in test: ${url}`)
    }) as unknown as typeof fetch
    window.history.pushState({}, '', '/')
    render(<App />)
    // While the session check runs, the guard shows its loading state.
    expect(screen.getByText('Checking your session…')).toBeInTheDocument()
    // After the 401, the login page renders instead of the dashboard.
    await waitFor(() => {
      expect(screen.getByText('Sign in to CourierLedger')).toBeInTheDocument()
    })
    expect(screen.queryByText('COD reconciliation overview.')).not.toBeInTheDocument()
  })
})
