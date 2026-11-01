// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { TeamPage } from './pages/Team'

const WS_ID = '11111111-1111-1111-1111-111111111111'
const OWNER_ID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
const MEMBER_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
const INVITED_ID = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
const REVOKED_ID = 'dddddddd-dddd-dddd-dddd-dddddddddddd'

const OWNER_MEMBERSHIP = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
const MEMBER_MEMBERSHIP = 'ffffffff-ffff-ffff-ffff-ffffffffffff'
const INVITED_MEMBERSHIP = '11111111-2222-3333-4444-555555555555'
const REVOKED_MEMBERSHIP = '66666666-7777-8888-9999-000000000000'

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

const MOCK_MEMBERS = [
  {
    id: OWNER_MEMBERSHIP,
    user_id: OWNER_ID,
    email: 'owner@example.com',
    display_name: 'Shop Owner',
    role: 'owner',
    status: 'active',
    invited_by: null,
    created_at: '2026-10-31T00:00:00Z',
    updated_at: null,
  },
  {
    id: MEMBER_MEMBERSHIP,
    user_id: MEMBER_ID,
    email: 'staff@example.com',
    display_name: 'Staff Member',
    role: 'member',
    status: 'active',
    invited_by: 'owner@example.com',
    created_at: '2026-10-31T01:00:00Z',
    updated_at: null,
  },
  {
    id: INVITED_MEMBERSHIP,
    user_id: INVITED_ID,
    email: 'teammate@example.com',
    display_name: null,
    role: 'viewer',
    status: 'invited',
    invited_by: 'owner@example.com',
    created_at: '2026-10-31T02:00:00Z',
    updated_at: null,
  },
  {
    id: REVOKED_MEMBERSHIP,
    user_id: REVOKED_ID,
    email: 'gone@example.com',
    display_name: null,
    role: 'member',
    status: 'revoked',
    invited_by: 'owner@example.com',
    created_at: '2026-10-31T03:00:00Z',
    updated_at: '2026-10-31T04:00:00Z',
  },
]

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
function installFetch(
  handler: (url: string, init?: RequestInit) => Promise<FetchResult>,
  captured: CapturedRequest[],
) {
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    let body: unknown = undefined
    if (init?.body) {
      try {
        body = JSON.parse(init.body as string)
      } catch {
        body = init.body
      }
    }
    captured.push({ url, method, body })
    const result = await handler(url, init)
    return { ok: result.ok, status: result.status, json: async () => result.payload }
  }) as unknown as typeof fetch
}

function ok(payload: unknown): FetchResult {
  return { ok: true, status: 200, payload }
}

/** Shared GET routes: workspaces, current user, members list. */
function baseHandler(
  myRole: string,
  myUserId: string,
  members: unknown,
): (url: string, init?: RequestInit) => Promise<FetchResult> {
  return async (url: string, init?: RequestInit) => {
    if (url === '/api/workspaces') return ok(mockWorkspaces(myRole))
    if (url === '/api/auth/me') {
      return ok({
        id: myUserId,
        email: 'owner@example.com',
        display_name: 'Shop Owner',
        status: 'active',
      })
    }
    if (url === `/api/workspaces/${WS_ID}/members` && (init?.method ?? 'GET') === 'GET') {
      return ok(members)
    }
    throw new Error(`unexpected fetch URL in test: ${url} ${init?.method ?? 'GET'}`)
  }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('TeamPage — member list (M9)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('renders members from the real list payload, with pending invitations and Bangla status labels', async () => {
    const captured: CapturedRequest[] = []
    installFetch(baseHandler('owner', OWNER_ID, MOCK_MEMBERS), captured)
    renderWithClient(
      <MemoryRouter>
        <TeamPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Members (4)')).toBeInTheDocument()
    })
    // Pending invitations are shown separately from active members.
    expect(screen.getByText('Pending invitations (1)')).toBeInTheDocument()
    expect(screen.getByText('teammate@example.com · viewer')).toBeInTheDocument()
    // Statuses with Bangla labels (two active members: owner + staff).
    expect(screen.getAllByText('active · সক্রিয়')).toHaveLength(2)
    expect(screen.getByText('invited · আমন্ত্রিত')).toBeInTheDocument()
    expect(screen.getByText('revoked · বাতিল')).toBeInTheDocument()
    // The owner row says the owner role cannot be changed here.
    expect(screen.getByText('Owner role cannot be changed here.')).toBeInTheDocument()
    // The revoked row says to invite again to restore access.
    expect(screen.getByText('Invite again to restore access.')).toBeInTheDocument()
    // The owner (not the invited teammate) sees no accept button —
    // accepting is done by the invited user themselves.
    expect(screen.queryByText('Accept invitation')).not.toBeInTheDocument()
    // The list came from the real members endpoint, not a bare guess.
    expect(
      captured.some((r) => r.url === `/api/workspaces/${WS_ID}/members` && r.method === 'GET'),
    ).toBe(true)
  })

  it('says plainly when there are no pending invitations', async () => {
    const captured: CapturedRequest[] = []
    installFetch(baseHandler('owner', OWNER_ID, MOCK_MEMBERS.slice(0, 2)), captured)
    renderWithClient(
      <MemoryRouter>
        <TeamPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Members (2)')).toBeInTheDocument()
    })
    expect(screen.getByText('Pending invitations (0)')).toBeInTheDocument()
    expect(screen.getByText('No pending invitations for this workspace.')).toBeInTheDocument()
  })
})

