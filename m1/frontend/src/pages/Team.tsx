import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ApiError,
  INVITABLE_ROLES,
  acceptInvitation,
  changeMemberRole,
  getMe,
  inviteMember,
  listMembers,
  revokeMember,
  transferOwnership,
  type WorkspaceMember,
} from '../lib/api'
import { useSelectedWorkspace } from '../lib/workspace'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { Input } from '../components/ui/Input'
import { Label } from '../components/ui/Label'
import { WorkspacePicker } from '../components/WorkspacePicker'

function statusVariant(status: string): 'success' | 'warning' | 'muted' {
  if (status === 'active') return 'success'
  if (status === 'invited') return 'warning'
  return 'muted'
}

function statusLabel(status: string): string {
  if (status === 'active') return 'active · সক্রিয়'
  if (status === 'invited') return 'invited · আমন্ত্রিত'
  if (status === 'revoked') return 'revoked · বাতিল'
  return status
}

/**
 * Team and Workspace page. Shows the real workspaces list and the
 * caller's role in each (from GET /api/workspaces), plus the selected
 * workspace's members from GET /api/workspaces/:id/members.
 *
 * Membership rules (backend/app/api/members.py, M9):
 * - Any active member may list members; only owners and admins can
 *   invite, change roles or revoke.
 * - An invitation goes to an EXISTING CourierLedger user by email —
 *   an unknown email is rejected (422). Invitable roles are
 *   admin / member / viewer; 'owner' is never invitable.
 * - Accepting is done by the invited user themselves, from their own
 *   pending invitation — nobody else can accept for them.
 * - Revoking sets status='revoked'; a revoked member is invited again
 *   to restore access. The owner role cannot be changed here.
 * - Transferring ownership (M10 step 2) is owner-only: the current
 *   owner picks another ACTIVE member, confirms, and that member
 *   becomes the owner while the old owner becomes an admin. Only one
 *   owner remains afterwards, and the last-owner guard still holds
 *   (the new sole owner cannot be revoked). POST
 *   /api/workspaces/:id/members/transfer-owner {user_id}.
 */
