import { createContext, useCallback, useContext, useMemo, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiFetch, errorMessage } from '../api/client'
import { roleOf, type AppRole, type SharedStatus } from '../api/types'

interface RoleContextValue {
  role: AppRole
  status: SharedStatus | undefined
  loading: boolean
  error: string
  reload: () => void
}

const RoleContext = createContext<RoleContextValue | null>(null)

/**
 * 角色由服务端 `GET /api/shared/status` 决定，浏览器只负责隐藏无权入口；
 * 真正的写权限仍由 Python 中间件执行。
 */
export function RoleProvider({ children }: { children: ReactNode }) {
  const query = useQuery({
    queryKey: ['shared-status'],
    queryFn: ({ signal }) => apiFetch<SharedStatus>('/api/shared/status?include_history=false', { signal }),
    staleTime: 15_000,
    refetchInterval: 30_000,
  })

  const reload = useCallback(() => {
    void query.refetch()
  }, [query])

  const value = useMemo<RoleContextValue>(
    () => ({
      role: roleOf(query.data),
      status: query.data,
      loading: query.isPending,
      error: query.error ? errorMessage(query.error) : '',
      reload,
    }),
    [query.data, query.isPending, query.error, reload],
  )

  return <RoleContext.Provider value={value}>{children}</RoleContext.Provider>
}

export function useRole(): RoleContextValue {
  const value = useContext(RoleContext)
  if (!value) throw new Error('useRole 必须在 RoleProvider 内使用')
  return value
}

export const ROLE_LABEL: Record<AppRole, string> = {
  hub: '共享后台',
  worker: 'Windows 采集端',
  standalone: '本机独立模式',
}
