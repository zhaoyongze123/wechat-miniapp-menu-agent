import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchWorkerStatus } from './api'
import { useProject } from '../projects/ProjectProvider'
import { projectQueryKey } from '../projects/scope'

export const WORKER_STATUS_KEY = (projectId: string) => projectQueryKey(projectId, 'worker-status')

/** 运行中 3 秒轮询，空闲时也保持与旧工作台一致的轻量刷新。 */
export function useWorkerStatus(enabled: boolean) {
  const { projectId } = useProject()
  return useQuery({
    queryKey: WORKER_STATUS_KEY(projectId),
    queryFn: ({ signal }) => fetchWorkerStatus(signal, projectId),
    enabled: enabled && Boolean(projectId),
    staleTime: 2000,
    refetchInterval: 3000,
    refetchIntervalInBackground: false,
  })
}

export function useRefreshWorkerStatus(): () => void {
  const client = useQueryClient()
  return () => {
    // 进程状态全局，切换项目后仍要刷新活动 run；每个项目的队列缓存各自更新。
    void client.invalidateQueries({ queryKey: ['project'] })
  }
}
