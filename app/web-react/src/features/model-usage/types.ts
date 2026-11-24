/** 模型用量接口结构：对应 menu_capture/model_usage.py 的 query() 返回。 */

export type UsageGroupBy = 'day' | 'run' | 'task' | 'model'
export type UsageSource = 'all' | 'capture' | 'annotation' | 'connection_test' | 'material_analysis' | 'agent' | 'other'

export interface UsageAggregate {
  calls: number
  success_calls: number
  error_calls: number
  running_calls: number
  interrupted_calls: number
  known_usage_calls: number
  partial_usage_calls: number
  unknown_usage_calls: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  cached_tokens: number
  cache_write_tokens: number
  reasoning_tokens: number
  image_tokens: number
  elapsed_ms: number
  input_tokens_known_calls?: number
  output_tokens_known_calls?: number
  total_tokens_known_calls?: number
  cached_tokens_known_calls?: number
  cache_write_tokens_known_calls?: number
  reasoning_tokens_known_calls?: number
  image_tokens_known_calls?: number
}

export interface UsageGroup extends UsageAggregate {
  key: string
  label?: string
  run_id?: string
  task_id?: string
  source_name?: string
  provider?: string
  model?: string
}

export interface UsageCall {
  call_id: string
  started_at: string
  local_day: string
  provider: string
  model: string
  endpoint: string
  status: string
  finished_at?: string | null
  returned_model?: string
  elapsed_ms?: number | null
  http_status?: number | null
  request_id?: string
  error_type?: string
  usage_state: string
  input_tokens?: number | null
  output_tokens?: number | null
  total_tokens?: number | null
  cached_tokens?: number | null
  cache_write_tokens?: number | null
  reasoning_tokens?: number | null
  image_tokens?: number | null
  run_id?: string
  task_id?: string
  source?: string
  source_name?: string
  attempt_id?: string
  action_id?: string | null
  stage?: string
  step?: string | null
  material_batch_id?: string
  material_version?: string
}

export interface UsageRun {
  run_id: string
  label?: string
  created_at?: string
}

export interface UsageModelOption {
  provider: string
  model: string
}

export interface UsageFilters {
  runs: UsageRun[]
  models: UsageModelOption[]
}

export interface UsagePagination {
  page: number
  page_size: number
  total: number
  pages: number
}

export interface UsageResponse {
  summary: UsageAggregate
  groups: UsageGroup[]
  groups_total: number
  calls: UsageCall[]
  pagination: UsagePagination
  filters: UsageFilters
  timezone: string
}

export interface UsageQuery {
  dateFrom: string
  dateTo: string
  groupBy: UsageGroupBy
  source: UsageSource
  runId: string
  taskId: string
  page: number
  pageSize: number
}
