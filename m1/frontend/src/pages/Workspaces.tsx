import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { ApiError, createWorkspace, listWorkspaces } from '../lib/api'
import { Button } from '../components/ui/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { Input } from '../components/ui/Input'
import { Label } from '../components/ui/Label'

const workspaceSchema = z.object({
  name: z.string().min(1, 'Workspace name is required').max(100),
})

type WorkspaceForm = z.infer<typeof workspaceSchema>

export function WorkspacesPage() {
  const queryClient = useQueryClient()
  const [serverError, setServerError] = useState<string | null>(null)

  // GET /api/workspaces. On error the page says so honestly — it never
  // falls back to invented demo workspaces.
  const workspaces = useQuery({
    queryKey: ['workspaces'],
    queryFn: listWorkspaces,
    retry: false,
  })

  const create = useMutation({
    mutationFn: (name: string) => createWorkspace(name),
    onSuccess: () => {
      setServerError(null)
      queryClient.invalidateQueries({ queryKey: ['workspaces'] })
    },
    onError: (err) => {
      setServerError(
        err instanceof ApiError
          ? err.message
          : 'Could not reach the backend. Is it running on port 8000?',
      )
    },
  })

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<WorkspaceForm>({ resolver: zodResolver(workspaceSchema) })

  function onSubmit(values: WorkspaceForm) {
    create.mutate(values.name, { onSuccess: () => reset() })
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Workspaces</h1>
        <p className="mt-1 text-sm text-slate-500">
          A workspace is one merchant business. Each workspace has its own shipments, couriers
          and reconciliation.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Create a workspace</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit(onSubmit)} className="flex items-end gap-3">
            <div className="flex-1 space-y-2">
              <Label htmlFor="workspace-name">Workspace name</Label>
              <Input
                id="workspace-name"
                placeholder="e.g. My Shop — Dhaka"
                {...register('name')}
              />
              {errors.name && <p className="text-xs text-red-600">{errors.name.message}</p>}
            </div>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? 'Creating…' : 'Create workspace'}
            </Button>
          </form>
          {serverError && <p className="mt-3 text-sm text-red-600">{serverError}</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Your workspaces</CardTitle>
          <CardDescription>
            {workspaces.isSuccess
              ? 'Loaded from the backend.'
              : 'Could not load workspaces from the backend.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {workspaces.isPending && <p className="text-sm text-slate-500">Loading…</p>}
          {workspaces.isSuccess && workspaces.data.length === 0 && (
            <p className="text-sm text-slate-500">No workspaces yet — create one above.</p>
          )}
          {workspaces.isSuccess && workspaces.data.length > 0 && (
            <ul className="divide-y">
              {workspaces.data.map((w) => (
                <li key={w.id} className="py-3 text-sm font-medium text-slate-900">
                  {w.name}
                </li>
              ))}
            </ul>
          )}
          {workspaces.isError && (
            <p className="text-sm text-red-600">
              Could not load workspaces from the backend. Is it running on port 8000?
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
