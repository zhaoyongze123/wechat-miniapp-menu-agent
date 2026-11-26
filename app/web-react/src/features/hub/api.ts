import { apiFetch } from '../../api/client'
import { projectApiPath } from '../projects/scope'
import type {
  AssignmentPhase,
  AssignmentRecordsResponse,
  AssignResponse,
  HubQueueParams,
  HubSharedStatus,
  HubStatus,
  HubUpdateRollout,
  PairingCode,
  ReclaimResponse,
  ResultFilesResponse,
} from './types'

/**
 * Hub 队列读取与写入适配层。
 * 所有参数名与 Python 路由保持一致；不在前端复制分页、筛选或分配规则。
 */
export function fetchHubStatus(params: HubQueueParams, signal?: AbortSignal, projectId?: string): Promise<HubStatus> {
  const search = new URLSearchParams({
    queue_page: String(params.page),
    queue_size: String(params.size),
    queue_scope: params.scope,
    queue_batch: params.batch,
    queue_q: params.q.trim(),
    queue_filter: params.filter,
    queue_sort: params.sort,
    selected: params.selected.join(','),
    include_events: params.includeEvents ? 'true' : 'false',
  })
  const path = `/api/status?${search.toString()}`
  return apiFetch<HubStatus>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

export function fetchSharedHubStatus(signal: AbortSignal | undefined, projectId: string): Promise<HubSharedStatus> {
  const path = '/api/shared/status?include_history=false'
  return apiFetch<HubSharedStatus>(projectApiPath(path, projectId), { signal })
}

export function fetchAssignmentRecords(
  filters: { workerId: string; phase: AssignmentPhase; q: string; page: number; size: number },
  signal: AbortSignal | undefined,
  projectId: string,
): Promise<AssignmentRecordsResponse> {
  const search = new URLSearchParams({
    worker_id: filters.workerId,
    phase: filters.phase,
    q: filters.q.trim(),
    page: String(filters.page),
    size: String(filters.size),
  })
  const path = `/api/shared/assignments?${search.toString()}`
  return apiFetch<AssignmentRecordsResponse>(projectApiPath(path, projectId), { signal })
}

export function assignTasks(workerId: string, taskIds: string[], projectId: string): Promise<AssignResponse> {
  const path = projectApiPath('/api/shared/assignments', projectId)
  return apiFetch<AssignResponse>(path, {
    method: 'POST',
    json: { worker_id: workerId, task_ids: taskIds, project_id: projectId },
  })
}

export function reclaimAssignments(assignmentIds: string[], projectId: string): Promise<ReclaimResponse> {
  const path = projectApiPath('/api/shared/assignments/reclaim', projectId)
  return apiFetch<ReclaimResponse>(path, {
    method: 'POST',
    json: { assignment_ids: assignmentIds, project_id: projectId },
  })
}

export function reclaimAssignment(assignmentId: string, projectId: string): Promise<{ id: string; status: string }> {
  const path = `/api/shared/assignments/${encodeURIComponent(assignmentId)}/reclaim`
  return apiFetch<{ id: string; status: string }>(
    projectApiPath(path, projectId),
    { method: 'POST', json: { project_id: projectId } },
  )
}

export function createPairingCode(): Promise<PairingCode> {
  return apiFetch<PairingCode>('/api/shared/pairing', { method: 'POST', json: {} })
}

export function editWorker(workerId: string, name: string, description: string): Promise<{ id: string; name: string; description: string }> {
  return apiFetch<{ id: string; name: string; description: string }>(`/api/shared/workers/${encodeURIComponent(workerId)}`, {
    method: 'PATCH',
    json: { name, description },
  })
}

export function revokeWorker(workerId: string): Promise<{ id: string; revoked_at: string }> {
  return apiFetch<{ id: string; revoked_at: string }>(`/api/shared/workers/${encodeURIComponent(workerId)}/revoke`, {
    method: 'POST',
    json: {},
  })
}

export function archiveWorker(workerId: string): Promise<{ id: string; archived_at: string }> {
  return apiFetch<{ id: string; archived_at: string }>(`/api/shared/workers/${encodeURIComponent(workerId)}/archive`, {
    method: 'POST',
    json: {},
  })
}

/** 保存版本推送范围；batch 只对选中设备分发目标版本，安装仍由设备本地确认。 */
export function saveUpdateRollout(mode: 'all' | 'batch', workerIds: string[]): Promise<HubUpdateRollout> {
  return apiFetch<HubUpdateRollout>('/api/shared/update-rollout', {
    method: 'PUT',
    json: { mode, worker_ids: workerIds },
  })
}

export function fetchResultFiles(assignmentId: string, signal: AbortSignal | undefined, projectId: string): Promise<ResultFilesResponse> {
  const path = `/api/shared/results/${encodeURIComponent(assignmentId)}/files`
  return apiFetch<ResultFilesResponse>(projectApiPath(path, projectId), { signal })
}

/** 结果文件名可能含多级路径，逐段编码后拼回。 */
export function resultFileUrl(assignmentId: string, relative: string, projectId: string): string {
  const encoded = relative.split('/').map((segment) => encodeURIComponent(segment)).join('/')
  const path = `/api/shared/results/${encodeURIComponent(assignmentId)}/files/${encoded}`
  return projectApiPath(path, projectId)
}
