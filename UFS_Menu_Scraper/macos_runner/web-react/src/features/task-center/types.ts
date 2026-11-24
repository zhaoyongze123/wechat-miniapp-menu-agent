export type TaskKind = 'capture' | 'materials'
export type TaskKindFilter = 'all' | TaskKind

export interface BatchSummary {
  id: string
  name: string
  versions: string[]
  stores: number
}

export interface TaskCounts {
  total?: number
  done?: number
  attention?: number
  rejected?: number
  recorded?: number
  processed?: number
  unknown?: number
  failed?: number
}

export interface HistoryRecord {
  kind: TaskKind
  id: string
  title?: string
  name?: string
  batch_id?: string
  batch_name?: string
  batches?: BatchSummary[]
  mode?: string
  status: string
  created_at?: string
  updated_at?: string
  message?: string
  counts?: TaskCounts
  worker_id?: string
  worker_name?: string
  synced_at?: string
  worker_last_seen?: string
  history_status?: 'synced' | 'partial'
  local_run_id?: string
  can_rename?: boolean
}

export interface HistorySummary {
  total?: number
  running?: number
  completed?: number
  failed?: number
  terminated?: number
}

export interface HistoryResponse {
  records: HistoryRecord[]
  summary: HistorySummary
  page: number
  page_size: number
  page_count: number
}

export interface CaptureTaskEntry {
  task_id: string
  store_name?: string
  source_name?: string
  batch_name?: string
  version?: string
  status?: string
  label?: string
  message?: string
  reject_reason?: string
}

export interface MaterialEvent {
  time?: string
  level?: string
  message?: string
}

export interface CaptureDetail extends HistoryRecord {
  kind: 'capture'
  tasks: CaptureTaskEntry[]
}

export interface MaterialsDetail extends HistoryRecord {
  kind: 'materials'
  events: MaterialEvent[]
}

export type TaskDetail = CaptureDetail | MaterialsDetail

export interface ActiveTask {
  source: 'materials' | 'capture'
  id: string
  title: string
  kind: string
  status: string
  progress: { done: number; total: number }
  extra: Record<string, unknown>
  created_at?: string
  updated_at?: string
}

export interface ActiveTasksResponse {
  tasks: ActiveTask[]
}

export interface TaskFilters {
  kind: TaskKindFilter
  status: string
  q: string
  page: number
  pageSize: number
}
