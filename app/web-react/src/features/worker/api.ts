import { apiFetch } from '../../api/client'
import { projectApiPath } from '../projects/scope'
import type {
  CaptureMode,
  ManualActionResponse,
  ManualCommandContext,
  ManualControlState,
  StartCaptureResponse,
  WorkerStatus,
} from './types'
import type { WorkerConnectionStatus } from '../../api/types'

/**
 * Windows 采集端接口适配层。
 *
 * Worker 角色的 `/api/status` 不分页（分页只在 Hub 生效），因此这里只拉一次
 * 全量任务，筛选、排序、分页与勾选都在页面内完成，与服务端口径一致。
 */
export function fetchWorkerStatus(signal?: AbortSignal, projectId?: string): Promise<WorkerStatus> {
  const path = '/api/status?include_events=true'
  return apiFetch<WorkerStatus>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

/** 当前 Worker 的共享连接与本地分配汇总，必须绑定所选项目。 */
export function fetchWorkerConnectionStatus(signal?: AbortSignal, projectId?: string): Promise<WorkerConnectionStatus> {
  const path = '/api/shared/status?include_history=false'
  return apiFetch<WorkerConnectionStatus>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

export function startCapture(payload: {
  mode: CaptureMode
  taskIds: string[]
  name: string
  projectId?: string
}): Promise<StartCaptureResponse> {
  const path = payload.projectId ? projectApiPath('/api/capture/start', payload.projectId) : '/api/capture/start'
  return apiFetch<StartCaptureResponse>(path, {
    method: 'POST',
    json: { mode: payload.mode, task_ids: payload.taskIds, name: payload.name, ...(payload.projectId ? { project_id: payload.projectId } : {}) },
  })
}

/** 恢复暂停/阻塞/中断的批次；传 mode=manual 时仅允许 Windows 全自动暂停批次切半自动。 */
export function resumeCapture(mode?: CaptureMode, projectId?: string): Promise<WorkerStatus> {
  const path = projectId ? projectApiPath('/api/resume', projectId) : '/api/resume'
  return apiFetch<WorkerStatus>(path, {
    method: 'POST',
    json: { ...(mode ? { mode } : {}), ...(projectId ? { project_id: projectId } : {}) },
  })
}

/** 暂停：当前门店完成后生效，不会丢弃已完成结果。 */
export function pauseCapture(projectId?: string): Promise<WorkerStatus> {
  const path = projectId ? projectApiPath('/api/stop', projectId) : '/api/stop'
  return apiFetch<WorkerStatus>(path, { method: 'POST', json: projectId ? { project_id: projectId } : {} })
}

/** 终止：结束当前批次，未处理门店保留在队列中可重新开跑。 */
export function terminateCapture(projectId?: string): Promise<WorkerStatus> {
  const path = projectId ? projectApiPath('/api/terminate', projectId) : '/api/terminate'
  return apiFetch<WorkerStatus>(path, { method: 'POST', json: projectId ? { project_id: projectId } : {} })
}

export function retryTask(taskId: string, projectId?: string): Promise<{ ok?: boolean }> {
  const path = `/api/tasks/${encodeURIComponent(taskId)}/retry`
  return apiFetch<{ ok?: boolean }>(projectId ? projectApiPath(path, projectId) : path, {
    method: 'POST',
    json: projectId ? { project_id: projectId } : {},
  })
}

function manualPath(action: string, projectId?: string): string {
  const path = `/api/manual/${action}`
  return projectId ? projectApiPath(path, projectId) : path
}

export function manualCapture(context: ManualCommandContext, projectId?: string): Promise<ManualActionResponse> {
  return apiFetch<ManualActionResponse>(manualPath('capture', projectId), { method: 'POST', json: { context, ...(projectId ? { project_id: projectId } : {}) } })
}

export function manualReject(context: ManualCommandContext, reason: string, detail: string, projectId?: string): Promise<ManualActionResponse> {
  return apiFetch<ManualActionResponse>(manualPath('reject', projectId), {
    method: 'POST',
    json: { context, reason, detail, ...(projectId ? { project_id: projectId } : {}) },
  })
}

export function manualSkip(context: ManualCommandContext, projectId?: string): Promise<ManualActionResponse> {
  return apiFetch<ManualActionResponse>(manualPath('skip', projectId), { method: 'POST', json: { context, ...(projectId ? { project_id: projectId } : {}) } })
}

/** 保存 F9 快捷键复用的不合格原因。 */
export function setManualReason(reason: string, projectId?: string): Promise<{ ok: boolean; reason: string }> {
  return apiFetch<{ ok: boolean; reason: string }>(manualPath('reason', projectId), {
    method: 'POST',
    json: { reason, ...(projectId ? { project_id: projectId } : {}) },
  })
}

export function fetchManualState(signal?: AbortSignal, projectId?: string): Promise<ManualControlState> {
  const path = projectId ? projectApiPath('/api/manual/state', projectId) : '/api/manual/state'
  return apiFetch<ManualControlState>(path, { signal })
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

export function fetchTaskActions(taskId: string, before: number | null, signal?: AbortSignal, projectId?: string): Promise<{ actions: WorkerAction[] }> {
  const cursor = before === null ? '' : `?before=${encodeURIComponent(String(before))}`
  const path = `/api/tasks/${encodeURIComponent(taskId)}/actions${cursor}`
  return apiFetch<{ actions: WorkerAction[] }>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

export function fetchTaskArtifacts(taskId: string, signal?: AbortSignal, projectId?: string): Promise<{ files: string[] }> {
  const path = `/api/artifacts/${encodeURIComponent(taskId)}`
  return apiFetch<{ files: string[] }>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

export function manualCommandContext(state: ManualControlState | undefined): ManualCommandContext | null {
  const manual = state?.manual
  if (!state || !manual?.waiting || manual.decision || !manual.wait_id || !manual.run_id) return null
  return { device_id: state.device_id, run_id: manual.run_id, task_id: manual.task_id, wait_id: manual.wait_id }
}
