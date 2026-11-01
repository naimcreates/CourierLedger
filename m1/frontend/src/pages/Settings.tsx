import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import {
  ApiError,
  getWorkspaceSettings,
  updateWorkspaceSettings,
} from '../lib/api'
import { useSelectedWorkspace } from '../lib/workspace'
import { Button } from '../components/ui/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { Input } from '../components/ui/Input'
import { Label } from '../components/ui/Label'
import { WorkspacePicker } from '../components/WorkspacePicker'

const settingsSchema = z.object({
  tolerancePoisha: z
    .number()
    .int('Tolerance must be a whole number of poisha')
    .min(0, 'Tolerance cannot be negative'),
  locale: z.enum(['en', 'en_bn']),
})

type SettingsForm = z.infer<typeof settingsSchema>

function formatTaka(poisha: number): string {
  return `৳${(poisha / 100).toFixed(2)}`
}

/**
 * Settings page. Loads the real workspace settings from
 * GET /api/workspaces/:id/settings and saves through
 * PATCH /api/workspaces/:id/settings (Owner/Admin only).
 *
 * Tolerance is the largest difference (in poisha) still counted as
 * matched. The gross difference is always stored and shown,
 * whatever the tolerance is, and a change affects future runs only.
 */
export function SettingsPage() {
  const { selected, selectedId, isLoading: workspacesLoading } = useSelectedWorkspace()
  const queryClient = useQueryClient()
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saveMessage, setSaveMessage] = useState<string | null>(null)

  const canEdit = selected?.role === 'owner' || selected?.role === 'admin'

  const settings = useQuery({
    queryKey: ['workspace-settings', selectedId],
    queryFn: () => getWorkspaceSettings(selectedId!),
    enabled: !!selectedId,
    retry: false,
  })

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting },
  } = useForm<SettingsForm>({
    resolver: zodResolver(settingsSchema),
    defaultValues: { tolerancePoisha: 0, locale: 'en_bn' },
  })

  // Fill the form with the fetched settings when they arrive.
  useEffect(() => {
    if (settings.data) {
      reset({
        tolerancePoisha: settings.data.tolerance_poisha,
        locale: settings.data.locale,
      })
    }
  }, [settings.data, reset])

  const save = useMutation({
    mutationFn: (values: SettingsForm) =>
      updateWorkspaceSettings(selectedId!, {
        tolerance_poisha: values.tolerancePoisha,
        locale: values.locale,
      }),
    onSuccess: (updated) => {
      setSaveError(null)
      setSaveMessage(
        `Settings saved — tolerance ${updated.tolerance_poisha} poisha (${formatTaka(updated.tolerance_poisha)}). Future runs will use it; past runs are unchanged.`,
      )
      queryClient.invalidateQueries({ queryKey: ['workspace-settings', selectedId] })
    },
    onError: (err) => {
      setSaveMessage(null)
      setSaveError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Could not save settings.',
      )
    },
  })

  function onSubmit(values: SettingsForm) {
    setSaveError(null)
    setSaveMessage(null)
    save.mutate(values)
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Settings</h1>
        <p className="mt-1 text-sm text-slate-500">
          Workspace settings, loaded from the backend. A change here affects future
          reconciliation runs only — past runs keep the tolerance they used.
        </p>
      </div>

      <WorkspacePicker />

      {!selectedId && !workspacesLoading && (
        <p className="text-sm text-slate-500">
          No workspace selected — create one on the{' '}
          <a href="/workspaces" className="font-medium text-primary hover:underline">
            Workspaces
          </a>{' '}
          page first.
        </p>
      )}

      {selectedId && settings.isPending && (
        <p className="text-sm text-slate-500">Loading settings…</p>
      )}

      {selectedId && settings.isError && (
        <p className="text-sm text-red-600">
          Could not load settings from the backend. Is it running on port 8000?
        </p>
      )}

      {selectedId && settings.data && (
        <Card>
          <CardHeader>
            <CardTitle>Reconciliation settings</CardTitle>
            <CardDescription>
              Tolerance is the largest difference (in poisha) still counted as matched.
              সহনশীলতা (tolerance) — এই পরিমাণ পর্যন্ত পার্থক্য থাকলেও ফলাফল মিলেছে বলে
              ধরা হয়। পার্থক্যের আসল পরিমাণ সবসময় দেখানো হয়, সহনশীলতা যাই হোক না কেন।
            </CardDescription>
          </CardHeader>
          <CardContent>
            <p className="mb-4 text-sm text-slate-700">
              Current tolerance:{' '}
              <span className="font-medium">
                {settings.data.tolerance_poisha} poisha (
                {formatTaka(settings.data.tolerance_poisha)})
              </span>
              {settings.data.is_default
                ? ' — no settings saved yet, this is the default (৳0.00).'
                : ` — saved${settings.data.updated_at ? ` on ${settings.data.updated_at.slice(0, 10)}` : ''}.`}
            </p>
            {!canEdit && (
              <p className="mb-4 text-sm text-slate-500">
                Your role in this workspace is “{selected?.role ?? 'member'}”. Only owners
                and admins can change settings, so this page is read-only for you.
              </p>
            )}
            <form
              onSubmit={handleSubmit(onSubmit)}
              noValidate
              className="max-w-md space-y-4"
            >
              <div className="space-y-2">
                <Label htmlFor="tolerancePoisha">Tolerance (poisha)</Label>
                <Input
                  id="tolerancePoisha"
                  type="number"
                  min={0}
                  disabled={!canEdit}
                  {...register('tolerancePoisha', { valueAsNumber: true })}
                />
                {errors.tolerancePoisha && (
                  <p className="text-xs text-red-600">{errors.tolerancePoisha.message}</p>
                )}
                <p className="text-xs text-slate-500">
                  A difference up to this amount is counted as matched. Send 0 to restore
                  the default (৳0.00). The full difference is always shown on each result.
                </p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="locale">Language</Label>
                <select
                  id="locale"
                  className="h-10 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-50"
                  disabled={!canEdit}
                  {...register('locale')}
                >
                  <option value="en_bn">English with Bangla explanations</option>
                  <option value="en">English only</option>
                </select>
                {errors.locale && (
                  <p className="text-xs text-red-600">{errors.locale.message}</p>
                )}
                <p className="text-xs text-slate-500">
                  Saved on the workspace. The app currently shows English with Bangla
                  explanations for money terms.
                </p>
              </div>
              {canEdit && (
                <Button type="submit" disabled={isSubmitting || save.isPending}>
                  {save.isPending ? 'Saving…' : 'Save settings'}
                </Button>
              )}
              {saveError && <p className="text-sm text-red-600">{saveError}</p>}
              {saveMessage && <p className="text-sm text-green-700">{saveMessage}</p>}
            </form>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
