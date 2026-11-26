import { useQuery } from '@tanstack/react-query'
import { fetchSharedHubStatus } from './api'
import { projectQueryKey } from '../projects/scope'
import type { HubAssignment, HubSharedStatus, HubWorker } from './types'

/** 只有仍在设备侧等待下载/已完成下载的分配占用门店，与后端唯一索引一致。 */
export const ACTIVE_ASSIGNMENT_STATUSES = new Set(['assigned', 'ready'])

export function useHubSharedStatus(projectId: string) {
  return useQuery({
    queryKey: projectQueryKey(projectId, 'hub-shared-status'),
    queryFn: ({ signal }) => fetchSharedHubStatus(signal, projectId),
    enabled: Boolean(projectId),
    refetchInterval: 5_000,
    staleTime: 4_000,
  })
}

/** 按任务 ID 索引活动分配；重新发布后 current_task_id 可能变化，两者都映射。 */
export function activeAssignmentMap(status: HubSharedStatus | undefined): Map<string, HubAssignment> {
  const map = new Map<string, HubAssignment>()
  for (const record of status?.assignments ?? []) {
    if (!ACTIVE_ASSIGNMENT_STATUSES.has(record.status)) continue
    map.set(record.task_id, record)
    if (record.current_task_id) map.set(record.current_task_id, record)
  }
  return map
}

export function workerNameMap(status: HubSharedStatus | undefined): Map<string, string> {
  return new Map((status?.workers ?? []).map((worker) => [worker.id, worker.name]))
}

export function activeAssignmentsOf(status: HubSharedStatus | undefined, workerId: string): HubAssignment[] {
  return (status?.assignments ?? []).filter(
    (record) => record.worker_id === workerId && ACTIVE_ASSIGNMENT_STATUSES.has(record.status),
  )
}

export function isWorkerActive(worker: HubWorker): boolean {
  return !worker.revoked_at && !worker.archived_at
}
