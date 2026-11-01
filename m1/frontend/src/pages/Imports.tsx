import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import {
  ApiError,
  createCourierAccount,
  listCourierAccounts,
  listImports,
  uploadImport,
} from '../lib/api'
import { CANONICAL_FIELDS, DEFAULT_STATEMENT_MAPPING } from '../lib/demo'
import { useSelectedWorkspace } from '../lib/workspace'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { Input } from '../components/ui/Input'
import { Label } from '../components/ui/Label'
import { WorkspacePicker } from '../components/WorkspacePicker'

/** The five courier codes seeded in the backend (no rates or templates). */
const COURIER_CODES = ['steadfast', 'pathao', 'paperfly', 'redx', 'ecourier']

const accountSchema = z.object({
  courier_code: z.enum(COURIER_CODES as [string, ...string[]]),
  merchant_account_ref: z.string().max(200).optional(),
  display_label: z.string().max(200).optional(),
})

type AccountForm = z.infer<typeof accountSchema>

const uploadSchema = z.object({
  mappingJson: z.string().min(1, 'Mapping JSON is required'),
  merchant_courier_account_id: z.string().min(1, 'Choose a courier account'),
})

type UploadForm = z.infer<typeof uploadSchema>

export function ImportsPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { selectedId, selected } = useSelectedWorkspace()
  const [file, setFile] = useState<File | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [accountError, setAccountError] = useState<string | null>(null)
  const [uploadResult, setUploadResult] = useState<string | null>(null)

  const imports = useQuery({
    queryKey: ['imports', selectedId],
    queryFn: () => listImports(selectedId!),
    enabled: !!selectedId,
    retry: false,
  })

  const accounts = useQuery({
    queryKey: ['courier-accounts', selectedId],
    queryFn: () => listCourierAccounts(selectedId!),
    enabled: !!selectedId,
    retry: false,
  })

  const createAccount = useMutation({
    mutationFn: (values: AccountForm) =>
      createCourierAccount(selectedId!, {
        courier_code: values.courier_code,
        merchant_account_ref: values.merchant_account_ref || null,
        display_label: values.display_label || null,
      }),
    onSuccess: () => {
      setAccountError(null)
      queryClient.invalidateQueries({ queryKey: ['courier-accounts', selectedId] })
    },
    onError: (err) => {
      setAccountError(
        err instanceof ApiError ? err.message : 'Could not create the courier account.',
      )
    },
  })

  const upload = useMutation({
    mutationFn: (values: UploadForm) => {
      if (!file) throw new Error('Choose a CSV file first.')
      let mapping: Record<string, string>
      try {
        mapping = JSON.parse(values.mappingJson) as Record<string, string>
      } catch {
        throw new Error('Mapping is not valid JSON.')
      }
      return uploadImport(selectedId!, file, mapping, values.merchant_courier_account_id)
    },
    onSuccess: (summary) => {
      setUploadError(null)
      setUploadResult(
        `Upload accepted — batch ${summary.batch_id.slice(0, 8)}…: ${summary.parsed_row_count} of ${summary.row_count} rows parsed, ${summary.error_row_count} error rows.`,
      )
      queryClient.invalidateQueries({ queryKey: ['imports', selectedId] })
      navigate(`/imports/${summary.batch_id}`)
    },
    onError: (err) => {
      setUploadResult(null)
      setUploadError(
        err instanceof ApiError ? err.message : err instanceof Error ? err.message : 'Upload failed.',
      )
    },
  })

  const accountForm = useForm<AccountForm>({
    resolver: zodResolver(accountSchema),
    defaultValues: { courier_code: 'steadfast', merchant_account_ref: '', display_label: '' },
  })

  const uploadForm = useForm<UploadForm>({
    resolver: zodResolver(uploadSchema),
    defaultValues: {
      mappingJson: JSON.stringify(DEFAULT_STATEMENT_MAPPING, null, 2),
      merchant_courier_account_id: '',
    },
  })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Imports</h1>
        <p className="mt-1 text-sm text-slate-500">
          Upload courier statement CSVs. The file is stored as-is, raw values are preserved
          verbatim, and each row is parsed with its data-quality state shown — never as a
          silent zero.
        </p>
      </div>

      <WorkspacePicker />

      {!selectedId && (
        <Card>
          <CardContent>
            <p className="text-sm text-slate-500">
              Create a workspace first (Workspaces page), then come back to upload a
              statement.
            </p>
          </CardContent>
        </Card>
      )}

      {selectedId && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Upload a statement CSV</CardTitle>
              <CardDescription>
                Multipart upload to POST /api/workspaces/:id/imports. An import belongs to
                one courier account — that is what ties statement rows to a courier.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {accounts.isSuccess && accounts.data.length === 0 && (
                <div className="rounded-md bg-amber-50 px-4 py-3 text-sm text-amber-800">
                  No courier account exists for {selected?.name ?? 'this workspace'} yet.
                  Create one below before uploading — an import always needs an account.
                </div>
              )}
              {accounts.isError && (
                <p className="text-sm text-red-600">
                  Could not load courier accounts from the backend.
                </p>
              )}
              <form
                onSubmit={uploadForm.handleSubmit((v) => upload.mutate(v))}
                className="space-y-4"
              >
                <div className="space-y-2">
                  <Label htmlFor="csv-file">CSV file</Label>
                  <Input
                    id="csv-file"
                    type="file"
                    accept=".csv,text/csv"
                    onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                  />
                  <p className="text-xs text-slate-500">
                    10 MB limit (local development). The original bytes are preserved with a
                    SHA-256 fingerprint.
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="courier-account">Courier account</Label>
                  <select
                    id="courier-account"
                    className="h-10 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                    {...uploadForm.register('merchant_courier_account_id')}
                  >
                    <option value="">Select a courier account…</option>
                    {(accounts.data ?? []).map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.courier_code}
                        {a.display_label ? ` — ${a.display_label}` : ''}
                        {a.merchant_account_ref ? ` (${a.merchant_account_ref})` : ''}
                      </option>
                    ))}
                  </select>
                  {uploadForm.formState.errors.merchant_courier_account_id && (
                    <p className="text-xs text-red-600">
                      {uploadForm.formState.errors.merchant_courier_account_id.message}
                    </p>
                  )}
                </div>
                <div className="space-y-2">
                  <Label htmlFor="mapping-json">Column mapping (canonical field → CSV header)</Label>
                  <textarea
                    id="mapping-json"
                    rows={11}
                    spellCheck={false}
                    className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-xs text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                    {...uploadForm.register('mappingJson')}
                  />
                  {uploadForm.formState.errors.mappingJson && (
                    <p className="text-xs text-red-600">
                      {uploadForm.formState.errors.mappingJson.message}
                    </p>
                  )}
                  <p className="text-xs text-slate-500">
                    Accepted canonical fields: {CANONICAL_FIELDS.join(', ')}. The mapping is
                    stored unchanged with the batch (immutable snapshot) and validated by the
                    backend — an unknown field is rejected with 422.
                  </p>
                </div>
                {uploadError && <p className="text-sm text-red-600">{uploadError}</p>}
                {uploadResult && <p className="text-sm text-emerald-700">{uploadResult}</p>}
                <Button type="submit" disabled={upload.isPending || !file}>
                  {upload.isPending ? 'Uploading…' : 'Upload statement'}
                </Button>
              </form>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Create a courier account</CardTitle>
              <CardDescription>
                Links this workspace to one courier (code only — no rates, templates,
                passwords or API keys are stored here).
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form
                onSubmit={accountForm.handleSubmit((v) => createAccount.mutate(v))}
                className="flex flex-wrap items-end gap-3"
              >
                <div className="space-y-2">
                  <Label htmlFor="courier-code">Courier</Label>
                  <select
                    id="courier-code"
                    className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                    {...accountForm.register('courier_code')}
                  >
                    {COURIER_CODES.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="flex-1 space-y-2">
                  <Label htmlFor="account-ref">Merchant account reference (optional)</Label>
                  <Input
                    id="account-ref"
                    placeholder="e.g. your Steadfast merchant id"
                    {...accountForm.register('merchant_account_ref')}
                  />
                </div>
                <div className="flex-1 space-y-2">
                  <Label htmlFor="account-label">Display label (optional)</Label>
                  <Input
                    id="account-label"
                    placeholder="e.g. Steadfast — main account"
                    {...accountForm.register('display_label')}
                  />
                </div>
                <Button type="submit" disabled={createAccount.isPending}>
                  {createAccount.isPending ? 'Creating…' : 'Create account'}
                </Button>
              </form>
              {accountError && <p className="mt-3 text-sm text-red-600">{accountError}</p>}
              {createAccount.isSuccess && (
                <p className="mt-3 text-sm text-emerald-700">
                  Courier account created — it now appears in the upload form above.
                </p>
              )}
              {accounts.isSuccess && accounts.data.length > 0 && (
                <ul className="mt-4 divide-y">
                  {accounts.data.map((a) => (
                    <li key={a.id} className="flex items-center justify-between py-2 text-sm">
                      <span className="font-medium text-slate-900">
                        {a.courier_code}
                        {a.display_label ? ` — ${a.display_label}` : ''}
                      </span>
                      <span className="text-xs text-slate-500">
                        {a.merchant_account_ref ?? 'no reference'} · {a.status}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Import batches</CardTitle>
              <CardDescription>
                Loaded from GET /api/workspaces/:id/imports (newest first).
              </CardDescription>
            </CardHeader>
            <CardContent>
              {imports.isPending && <p className="text-sm text-slate-500">Loading…</p>}
              {imports.isError && (
                <p className="text-sm text-red-600">
                  Could not load imports from the backend. Is it running on port 8000?
                </p>
              )}
              {imports.isSuccess && imports.data.length === 0 && (
                <p className="text-sm text-slate-500">
                  No imports yet for this workspace — upload a statement CSV above.
                </p>
              )}
              {imports.isSuccess && imports.data.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[640px] text-left text-sm">
                    <thead>
                      <tr className="border-b text-slate-500">
                        <th className="py-2 pr-4 font-medium">Batch</th>
                        <th className="py-2 pr-4 font-medium">Status</th>
                        <th className="py-2 pr-4 font-medium">Rows</th>
                        <th className="py-2 pr-4 font-medium">Parsed</th>
                        <th className="py-2 pr-4 font-medium">Error rows</th>
                        <th className="py-2 font-medium">Created</th>
                      </tr>
                    </thead>
                    <tbody>
                      {imports.data.map((b) => (
                        <tr key={b.batch_id} className="border-b last:border-0">
                          <td className="py-2 pr-4">
                            <Link
                              to={`/imports/${b.batch_id}`}
                              className="font-mono text-xs font-medium text-primary hover:underline"
                            >
                              {b.batch_id.slice(0, 8)}…
                            </Link>
                          </td>
                          <td className="py-2 pr-4">
                            <Badge variant={b.status === 'parsed' ? 'success' : 'warning'}>
                              {b.status}
                            </Badge>
                          </td>
                          <td className="py-2 pr-4">{b.row_count}</td>
                          <td className="py-2 pr-4">{b.parsed_row_count}</td>
                          <td className="py-2 pr-4">{b.error_row_count}</td>
                          <td className="py-2">
                            {b.created_at ? b.created_at.slice(0, 19).replace('T', ' ') : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="mt-4 text-xs text-slate-500">
                To reconcile an import, open its detail page and create a run from it —
                runs are listed under Reconciliation.
              </p>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
