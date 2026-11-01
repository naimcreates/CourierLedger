import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query'
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { Layout } from './components/Layout'
import { PlaceholderPage } from './components/PlaceholderPage'
import { getMe } from './lib/api'
import { DashboardPage } from './pages/Dashboard'
import { ImportsPage } from './pages/Imports'
import { ImportDetailPage } from './pages/ImportDetail'
import { LoginPage } from './pages/Login'
import { ReconciliationPage } from './pages/Reconciliation'
import { RunDetailPage } from './pages/Runs'
import { RegisterPage } from './pages/Register'
import { SettingsPage } from './pages/Settings'
import { ShipmentsPage } from './pages/Shipments'
import { TeamPage } from './pages/Team'
import { WorkspacesPage } from './pages/Workspaces'
import { BankReceiptsPage } from './pages/BankReceipts'
import { CourierStatementsPage } from './pages/CourierStatements'
import { ReportsPage } from './pages/Reports'
import { SettlementBatchesPage, SettlementBatchDetailPage } from './pages/SettlementBatches'

const queryClient = new QueryClient()

/**
 * Auth guard: every route under Layout requires a live session.
 * GET /api/auth/me returns 401 without a session cookie, so
 * unauthenticated users are sent to /login instead of seeing the
 * dashboard. Login/register stay public.
 */
function ProtectedLayout() {
  const location = useLocation()
  const me = useQuery({
    queryKey: ['me'],
    queryFn: getMe,
    retry: false,
    staleTime: 60_000,
  })

  if (me.isPending) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-slate-500">Checking your session…</p>
      </div>
    )
  }
  if (me.isError) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />
  }
  return <Layout />
}

/**
 * Phase 1 (C-6.2): global 401 handler. apiFetch dispatches
 * `cl:unauthorized` whenever any API call returns 401 (e.g. a
 * session expiring mid-use); this listener clears the React Query
 * cache — so no stale protected data is shown — and redirects to
 * /login. The ProtectedLayout guard still covers the initial load.
 */
function UnauthorizedRedirect() {
  const navigate = useNavigate()
  const client = useQueryClient()
  useEffect(() => {
    const onUnauthorized = () => {
      client.clear()
      navigate('/login', { replace: true })
    }
    window.addEventListener('cl:unauthorized', onUnauthorized)
    return () => window.removeEventListener('cl:unauthorized', onUnauthorized)
  }, [navigate, client])
  return null
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <UnauthorizedRedirect />
        <Routes>
          <Route element={<ProtectedLayout />}>
            <Route index element={<DashboardPage />} />
            <Route path="shipments" element={<ShipmentsPage />} />
            <Route path="workspaces" element={<WorkspacesPage />} />
            <Route path="imports" element={<ImportsPage />} />
            <Route path="imports/:batchId" element={<ImportDetailPage />} />
            <Route path="reconciliation" element={<ReconciliationPage />} />
            <Route path="runs/:runId" element={<RunDetailPage />} />
            <Route path="courier-statements" element={<CourierStatementsPage />} />
            <Route path="settlement-batches" element={<SettlementBatchesPage />} />
            <Route path="settlement-batches/:batchId" element={<SettlementBatchDetailPage />} />
            <Route path="bank-receipts" element={<BankReceiptsPage />} />
            <Route path="reports" element={<ReportsPage />} />
            <Route path="team" element={<TeamPage />} />
            <Route path="settings" element={<SettingsPage />} />
          </Route>
          <Route path="login" element={<LoginPage />} />
          <Route path="register" element={<RegisterPage />} />
          <Route
            path="*"
            element={
              <PlaceholderPage
                title="Page not found"
                description="This route does not exist yet."
              />
            }
          />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  )
}

export default App