export function TeamPage() {
  const { workspaces, selected, selectedId, isLoading, isError } = useSelectedWorkspace()
  const queryClient = useQueryClient()

  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteRole, setInviteRole] = useState<string>('member')
  const [inviteError, setInviteError] = useState<string | null>(null)
  const [inviteMessage, setInviteMessage] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionMessage, setActionMessage] = useState<string | null>(null)
  const [transferUserId, setTransferUserId] = useState('')
  const [transferArmed, setTransferArmed] = useState(false)
  const [transferError, setTransferError] = useState<string | null>(null)
  const [transferMessage, setTransferMessage] = useState<string | null>(null)

  const canManage = selected?.role === 'owner' || selected?.role === 'admin'
  const isOwner = selected?.role === 'owner'

  const me = useQuery({
    queryKey: ['me'],
    queryFn: getMe,
    retry: false,
  })

  const members = useQuery({
    queryKey: ['workspace-members', selectedId],
    queryFn: () => listMembers(selectedId!),
    enabled: !!selectedId,
    retry: false,
  })

  function refresh() {
    queryClient.invalidateQueries({ queryKey: ['workspace-members', selectedId] })
  }

  function mutationError(err: unknown): string {
    return err instanceof ApiError
      ? err.message
      : err instanceof Error
        ? err.message
        : 'That did not work — please try again.'
  }

  const invite = useMutation({
    mutationFn: () => {
      const email = inviteEmail.trim()
      if (!email) throw new Error('Enter the email address of the teammate to invite.')
      return inviteMember(selectedId!, { email, role: inviteRole })
    },
    onSuccess: (member) => {
      setInviteError(null)
      setInviteEmail('')
      setInviteMessage(
        `Invitation sent to ${member.email ?? 'the teammate'} as ${member.role}. ` +
          'They accept it from their own account.',
      )
      refresh()
    },
    onError: (err) => {
      setInviteMessage(null)
      setInviteError(mutationError(err))
    },
  })

  const accept = useMutation({
    mutationFn: (membershipId: string) => acceptInvitation(selectedId!, membershipId),
    onSuccess: (member) => {
      setActionError(null)
      setActionMessage(`You are now an active ${member.role} of this workspace.`)
      refresh()
    },
    onError: (err) => {
      setActionMessage(null)
      setActionError(mutationError(err))
    },
  })

  const changeRole = useMutation({
    mutationFn: ({ membershipId, role }: { membershipId: string; role: string }) =>
      changeMemberRole(selectedId!, membershipId, role),
    onSuccess: (member) => {
      setActionError(null)
      setActionMessage(
        `${member.email ?? 'Member'} is now ${member.role}.`,
      )
      refresh()
    },
    onError: (err) => {
      setActionMessage(null)
      setActionError(mutationError(err))
    },
  })

  const revoke = useMutation({
    mutationFn: (membershipId: string) => revokeMember(selectedId!, membershipId),
    onSuccess: (member) => {
      setActionError(null)
      setActionMessage(
        `${member.email ?? 'Member'} was revoked. Invite them again to restore access.`,
      )
      refresh()
    },
    onError: (err) => {
      setActionMessage(null)
      setActionError(mutationError(err))
    },
  })

  const transfer = useMutation({
    mutationFn: (userId: string) => transferOwnership(selectedId!, userId),
    onSuccess: (member) => {
      setTransferError(null)
      setTransferArmed(false)
      setTransferUserId('')
      setTransferMessage(
        `Ownership transferred to ${member.email ?? 'the new owner'}. ` +
          'You are now an admin of this workspace.',
      )
      refresh()
      // The caller's own role changed (owner -> admin), so the
      // workspaces list (which carries each role) must refresh too.
      queryClient.invalidateQueries({ queryKey: ['workspaces'] })
    },
    onError: (err) => {
      setTransferMessage(null)
      setTransferArmed(false)
      setTransferError(mutationError(err))
    },
  })

  const pendingInvites = (members.data ?? []).filter((m) => m.status === 'invited')
  const myInvite = pendingInvites.find((m) => me.data && m.user_id === me.data.id)
  // Only ACTIVE members who are not already the owner can receive
  // ownership; the owner cannot transfer to themselves.
  const transferCandidates = (members.data ?? []).filter(
    (m) => m.status === 'active' && m.role !== 'owner' && (!me.data || m.user_id !== me.data.id),
  )
  const transferTarget = transferCandidates.find((m) => m.user_id === transferUserId)

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Team and Workspace</h1>
        <p className="mt-1 text-sm text-slate-500">
          A workspace is one merchant business. Each workspace has its own shipments,
          couriers and reconciliation.
        </p>
      </div>

      <WorkspacePicker />

      <Card>
        <CardHeader>
          <CardTitle>Your workspaces</CardTitle>
          <CardDescription>
            Loaded from GET /api/workspaces. The role shown is your role in that workspace.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading && <p className="text-sm text-slate-500">Loading…</p>}
          {isError && (
            <p className="text-sm text-red-600">
              Could not load workspaces from the backend. Is it running on port 8000?
            </p>
          )}
          {!isLoading && !isError && workspaces.length === 0 && (
            <p className="text-sm text-slate-500">
              No workspaces yet — create one on the{' '}
              <a href="/workspaces" className="font-medium text-primary hover:underline">
                Workspaces
              </a>{' '}
              page.
            </p>
          )}
          {!isLoading && !isError && workspaces.length > 0 && (
            <ul className="divide-y">
              {workspaces.map((w) => (
                <li key={w.id} className="flex items-center justify-between py-3">
                  <div>
                    <p className="text-sm font-medium text-slate-900">{w.name}</p>
                    <p className="text-xs text-slate-500">
                      {w.trading_name ? `Trading name: ${w.trading_name} · ` : ''}
                      Status: {w.status ?? '—'}
                      {w.created_at ? ` · Created ${w.created_at.slice(0, 10)}` : ''}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    {selected?.id === w.id && <Badge variant="info">Selected</Badge>}
                    <Badge variant="default">{w.role ?? 'member'}</Badge>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Members and invites</CardTitle>
          <CardDescription>
            Who else can access the selected workspace, and inviting teammates.
            Only workspace owners and admins can invite, change roles or revoke.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!selectedId && (
            <p className="text-sm text-slate-500">
              Select a workspace above to see its members.
            </p>
          )}

          {selectedId && members.isPending && (
            <p className="text-sm text-slate-500">Loading…</p>
          )}
          {selectedId && members.isError && (
            <p className="text-sm text-red-600">
              Could not load members from the backend.
            </p>
          )}

          {selectedId && members.isSuccess && (
            <>
              {actionError && <p className="mb-3 text-sm text-red-600">{actionError}</p>}
              {actionMessage && <p className="mb-3 text-sm text-emerald-700">{actionMessage}</p>}

              <h3 className="text-sm font-medium text-slate-900">
                Pending invitations ({pendingInvites.length})
              </h3>
              {pendingInvites.length === 0 ? (
                <p className="mt-1 text-sm text-slate-500">
                  No pending invitations for this workspace.
                </p>
              ) : (
                <ul className="mt-2 divide-y">
                  {pendingInvites.map((m) => (
                    <li key={m.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                      <div>
                        <p className="text-sm font-medium text-slate-900">
                          {m.email ?? '—'} · {m.role}
                        </p>
                        <p className="text-xs text-slate-500">
                          Invited by {m.invited_by ?? '—'}
                          {m.created_at ? ` · ${m.created_at.slice(0, 10)}` : ''}
                        </p>
                      </div>
                      {myInvite && m.id === myInvite.id && (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={accept.isPending}
                          onClick={() => accept.mutate(m.id)}
                        >
                          {accept.isPending ? 'Accepting…' : 'Accept invitation'}
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {myInvite && (
                <p className="mt-2 text-xs text-slate-500">
                  This invitation is yours — only you can accept it.
                </p>
              )}

              <h3 className="mt-6 text-sm font-medium text-slate-900">
                Members ({members.data.length})
              </h3>
              <div className="mt-2 overflow-x-auto">
                <table className="w-full min-w-[600px] text-left text-sm">
                  <thead>
                    <tr className="border-b text-slate-500">
                      <th className="py-2 pr-4 font-medium">Member</th>
                      <th className="py-2 pr-4 font-medium">Role</th>
                      <th className="py-2 pr-4 font-medium">Status</th>
                      <th className="py-2 pr-4 font-medium">Invited by</th>
                      <th className="py-2 font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {members.data.map((m: WorkspaceMember) => (
                      <tr key={m.id} className="border-b last:border-0">
                        <td className="py-2 pr-4">
                          <p className="font-medium text-slate-900">
                            {m.display_name ?? m.email ?? '—'}
                          </p>
                          {m.display_name && (
                            <p className="text-xs text-slate-500">{m.email ?? '—'}</p>
                          )}
                        </td>
                        <td className="py-2 pr-4">
                          {canManage && m.role !== 'owner' && m.status !== 'revoked' ? (
                            <select
                              aria-label={`Role for ${m.email ?? m.id}`}
                              value={m.role}
                              disabled={changeRole.isPending}
                              onChange={(e) =>
                                changeRole.mutate({ membershipId: m.id, role: e.target.value })
                              }
                              className="h-9 rounded-md border border-slate-300 bg-white px-2 py-1 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                            >
                              {INVITABLE_ROLES.map((r) => (
                                <option key={r} value={r}>
                                  {r}
                                </option>
                              ))}
                            </select>
                          ) : (
                            m.role
                          )}
                        </td>
                        <td className="py-2 pr-4">
                          <Badge variant={statusVariant(m.status)}>
                            {statusLabel(m.status)}
                          </Badge>
                        </td>
                        <td className="py-2 pr-4">{m.invited_by ?? '—'}</td>
                        <td className="py-2">
                          {canManage && m.role !== 'owner' && m.status !== 'revoked' && (
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={revoke.isPending}
                              onClick={() => revoke.mutate(m.id)}
                            >
                              Revoke
                            </Button>
                          )}
                          {m.status === 'revoked' && (
                            <span className="text-xs text-slate-500">
                              Invite again to restore access.
                            </span>
                          )}
                          {m.role === 'owner' && (
                            <span className="text-xs text-slate-500">
                              Owner role cannot be changed here.
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {canManage ? (
                <form
                  onSubmit={(e) => {
                    e.preventDefault()
                    invite.mutate()
                  }}
                  className="mt-6 flex flex-wrap items-end gap-3 border-t pt-4"
                >
                  <div className="space-y-2">
                    <Label htmlFor="invite-email">Teammate&apos;s email</Label>
                    <Input
                      id="invite-email"
                      type="email"
                      placeholder="teammate@example.com"
                      value={inviteEmail}
                      onChange={(e) => setInviteEmail(e.target.value)}
                    />
                    <p className="text-xs text-slate-500">
                      They must already have a CourierLedger account — invite with
                      the email they registered with.
                    </p>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="invite-role">Role</Label>
                    <select
                      id="invite-role"
                      value={inviteRole}
                      onChange={(e) => setInviteRole(e.target.value)}
                      className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                    >
                      {INVITABLE_ROLES.map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </select>
                    <p className="text-xs text-slate-500">
                      The owner role is never invitable.
                    </p>
                  </div>
                  <Button type="submit" disabled={invite.isPending}>
                    {invite.isPending ? 'Inviting…' : 'Invite teammate'}
                  </Button>
                </form>
              ) : (
                <p className="mt-6 border-t pt-4 text-sm text-slate-500">
                  Only workspace owners and admins can invite teammates or change
                  roles. Your role in this workspace is {selected?.role ?? 'member'}.
                </p>
              )}
              {inviteError && <p className="mt-3 text-sm text-red-600">{inviteError}</p>}
              {inviteMessage && <p className="mt-3 text-sm text-emerald-700">{inviteMessage}</p>}

              {isOwner && (
                <div className="mt-6 border-t pt-4">
                  <h3 className="text-sm font-medium text-slate-900">
                    Transfer ownership · মালিকানা হস্তান্তর
                  </h3>
                  <p className="mt-1 text-xs text-slate-500">
                    Only you, as the workspace owner, can do this. The member you
                    choose becomes the only owner, and you become an admin — an
                    admin cannot transfer ownership back. Only active members
                    can receive ownership; invited and revoked members cannot.
                  </p>
                  {transferCandidates.length === 0 ? (
                    <p className="mt-3 text-sm text-slate-500">
                      No active member can receive ownership yet — invite a
                      teammate and have them accept first.
                    </p>
                  ) : (
                    <form
                      onSubmit={(e) => {
                        e.preventDefault()
                        if (!transferUserId) return
                        if (!transferArmed) {
                          setTransferArmed(true)
                          return
                        }
                        transfer.mutate(transferUserId)
                      }}
                      className="mt-3 flex flex-wrap items-end gap-3"
                    >
                      <div className="space-y-2">
                        <Label htmlFor="transfer-member">New owner</Label>
                        <select
                          id="transfer-member"
                          value={transferUserId}
                          onChange={(e) => {
                            setTransferUserId(e.target.value)
                            setTransferArmed(false)
                          }}
                          className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                        >
                          <option value="">Choose an active member…</option>
                          {transferCandidates.map((m) => (
                            <option key={m.user_id} value={m.user_id}>
                              {m.display_name ?? m.email ?? m.user_id} · {m.role}
                            </option>
                          ))}
                        </select>
                      </div>
                      <Button
                        type="submit"
                        variant={transferArmed ? 'destructive' : 'outline'}
                        disabled={!transferUserId || transfer.isPending}
                      >
                        {transfer.isPending
                          ? 'Transferring…'
                          : transferArmed
                            ? `Confirm transfer to ${transferTarget?.email ?? transferTarget?.display_name ?? 'this member'}?`
                            : 'Transfer ownership'}
                      </Button>
                      {transferArmed && (
                        <Button
                          type="button"
                          variant="ghost"
                          onClick={() => setTransferArmed(false)}
                        >
                          Cancel
                        </Button>
                      )}
                    </form>
                  )}
                  {transferArmed && (
                    <p className="mt-2 text-xs text-slate-500">
                      This cannot be undone by you afterwards — only the new owner
                      can transfer ownership again.
                    </p>
                  )}
                  {transferError && <p className="mt-3 text-sm text-red-600">{transferError}</p>}
                  {transferMessage && (
                    <p className="mt-3 text-sm text-emerald-700">{transferMessage}</p>
                  )}
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
