import type { ReactNode } from 'react'
import { STATUS_BANGLA, type ReconciliationStatus } from '../../lib/demo'

type BadgeVariant = 'default' | 'success' | 'warning' | 'danger' | 'info' | 'muted'

const variantClasses: Record<BadgeVariant, string> = {
  default: 'bg-indigo-100 text-indigo-800',
  success: 'bg-emerald-100 text-emerald-800',
  warning: 'bg-amber-100 text-amber-800',
  danger: 'bg-red-100 text-red-800',
  info: 'bg-sky-100 text-sky-800',
  muted: 'bg-slate-100 text-slate-600',
}

export function Badge({
  children,
  variant = 'default',
  className = '',
}: {
  children: ReactNode
  variant?: BadgeVariant
  className?: string
}) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${variantClasses[variant]} ${className}`}
    >
      {children}
    </span>
  )
}

const statusVariant: Record<ReconciliationStatus, BadgeVariant> = {
  matched: 'success',
  mismatch: 'danger',
  needs_review: 'warning',
  open: 'info',
  matched_with_tolerance: 'muted',
}

/** Status badge with English label + short Bangla explanation. */
export function StatusBadge({ status }: { status: ReconciliationStatus }) {
  return (
    <Badge variant={statusVariant[status]}>
      {status} · {STATUS_BANGLA[status]}
    </Badge>
  )
}