describe('TeamPage — invite, role change and revoke (M9)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  function renderAsOwner(
    fetchImpl: (url: string, init?: RequestInit) => Promise<FetchResult>,
    captured: CapturedRequest[],
  ) {
    installFetch(fetchImpl, captured)
    return renderWithClient(
      <MemoryRouter>
        <TeamPage />
      </MemoryRouter>,
    )
  }

  it('invites a teammate by email and role, posting to the real endpoint', async () => {
    const captured: CapturedRequest[] = []
    renderAsOwner(async (url, init) => {
      if (
        url === `/api/workspaces/${WS_ID}/members/invite` &&
        init?.method === 'POST'
      ) {
        const body = JSON.parse(init.body as string) as { email: string; role: string }
        return {
          ok: true,
          status: 201,
          payload: {
            id: INVITED_MEMBERSHIP,
            user_id: INVITED_ID,
            email: body.email,
            display_name: null,
            role: body.role,
            status: 'invited',
            invited_by: 'owner@example.com',
            created_at: '2026-11-01T00:00:00Z',
            updated_at: null,
          },
        }
      }
      return baseHandler('owner', OWNER_ID, MOCK_MEMBERS)(url, init)
    }, captured)
    await waitFor(() => {
      expect(screen.getByText('Members (4)')).toBeInTheDocument()
    })
    // The owner role is never an invite option.
    const roleSelect = screen.getByLabelText('Role') as HTMLSelectElement
    expect(Array.from(roleSelect.options).map((o) => o.value)).toEqual([
      'admin',
      'member',
      'viewer',
    ])
    fireEvent.change(screen.getByLabelText("Teammate's email"), {
      target: { value: 'newteammate@example.com' },
    })
    fireEvent.change(roleSelect, { target: { value: 'admin' } })
    fireEvent.click(screen.getByText('Invite teammate'))
    await waitFor(() => {
      expect(
        screen.getByText(/Invitation sent to newteammate@example.com as admin/),
      ).toBeInTheDocument()
    })
    const invite = captured.find(
      (r) => r.url === `/api/workspaces/${WS_ID}/members/invite`,
    )
    expect(invite).toBeDefined()
    expect(invite!.method).toBe('POST')
    expect(invite!.body).toEqual({ email: 'newteammate@example.com', role: 'admin' })
  })

  it('shows the backend error when inviting an unknown email', async () => {
    const captured: CapturedRequest[] = []
    renderAsOwner(async (url, init) => {
      if (
        url === `/api/workspaces/${WS_ID}/members/invite` &&
        init?.method === 'POST'
      ) {
        return {
          ok: false,
          status: 422,
          payload: {
            detail:
              'unknown_email: No CourierLedger account exists for that email address.',
          },
        }
      }
      return baseHandler('owner', OWNER_ID, MOCK_MEMBERS)(url, init)
    }, captured)
    await waitFor(() => {
      expect(screen.getByText('Members (4)')).toBeInTheDocument()
    })
    fireEvent.change(screen.getByLabelText("Teammate's email"), {
      target: { value: 'nobody@example.com' },
    })
    fireEvent.click(screen.getByText('Invite teammate'))
    await waitFor(() => {
      expect(
        screen.getByText(/unknown_email: No CourierLedger account exists/),
      ).toBeInTheDocument()
    })
  })

  it('changes a member role and revokes a member through the real endpoints', async () => {
    const captured: CapturedRequest[] = []
    renderAsOwner(async (url, init) => {
      if (
        url === `/api/workspaces/${WS_ID}/members/${MEMBER_MEMBERSHIP}/role` &&
        init?.method === 'PATCH'
      ) {
        return { ok: true, status: 200, payload: { ...MOCK_MEMBERS[1], role: 'viewer' } }
      }
      if (
        url === `/api/workspaces/${WS_ID}/members/${MEMBER_MEMBERSHIP}/revoke` &&
        init?.method === 'POST'
      ) {
        return { ok: true, status: 200, payload: { ...MOCK_MEMBERS[1], status: 'revoked' } }
      }
      return baseHandler('owner', OWNER_ID, MOCK_MEMBERS)(url, init)
    }, captured)
    await waitFor(() => {
      expect(screen.getByText('Members (4)')).toBeInTheDocument()
    })
    const roleSelect = screen.getByLabelText(
      'Role for staff@example.com',
    ) as HTMLSelectElement
    expect(roleSelect.value).toBe('member')
    fireEvent.change(roleSelect, { target: { value: 'viewer' } })
    await waitFor(() => {
      expect(screen.getByText('staff@example.com is now viewer.')).toBeInTheDocument()
    })
    const roleCall = captured.find((r) => r.url.endsWith(`/members/${MEMBER_MEMBERSHIP}/role`))
    expect(roleCall?.method).toBe('PATCH')
    expect(roleCall?.body).toEqual({ role: 'viewer' })

    fireEvent.click(screen.getAllByText('Revoke')[0])
    await waitFor(() => {
      expect(
        screen.getByText(/staff@example.com was revoked. Invite them again/),
      ).toBeInTheDocument()
    })
    const revokeCall = captured.find((r) =>
      r.url.endsWith(`/members/${MEMBER_MEMBERSHIP}/revoke`),
    )
    expect(revokeCall?.method).toBe('POST')
  })

  it('hides member management from non-managers', async () => {
    const captured: CapturedRequest[] = []
    installFetch(baseHandler('member', MEMBER_ID, MOCK_MEMBERS), captured)
    renderWithClient(
      <MemoryRouter>
        <TeamPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Members (4)')).toBeInTheDocument()
    })
    expect(screen.queryByText('Invite teammate')).not.toBeInTheDocument()
    expect(screen.queryByText('Revoke')).not.toBeInTheDocument()
    expect(
      screen.getByText(/Only workspace owners and admins can invite teammates/),
    ).toBeInTheDocument()
  })
})

