import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchWorkerStatus } from './api'
import { useProject } from '../projects/ProjectProvider'
import { projectQueryKey } from '../projects/scope'
import type { WorkerQueueParams } from './types'

export const WORKER_STATUS_KEY = (projectId: string, params?: WorkerQueueParams) =>
  params
    ? projectQueryKey(
        projectId,
        'worker-status',
        params.page,
        params.size,
        params.batch,
        params.q,
        params.filter,
        params.sort,
        params.selected.join(','),
        params.includeEvents,
      )
    : projectQueryKey(projectId, 'worker-status')

/** 服务端分页轮询：运行中 3 秒刷新，空闲时按 5 秒轻量刷新，避免全量任务传输。 */
export function useWorkerStatus(params: WorkerQueueParams, enabled: boolean) {
  const { projectId } = useProject()
  return useQuery({
    queryKey: WORKER_STATUS_KEY(projectId, params),
    queryFn: ({ signal }) => fetchWorkerStatus(params, signal, projectId),
    placeholderData: (previous, previousQuery) => previousQuery?.queryKey[1] === projectId ? previous : undefined,
    enabled: enabled && Boolean(projectId),
    staleTime: 2000,
    refetchInterval: (query) => (query.state.data?.running ? 3000 : 5000),
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
