/** 合格成品目录接口适配：状态、门店分页、手动触发与打开目录。 */

import { apiFetch } from '../../api/client'
import { projectApiPath } from '../projects/scope'
import type { DeliveryStatus, DeliveryStoresResponse } from './types'

export function fetchDeliveryStatus(signal?: AbortSignal, projectId?: string): Promise<DeliveryStatus> {
  const path = '/api/delivery/status'
  return apiFetch<DeliveryStatus>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

export function fetchDeliveryStores(
  params: { page: number; size: number; q: string; batchId: string },
  signal?: AbortSignal,
  projectId?: string,
): Promise<DeliveryStoresResponse> {
  const search = new URLSearchParams({
    page: String(params.page),
    size: String(params.size),
    q: params.q.trim(),
    batch_id: params.batchId,
  })
  const path = `/api/delivery/stores?${search.toString()}`
  return apiFetch<DeliveryStoresResponse>(projectId ? projectApiPath(path, projectId) : path, { signal })
}

export function triggerDeliverySync(projectId?: string): Promise<DeliveryStatus> {
  const path = '/api/delivery/sync'
  return apiFetch<DeliveryStatus>(projectId ? projectApiPath(path, projectId) : path, { method: 'POST', json: projectId ? { project_id: projectId } : {} })
}

/** 仅在服务端确认可打开时调用；容器环境会返回 409 与真实原因。 */
export function revealDelivery(projectId?: string): Promise<{ ok: boolean }> {
  const path = '/api/delivery/reveal'
  return apiFetch<{ ok: boolean }>(projectId ? projectApiPath(path, projectId) : path, { method: 'POST', json: projectId ? { project_id: projectId } : {} })
}
