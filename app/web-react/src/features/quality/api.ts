/** 质量审核接口适配：参数名与 Python 路由一致，不在前端复制筛选与状态规则。 */

import { ApiError, apiFetch } from '../../api/client'
import { projectApiPath } from '../projects/scope'
import { REJECTION_REASONS } from '../materials/format'
import type {
  ExportPreviewResponse,
  PendingReviewsResponse,
  RejectionQuery,
  RejectionReviewPayload,
  RejectionsResponse,
} from './types'

const BASE = '/api/rejections'

function rejectionSearch(query: RejectionQuery): URLSearchParams {
  return new URLSearchParams({
    batch_id: query.batchId,
    source: query.source,
    reason: query.reason,
    q: query.q.trim(),
    reviewed: query.reviewed,
  })
}

export function fetchRejections(query: RejectionQuery, signal?: AbortSignal, projectId?: string): Promise<RejectionsResponse> {
  const path = `${BASE}?${rejectionSearch(query).toString()}`
  return apiFetch<RejectionsResponse>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

/** 预览当前服务端筛选下的不合格门店导出表格，与 export.xlsx 同源。 */
export function fetchRejectionsExportPreview(
  query: RejectionQuery,
  signal?: AbortSignal,
  projectId?: string,
): Promise<ExportPreviewResponse> {
  const path = `${BASE}/export-preview?${rejectionSearch(query).toString()}`
  return apiFetch<ExportPreviewResponse>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

/** 预览全量门店名称、采集状态与原因表格，与 export-store-names.xlsx 同源。 */
export function fetchStoreNamesExportPreview(
  signal?: AbortSignal,
  projectId?: string,
): Promise<ExportPreviewResponse> {
  const path = `${BASE}/export-store-names/preview`
  return apiFetch<ExportPreviewResponse>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

/** 导出当前服务端筛选结果，含人工确认状态筛选。 */
export async function downloadRejections(query: RejectionQuery, projectId?: string): Promise<void> {
  const path = `${BASE}/export.xlsx?${rejectionSearch(query).toString()}`
  const response = await fetch(projectId ? projectApiPath(path, projectId) : path, { cache: 'no-store' })
  if (!response.ok) {
    let detail = `导出失败（${response.status}）`
    try {
      const body: unknown = await response.json()
      if (typeof body === 'object' && body !== null) {
        const value = (body as { detail?: unknown }).detail
        if (typeof value === 'string' && value) detail = value
      }
    } catch {
      // 非 JSON 错误响应：保留状态码提示。
    }
    throw new ApiError(detail, response.status)
  }
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  try {
    const link = document.createElement('a')
    link.href = url
    link.download = '不合格门店清单.xlsx'
    document.body.append(link)
    link.click()
    link.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

/** 导出全量门店名称与采集状态：名称提取独立于采集结果，覆盖成功、待检查与不合格。 */
export async function downloadStoreNames(projectId?: string): Promise<void> {
  const path = `${BASE}/export-store-names.xlsx`
  const response = await fetch(projectId ? projectApiPath(path, projectId) : path, { cache: 'no-store' })
  if (!response.ok) {
    let detail = `导出失败（${response.status}）`
    try {
      const body: unknown = await response.json()
      if (typeof body === 'object' && body !== null) {
        const value = (body as { detail?: unknown }).detail
        if (typeof value === 'string' && value) detail = value
      }
    } catch {
      // 非 JSON 错误响应：保留状态码提示。
    }
    throw new ApiError(detail, response.status)
  }
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  try {
    const link = document.createElement('a')
    link.href = url
    link.download = '全量门店名称.xlsx'
    document.body.append(link)
    link.click()
    link.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

export function markRejectionReviewed(
  payload: RejectionReviewPayload,
  projectId?: string,
): Promise<{ ok: boolean; reviewed_at: string }> {
  return apiFetch(projectId ? projectApiPath(`${BASE}/review`, projectId) : `${BASE}/review`, { method: 'POST', json: { ...payload, ...(projectId ? { project_id: projectId } : {}) } })
}

export function undoCaptureRejection(taskId: string, projectId?: string): Promise<{ ok: boolean }> {
  const path = `/api/tasks/${encodeURIComponent(taskId)}/rejection/undo`
  return apiFetch(projectId ? projectApiPath(path, projectId) : path, { method: 'POST', json: projectId ? { project_id: projectId } : {} })
}

export function undoMaterialRejection(batchId: string, storeId: string, projectId?: string): Promise<unknown> {
  const path = `${BASE}/materials/${encodeURIComponent(batchId)}/${encodeURIComponent(storeId)}/undo`
  return apiFetch(
    projectId ? projectApiPath(path, projectId) : path,
    { method: 'POST', json: projectId ? { project_id: projectId } : {} },
  )
}

export function undoRejectionsBatch(items: import('./types').RejectionUndoItem[], projectId?: string) {
  const path = `${BASE}/undo-batch`
  return apiFetch<import('./types').RejectionUndoResponse>(projectId ? projectApiPath(path, projectId) : path, {
    method: 'POST',
    json: { items, ...(projectId ? { project_id: projectId } : {}) },
  })
}

/** 打回素材清洗：清除不合格、素材决策回待处理、采集任务置为已阻塞。 */
export function returnRejectionToMaterials(
  taskId: string,
  projectId?: string,
): Promise<{ ok: boolean; batch_id: string; batch_name: string; store_id: string }> {
  const path = `${BASE}/tasks/${encodeURIComponent(taskId)}/return-to-materials`
  return apiFetch(projectId ? projectApiPath(path, projectId) : path, {
    method: 'POST',
    json: projectId ? { project_id: projectId } : {},
  })
}

export function retryCaptureTask(taskId: string, projectId?: string): Promise<{ ok: boolean }> {
  const path = `/api/tasks/${encodeURIComponent(taskId)}/retry`
  return apiFetch(projectId ? projectApiPath(path, projectId) : path, { method: 'POST', json: projectId ? { project_id: projectId } : {} })
}

/** 人工把待检查门店标记为不合格；只记录结论，不自动重新采集。 */
export function rejectPendingTask(taskId: string, reason: string, detail: string, projectId?: string): Promise<{ ok: boolean }> {
  const path = `/api/tasks/${encodeURIComponent(taskId)}/reject`
  return apiFetch(projectId ? projectApiPath(path, projectId) : path, {
    method: 'POST',
    json: { reason, detail, ...(projectId ? { project_id: projectId } : {}) },
  })
}

export function captureEvidenceUrl(taskId: string, projectId?: string): string {
  const path = `${BASE}/tasks/${encodeURIComponent(taskId)}/evidence`
  return projectId ? projectApiPath(path, projectId) : path
}

export function taskSourceUrl(taskId: string, projectId?: string): string {
  const path = `/api/tasks/${encodeURIComponent(taskId)}/source`
  return projectId ? projectApiPath(path, projectId) : path
}

export function taskLastFrameUrl(taskId: string, projectId?: string): string {
  const path = `/api/tasks/${encodeURIComponent(taskId)}/last-frame`
  return projectId ? projectApiPath(path, projectId) : path
}

export function fetchPendingReviews(
  batchIds: string[],
  query: string,
  signal?: AbortSignal,
  projectId?: string,
): Promise<PendingReviewsResponse> {
  const search = new URLSearchParams({
    batch_ids: batchIds.length ? batchIds.join(',') : 'all',
    q: query.trim(),
  })
  const path = `/api/reviews/pending?${search.toString()}`
  return apiFetch<PendingReviewsResponse>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

export function approveBatches(batchIds: string[], projectId?: string): Promise<{ ok: boolean; batches: unknown[] }> {
  const path = '/api/reviews/approve'
  return apiFetch(projectId ? projectApiPath(path, projectId) : path, { method: 'POST', json: { batch_ids: batchIds, ...(projectId ? { project_id: projectId } : {}) } })
}

/** 撤销通过按批次逐个提交，与后端单批次入口一致；任一失败立即抛出并保留已完成结果。 */
export async function unapproveBatches(batchIds: string[], projectId?: string): Promise<void> {
  for (const batchId of batchIds) {
    const path = `/api/materials/batches/${encodeURIComponent(batchId)}/approve/undo`
    await apiFetch(projectId ? projectApiPath(path, projectId) : path, {
      method: 'POST',
      json: projectId ? { project_id: projectId } : {},
    })
  }
}

/** 不合格原因选项与素材清洗共用同一码表，避免两处文案漂移。 */
export const REASON_OPTIONS = [
  { value: 'all', label: '全部原因' },
  ...REJECTION_REASONS.map((item) => ({ value: item.code, label: item.label })),
]

/** Hub 是否正在执行采集；运行中禁用需要空闲状态的不合格处置。 */
export function fetchHubRunning(signal?: AbortSignal, projectId?: string): Promise<boolean> {
  const search = new URLSearchParams({
    include_events: 'false',
    queue_page: '1',
    queue_size: '1',
    queue_scope: 'all',
    queue_batch: 'all',
    queue_q: '',
    queue_filter: 'all',
    queue_sort: 'source',
    selected: '',
  })
  const path = `/api/status?${search.toString()}`
  return apiFetch<{ running?: boolean }>(projectId ? projectApiPath(path, projectId) : path, { signal }).then(
    (status) => status.running === true,
  )
}