describe('TeamPage — owner transfer (M10 step 2)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  function renderAsOwner(
    fetchImpl: (url: string, init?: RequestInit) => Promise<FetchResult>,
    captured: CapturedRequest[],
  ) {
    installFetch(fetchImpl, captured)
    return renderWithClient(
      <MemoryRouter>
        <TeamPage />
      </MemoryRouter>,
    )
  }

  it('shows an owner-only transfer control listing only active non-owner members', async () => {
    const captured: CapturedRequest[] = []
    installFetch(baseHandler('owner', OWNER_ID, MOCK_MEMBERS), captured)
    renderWithClient(
      <MemoryRouter>
        <TeamPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Members (4)')).toBeInTheDocument()
    })
    expect(screen.getByText('Transfer ownership · মালিকানা হস্তান্তর')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Transfer ownership' })).toBeInTheDocument()
    const select = screen.getByLabelText('New owner') as HTMLSelectElement
    const options = Array.from(select.options).map((o) => o.value)
    // Only the active staff member is eligible: not the owner
    // (self), not the invited teammate, not the revoked member.
    expect(options).toEqual(['', MEMBER_ID])
    // The explanatory copy says the old owner becomes an admin.
    expect(screen.getByText(/you become an admin/)).toBeInTheDocument()
  })

  it('transfers ownership only after confirmation, posting the target user id', async () => {
    const captured: CapturedRequest[] = []
    renderAsOwner(async (url, init) => {
      if (
        url === `/api/workspaces/${WS_ID}/members/transfer-owner` &&
        init?.method === 'POST'
      ) {
        const body = JSON.parse(init.body as string) as { user_id: string }
        expect(body.user_id).toBe(MEMBER_ID)
        return { ok: true, status: 200, payload: { ...MOCK_MEMBERS[1], role: 'owner' } }
      }
      return baseHandler('owner', OWNER_ID, MOCK_MEMBERS)(url, init)
    }, captured)
    await waitFor(() => {
      expect(screen.getByText('Members (4)')).toBeInTheDocument()
    })
    const select = screen.getByLabelText('New owner') as HTMLSelectElement
    fireEvent.change(select, { target: { value: MEMBER_ID } })
    // First click arms the confirm step; nothing is posted yet.
    fireEvent.click(screen.getByText('Transfer ownership'))
    await waitFor(() => {
      expect(screen.getByText('Confirm transfer to staff@example.com?')).toBeInTheDocument()
    })
    expect(
      captured.some((r) => r.url.endsWith('/members/transfer-owner')),
    ).toBe(false)
    // Second click confirms and posts.
    fireEvent.click(screen.getByText('Confirm transfer to staff@example.com?'))
    await waitFor(() => {
      expect(
        screen.getByText(
          'Ownership transferred to staff@example.com. You are now an admin of this workspace.',
        ),
      ).toBeInTheDocument()
    })
    const transferCall = captured.find((r) => r.url.endsWith('/members/transfer-owner'))
    expect(transferCall).toBeDefined()
    expect(transferCall!.method).toBe('POST')
    expect(transferCall!.body).toEqual({ user_id: MEMBER_ID })
  })

  it('shows the backend error verbatim when the transfer is rejected', async () => {
    const captured: CapturedRequest[] = []
    renderAsOwner(async (url, init) => {
      if (
        url === `/api/workspaces/${WS_ID}/members/transfer-owner` &&
        init?.method === 'POST'
      ) {
        return {
          ok: false,
          status: 422,
          payload: {
            detail:
              'target_revoked: That member was revoked. Invite them again and have them accept before transferring ownership.',
          },
        }
      }
      return baseHandler('owner', OWNER_ID, MOCK_MEMBERS)(url, init)
    }, captured)
    await waitFor(() => {
      expect(screen.getByText('Members (4)')).toBeInTheDocument()
    })
    fireEvent.change(screen.getByLabelText('New owner'), {
      target: { value: MEMBER_ID },
    })
    fireEvent.click(screen.getByText('Transfer ownership'))
    await waitFor(() => {
      expect(screen.getByText('Confirm transfer to staff@example.com?')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('Confirm transfer to staff@example.com?'))
    await waitFor(() => {
      expect(
        screen.getByText(/target_revoked: That member was revoked/),
      ).toBeInTheDocument()
    })
  })

  it('hides the transfer control from admins', async () => {
    const captured: CapturedRequest[] = []
    installFetch(baseHandler('admin', MEMBER_ID, MOCK_MEMBERS), captured)
    renderWithClient(
      <MemoryRouter>
        <TeamPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Members (4)')).toBeInTheDocument()
    })
    // Admins can manage members but cannot move the owner role.
    expect(screen.getByText('Invite teammate')).toBeInTheDocument()
    expect(screen.queryByText('Transfer ownership')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('New owner')).not.toBeInTheDocument()
  })
})

describe('TeamPage — accepting an invitation (M9)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('shows Accept on the invited user\'s own pending invitation and posts the accept', async () => {
    const captured: CapturedRequest[] = []
    // The current user is the invited teammate (role member, not a
    // manager): the invitation is theirs to accept.
    installFetch(async (url, init) => {
      if (
        url === `/api/workspaces/${WS_ID}/members/${INVITED_MEMBERSHIP}/accept` &&
        init?.method === 'POST'
      ) {
        return { ok: true, status: 200, payload: { ...MOCK_MEMBERS[2], status: 'active' } }
      }
      return baseHandler('member', INVITED_ID, MOCK_MEMBERS)(url, init)
    }, captured)
    renderWithClient(
      <MemoryRouter>
        <TeamPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Pending invitations (1)')).toBeInTheDocument()
    })
    expect(screen.getByText('This invitation is yours — only you can accept it.')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Accept invitation'))
    await waitFor(() => {
      expect(
        screen.getByText('You are now an active viewer of this workspace.'),
      ).toBeInTheDocument()
    })
    const acceptCall = captured.find((r) =>
      r.url.endsWith(`/members/${INVITED_MEMBERSHIP}/accept`),
    )
    expect(acceptCall?.method).toBe('POST')
  })
})
