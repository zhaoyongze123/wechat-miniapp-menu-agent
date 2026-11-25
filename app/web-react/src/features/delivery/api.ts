/** 合格成品目录接口适配：状态、门店分页、手动触发与打开目录。 */

import { apiFetch } from '../../api/client'
import type { DeliveryStatus, DeliveryStoresResponse } from './types'

export function fetchDeliveryStatus(signal?: AbortSignal): Promise<DeliveryStatus> {
  return apiFetch<DeliveryStatus>('/api/delivery/status', { signal })
}

export function fetchDeliveryStores(
  params: { page: number; size: number; q: string; batchId: string },
  signal?: AbortSignal,
): Promise<DeliveryStoresResponse> {
  const search = new URLSearchParams({
    page: String(params.page),
    size: String(params.size),
    q: params.q.trim(),
    batch_id: params.batchId,
  })
  return apiFetch<DeliveryStoresResponse>(`/api/delivery/stores?${search.toString()}`, { signal })
}

export function triggerDeliverySync(): Promise<DeliveryStatus> {
  return apiFetch<DeliveryStatus>('/api/delivery/sync', { method: 'POST' })
}

/** 仅在服务端确认可打开时调用；容器环境会返回 409 与真实原因。 */
export function revealDelivery(): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>('/api/delivery/reveal', { method: 'POST' })
}
