import { apiFetch } from '../../api/client'
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

export function fetchTaskHistory(filters: TaskFilters, signal?: AbortSignal): Promise<HistoryResponse> {
  return apiFetch<HistoryResponse>(`/api/tasks/history?${historyParams(filters)}`, { signal })
}

export function fetchActiveTasks(signal?: AbortSignal): Promise<ActiveTasksResponse> {
  return apiFetch<ActiveTasksResponse>('/api/tasks/active', { signal })
}

export function fetchTaskDetail(kind: string, id: string, signal?: AbortSignal): Promise<TaskDetail> {
  return apiFetch<TaskDetail>(`/api/tasks/history/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`, { signal })
}

export function renameCaptureRun(runId: string, name: string): Promise<HistoryRecord> {
  return apiFetch<HistoryRecord>(`/api/tasks/history/capture/${encodeURIComponent(runId)}`, {
    method: 'PATCH',
    json: { name },
  })
}
