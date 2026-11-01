import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { getHealth, listImports } from '../lib/api'
import { useDemoReconcile } from '../lib/reconcileDemo'
import { formatTaka, type ReconciliationStatus } from '../lib/demo'
import { useSelectedWorkspace } from '../lib/workspace'
import { Badge, StatusBadge } from '../components/ui/Badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { WorkspacePicker } from '../components/WorkspacePicker'

export function DashboardPage() {
  const health = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    retry: false,
    refetchInterval: 30_000,
  })

  const { workspaces, selectedId, selected } = useSelectedWorkspace()
  const demo = useDemoReconcile()

  const imports = useQuery({
    queryKey: ['imports', selectedId],
    queryFn: () => listImports(selectedId!),
    enabled: !!selectedId,
    retry: false,
  })

  const matched = demo.results.filter((r) => r.overall_status === 'matched').length
  const open = demo.results.filter((r) => r.overall_status === 'open').length
  const needsReview = demo.results.filter((r) => r.overall_status === 'needs_review').length
  const mismatch = demo.results.filter((r) => r.overall_status === 'mismatch').length
  const latestImport = imports.data?.[0]

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Dashboard</h1>
        <p className="mt-1 text-sm text-slate-500">
          COD reconciliation overview. Import figures below are real data from the backend;
          shipment figures are the labelled synthetic demo run.
        </p>
      </div>

      <WorkspacePicker />

      <Card>
        <CardHeader>
          <CardTitle>Backend status</CardTitle>
          <CardDescription>
            Live check of GET /api/health (via the Vite dev proxy to http://127.0.0.1:8000).
          </CardDescription>
        </CardHeader>
        <CardContent>
          {health.isPending && <p className="text-sm text-slate-500">Checking backend…</p>}
          {health.isError && (
            <div className="space-y-1">
              <Badge variant="danger">Backend unreachable</Badge>
              <p className="text-sm text-slate-600">
                {health.error instanceof Error ? health.error.message : 'Unknown error'} — start
                the backend on port 8000.
              </p>
            </div>
          )}
          {health.isSuccess && (
            <div className="space-y-1">
              <Badge variant="success">Backend reachable</Badge>
              <p className="text-sm text-slate-600">
                status: <code className="rounded bg-slate-100 px-1">{health.data.status}</code>
                {health.data.version ? ` · version ${health.data.version}` : ''}
                {typeof health.data.database === 'string' ? ` · database: ${health.data.database}` : ''}
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card>
          <CardContent className="pt-4">
            <p className="text-sm text-slate-500">Workspaces (real)</p>
            <p className="mt-1 text-2xl font-bold">{workspaces.length}</p>
            <p className="text-xs text-slate-500">from GET /api/workspaces</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4">
            <p className="text-sm text-slate-500">Import batches (real)</p>
            <p className="mt-1 text-2xl font-bold">
              {imports.isSuccess ? imports.data.length : '—'}
            </p>
            <p className="text-xs text-slate-500">
              {selected ? `for ${selected.name}` : 'select a workspace'}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4">
            <p className="text-sm text-slate-500">Latest import rows (real)</p>
            <p className="mt-1 text-2xl font-bold">
              {latestImport ? `${latestImport.parsed_row_count}/${latestImport.row_count} parsed` : '—'}
            </p>
            <p className="text-xs text-slate-500">
              {latestImport ? `${latestImport.error_row_count} error rows` : 'no imports yet'}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4">
            <p className="text-sm text-slate-500">Demo shipments reconciled</p>
            <p className="mt-1 text-2xl font-bold">
              {demo.results.length > 0 ? demo.results.length : '—'}
            </p>
            <p className="text-xs text-slate-500">
              <Badge variant="warning">SYNTHETIC DEMO</Badge>
            </p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Demo reconciliation — status counts</CardTitle>
          <CardDescription>
            <Badge variant="warning">SYNTHETIC DEMO</Badge> — five invented shipments
            reconciled live by POST /api/reconcile. Nothing is saved.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {demo.isPending && <p className="text-sm text-slate-500">Reconciling…</p>}
          {demo.isError && (
            <p className="text-sm text-red-600">
              Could not reach the reconcile endpoint: {demo.error?.message ?? 'unknown error'}
            </p>
          )}
          {!demo.isPending && !demo.isError && (
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              <div>
                <p className="text-sm text-slate-500">Matched · মিলেছে</p>
                <p className="mt-1 text-2xl font-bold">{matched}</p>
              </div>
              <div>
                <p className="text-sm text-slate-500">Open · খোলা</p>
                <p className="mt-1 text-2xl font-bold">{open}</p>
              </div>
              <div>
                <p className="text-sm text-slate-500">Needs review · যাচাই প্রয়োজন</p>
                <p className="mt-1 text-2xl font-bold">{needsReview}</p>
              </div>
              <div>
                <p className="text-sm text-slate-500">Mismatch · অমিল</p>
                <p className="mt-1 text-2xl font-bold">{mismatch}</p>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Recent shipments (demo)</CardTitle>
          <CardDescription>
            <Badge variant="warning">SYNTHETIC DEMO</Badge> — invented rows reconciled live,
            not real shipments.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {demo.isPending && <p className="text-sm text-slate-500">Reconciling…</p>}
          {demo.isError && (
            <p className="text-sm text-red-600">
              Could not reach the reconcile endpoint.
            </p>
          )}
          {!demo.isPending && !demo.isError && (
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b text-slate-500">
                  <th className="py-2 pr-4 font-medium">Tracking</th>
                  <th className="py-2 pr-4 font-medium">Expected COD (E)</th>
                  <th className="py-2 pr-4 font-medium">Reported (R)</th>
                  <th className="py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {demo.results.map((r) => (
                  <tr key={r.tracking_id ?? ''} className="border-b last:border-0">
                    <td className="py-2 pr-4 font-mono text-xs">{r.tracking_id}</td>
                    <td className="py-2 pr-4">{formatTaka(r.E)}</td>
                    <td className="py-2 pr-4">{formatTaka(r.R)}</td>
                    <td className="py-2">
                      <StatusBadge status={r.overall_status as ReconciliationStatus} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Recent imports (real data)</CardTitle>
          <CardDescription>
            Latest statement imports for {selected?.name ?? 'the selected workspace'}.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {imports.isPending && <p className="text-sm text-slate-500">Loading…</p>}
          {imports.isError && (
            <p className="text-sm text-red-600">
              Could not load imports from the backend.
            </p>
          )}
          {imports.isSuccess && imports.data.length === 0 && (
            <p className="text-sm text-slate-500">
              No imports yet —{' '}
              <Link to="/imports" className="font-medium text-primary hover:underline">
                upload a statement CSV
              </Link>
              .
            </p>
          )}
          {imports.isSuccess && imports.data.length > 0 && (
            <ul className="divide-y">
              {imports.data.slice(0, 5).map((b) => (
                <li key={b.batch_id} className="flex items-center justify-between py-3">
                  <div>
                    <Link
                      to={`/imports/${b.batch_id}`}
                      className="font-mono text-xs font-medium text-primary hover:underline"
                    >
                      {b.batch_id.slice(0, 8)}…
                    </Link>
                    <p className="text-xs text-slate-500">
                      {b.parsed_row_count} of {b.row_count} rows parsed · {b.error_row_count}{' '}
                      error rows
                      {b.created_at ? ` · ${b.created_at.slice(0, 19).replace('T', ' ')}` : ''}
                    </p>
                  </div>
                  <Badge variant={b.status === 'parsed' ? 'success' : 'warning'}>{b.status}</Badge>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
