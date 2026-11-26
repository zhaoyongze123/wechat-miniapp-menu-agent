import { apiFetch } from '../../api/client'
import { projectApiPath } from '../projects/scope'
import type {
  AnnotationClickPayload,
  AnnotationHistory,
  AnnotationState,
  AnnotationTask,
} from './types'

/** 标注会话状态；轮询由页面按忙闲控制，避免操作过程中被覆盖。 */
export function fetchAnnotationState(signal?: AbortSignal, projectId?: string): Promise<AnnotationState> {
  const path = '/api/annotation/state'
  return apiFetch<AnnotationState>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

/** 标注排队复用本机任务列表，只保留展示需要的字段。 */
export async function fetchAnnotationTasks(signal?: AbortSignal, projectId?: string): Promise<AnnotationTask[]> {
  const path = '/api/status?include_events=false'
  const data = await apiFetch<{ tasks?: AnnotationTask[] }>(projectId ? projectApiPath(path, projectId) : path, { signal })
  return Array.isArray(data.tasks) ? data.tasks : []
}

/** 已完成的标注记录，用于“查看效果”回看。 */
export function fetchAnnotationHistory(taskId: string, signal?: AbortSignal, projectId?: string): Promise<AnnotationHistory> {
  const path = `/api/annotation/history?task_id=${encodeURIComponent(taskId)}`
  return apiFetch<AnnotationHistory>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

function post(action: string, json: Record<string, unknown> = {}, projectId?: string): Promise<AnnotationState> {
  const path = `/api/annotation/${action}`
  return apiFetch<AnnotationState>(projectId ? projectApiPath(path, projectId) : path, {
    method: 'POST', json: { ...json, ...(projectId ? { project_id: projectId } : {}) },
  })
}

export function startAnnotation(taskIds: string[], projectId?: string): Promise<AnnotationState> {
  return post('start', { task_ids: taskIds }, projectId)
}

export function pauseAnnotation(projectId?: string): Promise<AnnotationState> {
  return post('pause', {}, projectId)
}

export function resumeAnnotation(projectId?: string): Promise<AnnotationState> {
  return post('resume', {}, projectId)
}

export function decideAnnotation(projectId?: string): Promise<AnnotationState> {
  return post('decide', {}, projectId)
}

export function clickAnnotation(payload: AnnotationClickPayload, projectId?: string): Promise<AnnotationState> {
  return post('click', payload as unknown as Record<string, unknown>, projectId)
}

export function stepAnnotation(delta: number, projectId?: string): Promise<AnnotationState> {
  return post('step', { delta }, projectId)
}

export function completeAnnotation(projectId?: string): Promise<AnnotationState> {
  return post('complete', {}, projectId)
}

export function skipAnnotation(reason: string, projectId?: string): Promise<AnnotationState> {
  return post('skip', { reason }, projectId)
}

export function rejectAnnotation(reason: string, detail: string, projectId?: string): Promise<AnnotationState> {
  return post('reject', { reason, detail }, projectId)
}

export function retryAnnotation(projectId?: string): Promise<AnnotationState> {
  return post('retry', {}, projectId)
}

export function refreshAnnotation(projectId?: string): Promise<AnnotationState> {
  return post('refresh', {}, projectId)
}

export function stopAnnotation(projectId?: string): Promise<AnnotationState> {
  return post('stop', {}, projectId)
}
