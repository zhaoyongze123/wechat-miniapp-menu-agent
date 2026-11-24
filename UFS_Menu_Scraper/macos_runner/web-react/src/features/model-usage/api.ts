import { apiFetch } from '../../api/client'
import type { UsageQuery, UsageResponse } from './types'

/** 模型用量只读查询；日期按服务端本地时区口径过滤。 */
export function fetchModelUsage(query: UsageQuery, signal?: AbortSignal): Promise<UsageResponse> {
  const params = new URLSearchParams({
    date_from: query.dateFrom,
    date_to: query.dateTo,
    group_by: query.groupBy,
    source: query.source,
    page: String(query.page),
    page_size: String(query.pageSize),
  })
  if (query.runId) params.set('run_id', query.runId)
  if (query.taskId) params.set('task_id', query.taskId)
  return apiFetch<UsageResponse>(`/api/model-usage?${params.toString()}`, { signal })
}
