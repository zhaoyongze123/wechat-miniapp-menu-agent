import { apiFetch } from '../../api/client'
import type {
  CaptureMode,
  ManualActionResponse,
  ManualState,
  StartCaptureResponse,
  WorkerStatus,
} from './types'

/**
 * Windows 采集端接口适配层。
 *
 * Worker 角色的 `/api/status` 不分页（分页只在 Hub 生效），因此这里只拉一次
 * 全量任务，筛选、排序、分页与勾选都在页面内完成，与服务端口径一致。
 */
export function fetchWorkerStatus(signal?: AbortSignal): Promise<WorkerStatus> {
  return apiFetch<WorkerStatus>('/api/status?include_events=true', { signal })
}

export function startCapture(payload: {
  mode: CaptureMode
  taskIds: string[]
  name: string
}): Promise<StartCaptureResponse> {
  return apiFetch<StartCaptureResponse>('/api/capture/start', {
    method: 'POST',
    json: { mode: payload.mode, task_ids: payload.taskIds, name: payload.name },
  })
}

/** 恢复暂停/阻塞/中断的批次；传 mode=manual 时仅允许 Windows 全自动暂停批次切半自动。 */
export function resumeCapture(mode?: CaptureMode): Promise<WorkerStatus> {
  return apiFetch<WorkerStatus>('/api/resume', {
    method: 'POST',
    json: mode ? { mode } : {},
  })
}

/** 暂停：当前门店完成后生效，不会丢弃已完成结果。 */
export function pauseCapture(): Promise<WorkerStatus> {
  return apiFetch<WorkerStatus>('/api/stop', { method: 'POST', json: {} })
}

/** 终止：结束当前批次，未处理门店保留在队列中可重新开跑。 */
export function terminateCapture(): Promise<WorkerStatus> {
  return apiFetch<WorkerStatus>('/api/terminate', { method: 'POST', json: {} })
}

export function retryTask(taskId: string): Promise<{ ok?: boolean }> {
  return apiFetch<{ ok?: boolean }>(`/api/tasks/${encodeURIComponent(taskId)}/retry`, {
    method: 'POST',
    json: {},
  })
}

export function manualCapture(): Promise<ManualActionResponse> {
  return apiFetch<ManualActionResponse>('/api/manual/capture', { method: 'POST', json: {} })
}

export function manualReject(reason: string, detail: string): Promise<ManualActionResponse> {
  return apiFetch<ManualActionResponse>('/api/manual/reject', {
    method: 'POST',
    json: { reason, detail },
  })
}

export function manualSkip(): Promise<ManualActionResponse> {
  return apiFetch<ManualActionResponse>('/api/manual/skip', { method: 'POST', json: {} })
}

/** 保存 F9 快捷键复用的不合格原因。 */
export function setManualReason(reason: string): Promise<{ ok: boolean; reason: string }> {
  return apiFetch<{ ok: boolean; reason: string }>('/api/manual/reason', {
    method: 'POST',
    json: { reason },
  })
}

export function fetchManualState(signal?: AbortSignal): Promise<{ manual: ManualState | null }> {
  return apiFetch<{ manual: ManualState | null }>('/api/manual/state', { signal })
}

export interface WorkerAction {
  id: number
  run_id: string
  task_id: string
  attempt_id: string
  stage: string
  step: number | null
  action: string
  started_at: string
  elapsed_ms: number | null
  result: string
  details: Record<string, unknown>
  debug_state?: string
}

export function fetchTaskActions(taskId: string, before: number | null, signal?: AbortSignal): Promise<{ actions: WorkerAction[] }> {
  const cursor = before === null ? '' : `?before=${encodeURIComponent(String(before))}`
  return apiFetch<{ actions: WorkerAction[] }>(`/api/tasks/${encodeURIComponent(taskId)}/actions${cursor}`, { signal })
}

export function fetchTaskArtifacts(taskId: string, signal?: AbortSignal): Promise<{ files: string[] }> {
  return apiFetch<{ files: string[] }>(`/api/artifacts/${encodeURIComponent(taskId)}`, { signal })
}
