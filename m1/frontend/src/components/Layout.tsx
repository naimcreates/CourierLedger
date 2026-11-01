import { useMutation, useQueryClient } from '@tanstack/react-query'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { logout, type CurrentUser } from '../lib/api'
import { useSelectedWorkspace } from '../lib/workspace'
import { Badge } from './ui/Badge'
import { Button } from './ui/Button'

const NAV = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/shipments', label: 'Shipments' },
  { to: '/imports', label: 'Imports' },
  { to: '/reconciliation', label: 'Reconciliation' },
  { to: '/courier-statements', label: 'Courier Statements' },
  { to: '/settlement-batches', label: 'Settlement Batches' },
  { to: '/bank-receipts', label: 'Bank Receipts' },
  { to: '/reports', label: 'Reports' },
  { to: '/team', label: 'Team and Workspace' },
  { to: '/settings', label: 'Settings' },
]

export function Layout() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { selected, isLoading } = useSelectedWorkspace()
  // The auth guard (App.tsx) runs the ['me'] query; reuse its cached value here.
  const me = queryClient.getQueryData<CurrentUser>(['me'])

  const signOut = useMutation({
    mutationFn: logout,
    onSettled: () => {
      queryClient.clear()
      navigate('/login')
    },
  })

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-64 shrink-0 flex-col border-r border-slate-200 bg-white">
        <div className="border-b border-slate-100 px-5 py-4">
          <p className="text-lg font-bold text-primary">CourierLedger</p>
          <p className="text-xs text-slate-500">COD courier reconciliation · Bangladesh</p>
        </div>
        <nav className="flex-1 space-y-1 px-3 py-4" aria-label="Main navigation">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `block rounded-md px-3 py-2 text-sm font-medium ${
                  isActive
                    ? 'bg-primary-50 text-primary-700'
                    : 'text-slate-700 hover:bg-slate-100'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-slate-100 px-5 py-3">
          <Badge variant="warning">SYNTHETIC DEMO</Badge>
          <p className="mt-2 text-xs text-slate-500">
            Demo data only — no real merchants or courier rates.
          </p>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-8 py-3">
          <p className="text-sm text-slate-500">
            Merchant workspace:{' '}
            {isLoading
              ? 'Loading…'
              : selected
                ? selected.name
                : 'No workspace yet — create one on the Workspaces page.'}
          </p>
          <div className="flex items-center gap-3">
            <Badge variant="muted">Light theme</Badge>
            {me?.email && <span className="text-sm text-slate-600">{me.email}</span>}
            <Button variant="ghost" size="sm" onClick={() => signOut.mutate()} disabled={signOut.isPending}>
              {signOut.isPending ? 'Signing out…' : 'Sign out'}
            </Button>
          </div>
        </header>
        <main className="mx-auto w-full max-w-6xl flex-1 px-8 py-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
