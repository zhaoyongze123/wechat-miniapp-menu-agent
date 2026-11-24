/** Hub 工作台、设备与分配记录的 DTO；字段缺失显示未知，不推断事实。 */

export type HubQueueScope = 'all' | 'current'
export type HubQueueFilter = 'all' | 'pending' | 'running' | 'captured' | 'rejected' | 'review' | 'returned'
export type HubQueueSort = 'source' | 'recent' | 'status'
export type AssignmentPhase = '' | 'assigned' | 'ready' | 'completed' | 'reclaimed' | 'history'

export interface HubTask {
  id: string
  source_name: string
  source_path?: string
  status: string
  store_state?: string
  reject_reason?: string
  reject_label?: string
  runnable?: boolean
  batch_id?: string
  batch_name?: string
  version?: string
  snapshot_id?: string
  updated_at?: string
  error_code?: string
  label?: string
  message?: string
  group?: string
}

export interface HubSelectedTask {
  id: string
  source_name?: string
  status?: string
  reject_reason?: string
  runnable?: boolean
  batch_name?: string
  version?: string
}

export interface HubQueue {
  selected: HubSelectedTask[]
  page: number
  pages: number
  total: number
  batches?: { id: string; name: string }[]
}

export interface MaterialRelease {
  batch_id?: string
  batch_name?: string
  version?: string
  snapshot_id?: string
  store_count?: number
  workflow?: string
}

export interface HubRun {
  id: string
  status: string
  mode?: string
  name?: string
  total?: number
  processed?: number
  task_ids?: string[]
}

/** 运行事件：来自后端 events 表，仅调试视图展示。 */
export interface HubEvent {
  time?: string
  level?: string
  message?: string
  task_id?: string
}

export interface HubStatus {
  execution_engine?: string
  launch_mode?: string
  engine_ready?: boolean | null
  engine_message?: string
  running?: boolean
  run?: HubRun | null
  tasks?: HubTask[]
  counts?: Record<string, number>
  queue?: HubQueue
  events?: HubEvent[]
  material_release?: MaterialRelease | null
  material_rejected?: number
  material_rejected_all?: number
}

export interface HubWorkerRun {
  status?: string
  mode?: string
  processed?: number
  total?: number
  reported_at?: string
}

/** 采集端在同步时上报的软件状态；未上报为 null，不用空对象冒充最新。 */
export interface HubWorkerSoftware {
  current_version: string
  platform: string
  latest_version: string
  update_available: boolean
  channel: string
  checked_at: string
  download_status: string
  install_status: string
  reported_at: string
}

export interface HubWorker {
  id: string
  name: string
  last_seen?: string
  pending_uploads?: number
  description?: string
  paired_at?: string
  revoked_at?: string
  archived_at?: string
  last_run?: HubWorkerRun | null
  software?: HubWorkerSoftware | null
}

export interface HubAssignment {
  id: string
  task_id: string
  worker_id: string
  worker_name?: string
  batch_id: string
  batch_name: string
  store: string
  revision: string
  snapshot_id?: string
  status: string
  created_at: string
  disposition?: string
  result_message?: string
  current_task_id?: string
}

export interface HubUpdateSummary {
  reported: number
  unreported: number
  update_available: number
  target_version: string
  versions: Record<string, number>
}

export interface HubSharedStatus {
  role: 'hub'
  workers: HubWorker[]
  assignments: HubAssignment[]
  update_summary?: HubUpdateSummary
  update_target?: { version: string; channel: string; checked_at: number } | null
}

export interface AssignmentRecord {
  id: string
  task_id: string
  worker_id: string
  worker_name: string
  store: string
  batch_name: string
  revision: string
  status: string
  disposition: string
  result_message: string
}

export interface AssignmentRecordsResponse {
  records: AssignmentRecord[]
  total: number
  page: number
  pages: number
}

export interface PairingCode {
  code: string
  expires_at: string
}

export type AssignConflictReason = 'materials' | 'revision' | 'unavailable' | 'source' | 'assigned'

export interface AssignConflict {
  task_id: string
  reason: AssignConflictReason
  worker_name?: string
}

export interface AssignResponse {
  assignments: HubAssignment[]
}

export interface ReclaimResponse {
  count: number
  tasks: { id: string; source_name?: string; batch_name?: string; version?: string }[]
  results: { id: string; task_id?: string; store?: string; status: 'reclaimed' | 'failed'; message: string }[]
}

export interface ResultFilesResponse {
  files: string[]
}

export interface HubQueueParams {
  page: number
  size: number
  scope: HubQueueScope
  batch: string
  q: string
  filter: HubQueueFilter
  sort: HubQueueSort
  selected: string[]
  /** 调试视图才请求运行事件，正常视图保持响应体精简。 */
  includeEvents?: boolean
}
