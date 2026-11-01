import type { ReactNode } from 'react'

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-lg border border-slate-200 bg-white shadow-sm ${className}`}>
      {children}
    </div>
  )
}

export function CardHeader({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`border-b border-slate-100 px-6 py-4 ${className}`}>{children}</div>
}

export function CardTitle({ children }: { children: ReactNode }) {
  return <h2 className="text-base font-semibold text-slate-900">{children}</h2>
}

export function CardDescription({ children }: { children: ReactNode }) {
  return <p className="mt-1 text-sm text-slate-500">{children}</p>
}

export function CardContent({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`px-6 py-4 ${className}`}>{children}</div>
}
