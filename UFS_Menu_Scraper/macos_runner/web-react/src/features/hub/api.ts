import { apiFetch } from '../../api/client'
import type {
  AssignmentPhase,
  AssignmentRecordsResponse,
  AssignResponse,
  HubQueueParams,
  HubSharedStatus,
  HubStatus,
  PairingCode,
  ReclaimResponse,
  ResultFilesResponse,
} from './types'

/**
 * Hub 队列读取与写入适配层。
 * 所有参数名与 Python 路由保持一致；不在前端复制分页、筛选或分配规则。
 */
export function fetchHubStatus(params: HubQueueParams, signal?: AbortSignal): Promise<HubStatus> {
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
  return apiFetch<HubStatus>(`/api/status?${search.toString()}`, { signal })
}

export function fetchSharedHubStatus(signal?: AbortSignal): Promise<HubSharedStatus> {
  return apiFetch<HubSharedStatus>('/api/shared/status?include_history=false', { signal })
}

export function fetchAssignmentRecords(
  filters: { workerId: string; phase: AssignmentPhase; q: string; page: number; size: number },
  signal?: AbortSignal,
): Promise<AssignmentRecordsResponse> {
  const search = new URLSearchParams({
    worker_id: filters.workerId,
    phase: filters.phase,
    q: filters.q.trim(),
    page: String(filters.page),
    size: String(filters.size),
  })
  return apiFetch<AssignmentRecordsResponse>(`/api/shared/assignments?${search.toString()}`, { signal })
}

export function assignTasks(workerId: string, taskIds: string[]): Promise<AssignResponse> {
  return apiFetch<AssignResponse>('/api/shared/assignments', {
    method: 'POST',
    json: { worker_id: workerId, task_ids: taskIds },
  })
}

export function reclaimAssignments(assignmentIds: string[]): Promise<ReclaimResponse> {
  return apiFetch<ReclaimResponse>('/api/shared/assignments/reclaim', {
    method: 'POST',
    json: { assignment_ids: assignmentIds },
  })
}

export function reclaimAssignment(assignmentId: string): Promise<{ id: string; status: string }> {
  return apiFetch<{ id: string; status: string }>(
    `/api/shared/assignments/${encodeURIComponent(assignmentId)}/reclaim`,
    { method: 'POST', json: {} },
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

export function fetchResultFiles(assignmentId: string, signal?: AbortSignal): Promise<ResultFilesResponse> {
  return apiFetch<ResultFilesResponse>(`/api/shared/results/${encodeURIComponent(assignmentId)}/files`, { signal })
}

/** 结果文件名可能含多级路径，逐段编码后拼回。 */
export function resultFileUrl(assignmentId: string, relative: string): string {
  const encoded = relative.split('/').map((segment) => encodeURIComponent(segment)).join('/')
  return `/api/shared/results/${encodeURIComponent(assignmentId)}/files/${encoded}`
}
