import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchConfig, fetchDebugSummary } from './api'

export const settingsKeys = {
  config: ['settings', 'config'] as const,
  debug: ['settings', 'debug-summary'] as const,
}

export function useConfig() {
  return useQuery({
    queryKey: settingsKeys.config,
    queryFn: ({ signal }) => fetchConfig(signal),
    staleTime: 10_000,
  })
}

/** 占用统计按需读取，避免每次进入设置页都扫描调试目录。 */
export function useDebugSummary(enabled = true) {
  return useQuery({
    queryKey: settingsKeys.debug,
    queryFn: ({ signal }) => fetchDebugSummary(signal),
    enabled,
    staleTime: 5_000,
  })
}

export function useRefreshConfig(): () => void {
  const client = useQueryClient()
  return () => {
    void client.invalidateQueries({ queryKey: settingsKeys.config })
  }
}
