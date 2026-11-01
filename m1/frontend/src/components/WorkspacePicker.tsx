import { useSelectedWorkspace } from '../lib/workspace'
import { Label } from './ui/Label'

/** Workspace picker shown on pages that are scoped to one workspace. */
export function WorkspacePicker() {
  const { workspaces, selectedId, setSelectedId, isLoading, isError } = useSelectedWorkspace()

  if (isLoading) {
    return <p className="text-sm text-slate-500">Loading workspaces…</p>
  }
  if (isError) {
    return (
      <p className="text-sm text-red-600">
        Could not load workspaces from the backend. Is it running on port 8000?
      </p>
    )
  }
  if (workspaces.length === 0) {
    return (
      <p className="text-sm text-slate-500">
        No workspaces yet — create one on the{' '}
        <a href="/workspaces" className="font-medium text-primary hover:underline">
          Workspaces
        </a>{' '}
        page.
      </p>
    )
  }

  return (
    <div className="flex items-center gap-2">
      <Label htmlFor="workspace-picker">Workspace</Label>
      <select
        id="workspace-picker"
        className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
        value={selectedId ?? ''}
        onChange={(e) => setSelectedId(e.target.value)}
      >
        {workspaces.map((w) => (
          <option key={w.id} value={w.id}>
            {w.name}
          </option>
        ))}
      </select>
    </div>
  )
}
