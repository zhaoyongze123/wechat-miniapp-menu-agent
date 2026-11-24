import { apiFetch } from '../../api/client'
import type {
  AnnotationClickPayload,
  AnnotationHistory,
  AnnotationState,
  AnnotationTask,
} from './types'

/** 标注会话状态；轮询由页面按忙闲控制，避免操作过程中被覆盖。 */
export function fetchAnnotationState(signal?: AbortSignal): Promise<AnnotationState> {
  return apiFetch<AnnotationState>('/api/annotation/state', { signal })
}

/** 标注排队复用本机任务列表，只保留展示需要的字段。 */
export async function fetchAnnotationTasks(signal?: AbortSignal): Promise<AnnotationTask[]> {
  const data = await apiFetch<{ tasks?: AnnotationTask[] }>('/api/status?include_events=false', { signal })
  return Array.isArray(data.tasks) ? data.tasks : []
}

/** 已完成的标注记录，用于“查看效果”回看。 */
export function fetchAnnotationHistory(taskId: string, signal?: AbortSignal): Promise<AnnotationHistory> {
  return apiFetch<AnnotationHistory>(`/api/annotation/history?task_id=${encodeURIComponent(taskId)}`, { signal })
}

function post(action: string, json: Record<string, unknown> = {}): Promise<AnnotationState> {
  return apiFetch<AnnotationState>(`/api/annotation/${action}`, { method: 'POST', json })
}

export function startAnnotation(taskIds: string[]): Promise<AnnotationState> {
  return post('start', { task_ids: taskIds })
}

export function pauseAnnotation(): Promise<AnnotationState> {
  return post('pause')
}

export function resumeAnnotation(): Promise<AnnotationState> {
  return post('resume')
}

export function decideAnnotation(): Promise<AnnotationState> {
  return post('decide')
}

export function clickAnnotation(payload: AnnotationClickPayload): Promise<AnnotationState> {
  return post('click', payload as unknown as Record<string, unknown>)
}

export function stepAnnotation(delta: number): Promise<AnnotationState> {
  return post('step', { delta })
}

export function completeAnnotation(): Promise<AnnotationState> {
  return post('complete')
}

export function skipAnnotation(reason: string): Promise<AnnotationState> {
  return post('skip', { reason })
}

export function rejectAnnotation(reason: string, detail: string): Promise<AnnotationState> {
  return post('reject', { reason, detail })
}

export function retryAnnotation(): Promise<AnnotationState> {
  return post('retry')
}

export function refreshAnnotation(): Promise<AnnotationState> {
  return post('refresh')
}

export function stopAnnotation(): Promise<AnnotationState> {
  return post('stop')
}
