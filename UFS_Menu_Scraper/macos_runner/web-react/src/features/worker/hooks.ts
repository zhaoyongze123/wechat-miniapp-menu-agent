import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchWorkerStatus } from './api'

export const WORKER_STATUS_KEY = ['worker-status'] as const

/** 运行中 3 秒轮询，空闲时也保持与旧工作台一致的轻量刷新。 */
export function useWorkerStatus(enabled: boolean) {
  return useQuery({
    queryKey: WORKER_STATUS_KEY,
    queryFn: ({ signal }) => fetchWorkerStatus(signal),
    enabled,
    staleTime: 2000,
    refetchInterval: 3000,
    refetchIntervalInBackground: false,
  })
}

export function useRefreshWorkerStatus(): () => void {
  const client = useQueryClient()
  return () => {
    void client.invalidateQueries({ queryKey: WORKER_STATUS_KEY })
  }
}
