/** 质量审核接口适配：参数名与 Python 路由一致，不在前端复制筛选与状态规则。 */

import { ApiError, apiFetch } from '../../api/client'
import { REJECTION_REASONS } from '../materials/format'
import type {
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

export function fetchRejections(query: RejectionQuery, signal?: AbortSignal): Promise<RejectionsResponse> {
  return apiFetch<RejectionsResponse>(`${BASE}?${rejectionSearch(query).toString()}`, { signal })
}

/** 导出当前服务端筛选结果，含人工确认状态筛选。 */
export async function downloadRejections(query: RejectionQuery): Promise<void> {
  const response = await fetch(`${BASE}/export.xlsx?${rejectionSearch(query).toString()}`, { cache: 'no-store' })
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

export function markRejectionReviewed(
  payload: RejectionReviewPayload,
): Promise<{ ok: boolean; reviewed_at: string }> {
  return apiFetch(`${BASE}/review`, { method: 'POST', json: payload })
}

export function undoCaptureRejection(taskId: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/rejection/undo`, { method: 'POST', json: {} })
}

export function undoMaterialRejection(batchId: string, storeId: string): Promise<unknown> {
  return apiFetch(
    `${BASE}/materials/${encodeURIComponent(batchId)}/${encodeURIComponent(storeId)}/undo`,
    { method: 'POST', json: {} },
  )
}

export function undoRejectionsBatch(items: import('./types').RejectionUndoItem[]) {
  return apiFetch<import('./types').RejectionUndoResponse>(`${BASE}/undo-batch`, {
    method: 'POST',
    json: { items },
  })
}

/** 打回素材清洗：清除不合格、素材决策回待处理、采集任务置为已阻塞。 */
export function returnRejectionToMaterials(
  taskId: string,
): Promise<{ ok: boolean; batch_id: string; batch_name: string; store_id: string }> {
  return apiFetch(`${BASE}/tasks/${encodeURIComponent(taskId)}/return-to-materials`, {
    method: 'POST',
    json: {},
  })
}

export function retryCaptureTask(taskId: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/retry`, { method: 'POST', json: {} })
}

/** 人工把待检查门店标记为不合格；只记录结论，不自动重新采集。 */
export function rejectPendingTask(taskId: string, reason: string, detail: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/reject`, {
    method: 'POST',
    json: { reason, detail },
  })
}

export function captureEvidenceUrl(taskId: string): string {
  return `${BASE}/tasks/${encodeURIComponent(taskId)}/evidence`
}

export function taskSourceUrl(taskId: string): string {
  return `/api/tasks/${encodeURIComponent(taskId)}/source`
}

export function taskLastFrameUrl(taskId: string): string {
  return `/api/tasks/${encodeURIComponent(taskId)}/last-frame`
}

export function fetchPendingReviews(
  batchIds: string[],
  query: string,
  signal?: AbortSignal,
): Promise<PendingReviewsResponse> {
  const search = new URLSearchParams({
    batch_ids: batchIds.length ? batchIds.join(',') : 'all',
    q: query.trim(),
  })
  return apiFetch<PendingReviewsResponse>(`/api/reviews/pending?${search.toString()}`, { signal })
}

export function approveBatches(batchIds: string[]): Promise<{ ok: boolean; batches: unknown[] }> {
  return apiFetch('/api/reviews/approve', { method: 'POST', json: { batch_ids: batchIds } })
}

/** 撤销通过按批次逐个提交，与后端单批次入口一致；任一失败立即抛出并保留已完成结果。 */
export async function unapproveBatches(batchIds: string[]): Promise<void> {
  for (const batchId of batchIds) {
    await apiFetch(`/api/materials/batches/${encodeURIComponent(batchId)}/approve/undo`, {
      method: 'POST',
      json: {},
    })
  }
}

/** 不合格原因选项与素材清洗共用同一码表，避免两处文案漂移。 */
export const REASON_OPTIONS = [
  { value: 'all', label: '全部原因' },
  ...REJECTION_REASONS.map((item) => ({ value: item.code, label: item.label })),
]

/** Hub 是否正在执行采集；运行中禁用需要空闲状态的不合格处置。 */
export function fetchHubRunning(signal?: AbortSignal): Promise<boolean> {
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
  return apiFetch<{ running?: boolean }>(`/api/status?${search.toString()}`, { signal }).then(
    (status) => status.running === true,
  )
}
