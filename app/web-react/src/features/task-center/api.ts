import { apiFetch } from '../../api/client'
import { projectApiPath } from '../projects/scope'
import type {
  ActiveTasksResponse,
  HistoryRecord,
  HistoryResponse,
  TaskDetail,
  TaskFilters,
} from './types'

function historyParams(filters: TaskFilters): string {
  const params = new URLSearchParams({
    kind: filters.kind,
    status: filters.status === 'all' ? '' : filters.status,
    q: filters.q.trim(),
    page: String(filters.page),
    page_size: String(filters.pageSize),
    worker_id: filters.workerId,
  })
  return params.toString()
}

export function fetchTaskHistory(filters: TaskFilters, signal?: AbortSignal, projectId?: string): Promise<HistoryResponse> {
  const path = `/api/tasks/history?${historyParams(filters)}`
  return apiFetch<HistoryResponse>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

export function fetchActiveTasks(signal?: AbortSignal, projectId?: string): Promise<ActiveTasksResponse> {
  const path = '/api/tasks/active'
  return apiFetch<ActiveTasksResponse>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

export function fetchTaskDetail(kind: string, id: string, signal?: AbortSignal, projectId?: string): Promise<TaskDetail> {
  const path = `/api/tasks/history/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`
  return apiFetch<TaskDetail>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

export function renameCaptureRun(runId: string, name: string, projectId?: string): Promise<HistoryRecord> {
  const path = `/api/tasks/history/capture/${encodeURIComponent(runId)}`
  return apiFetch<HistoryRecord>(projectId ? projectApiPath(path, projectId) : path, {
    method: 'PATCH',
    json: { name, ...(projectId ? { project_id: projectId } : {}) },
  })
}
