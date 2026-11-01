import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { listWorkspaces, type Workspace } from './api'

const STORAGE_KEY = 'courierledger-selected-workspace'

function readStoredId(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

/**
 * The workspace the user is currently looking at. The choice is stored
 * in localStorage so it survives reloads; it defaults to the first
 * workspace the backend lists for this user.
 */
export function useSelectedWorkspace(): {
  workspaces: Workspace[]
  selected: Workspace | null
  selectedId: string | null
  setSelectedId: (id: string) => void
  isLoading: boolean
  isError: boolean
  error: Error | null
} {
  const query = useQuery({
    queryKey: ['workspaces'],
    queryFn: listWorkspaces,
    retry: false,
  })

  const [storedId, setStoredId] = useState<string | null>(() => readStoredId())

  const selectedId = useMemo(() => {
    const list = query.data ?? []
    if (list.length === 0) return null
    if (storedId && list.some((w) => w.id === storedId)) return storedId
    return list[0].id
  }, [query.data, storedId])

  const selected = (query.data ?? []).find((w) => w.id === selectedId) ?? null

  return {
    workspaces: query.data ?? [],
    selected,
    selectedId,
    setSelectedId: (id: string) => {
      setStoredId(id)
      try {
        localStorage.setItem(STORAGE_KEY, id)
      } catch {
        // Storage unavailable — selection just won't persist.
      }
    },
    isLoading: query.isPending,
    isError: query.isError,
    error: query.error instanceof Error ? query.error : null,
  }
}
