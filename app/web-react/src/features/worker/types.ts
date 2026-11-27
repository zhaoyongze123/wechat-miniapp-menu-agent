/** Windows 采集端工作台 DTO；字段缺失按未知展示，不推断事实。 */

export interface WorkerTask {
  id: string
  source_name: string
  source_path?: string
  status: string
  stage?: string
  message?: string
  store_state?: string
  reject_reason?: string
  reject_label?: string
  reject_evidence?: string
  capture_outcome?: string
  runnable?: boolean
  batch_id?: string
  batch_name?: string
  version?: string
  snapshot_id?: string
  updated_at?: string
  error_code?: string
  label?: string
  group?: string
}

export interface WorkerRunTask {
  id: string
  name: string
  status: string
}

export interface WorkerRun {
  id: string
  status: string
  mode: string
  capture_mode?: string
  name?: string
  message?: string
  total?: number
  processed?: number
  captured?: number
  unqualified?: number
  review?: number
  remaining?: number
  current_index?: number
  current_task_id?: string
  created_at?: string
  updated_at?: string
  cross_batch?: boolean
  batch_count?: number
  next_name?: string
  task_list?: WorkerRunTask[]
}

export interface WorkerQueue {
  page: number
  pages: number
  total: number
  batches?: { id: string; name: string }[]
}

export interface ManualState {
  run_id?: string
  wait_id?: string
  task_id: string
  store_name: string
  observed_store_name?: string
  index: number
  total: number
  stage: string
  waiting: boolean
  decision: string
  reason?: string
  last_result?: string
}

export interface ManualRecognizeStoreNameResponse {
  ok: boolean
  task_id: string
  observed_store_name: string
  recognized_name?: string
  reason?: string
  stale?: boolean
  manual?: ManualState | null
}

export interface ManualSaveStoreNameResponse {
  ok: boolean
  task_id: string
  observed_store_name: string
  manual?: ManualState | null
}

export interface ManualControlState {
  device_id: string
  device_name: string
  manual: ManualState | null
  run_name: string
  run_status: string
  remote_url: string
}

export interface ManualCommandContext {
  device_id: string
  run_id: string
  task_id: string
  wait_id: string
}

export interface WorkerEvent {
  time: string
  level: string
  message: string
  task_id?: string
}

export interface WorkerRelease {
  batch_id?: string
  batch_name?: string
  version?: string
  snapshot_id?: string
  store_count?: number
  workflow?: string
}

export interface WorkerReadiness {
  ready: boolean | null
  issues?: string[]
  launch_mode?: string
}

export interface WorkerStatus {
  execution_engine?: string
  launch_mode?: string
  engine_ready?: boolean | null
  engine_message?: string
  robot_readiness?: WorkerReadiness
  run_directory?: string
  running?: boolean
  stop_requested?: boolean
  run?: WorkerRun | null
  current_task?: WorkerTask | null
  material_release?: WorkerRelease | null
  batch_output_dir?: string
  input_name?: string
  counts?: Record<string, number>
  tasks?: WorkerTask[]
  queue?: WorkerQueue
  material_rejected?: number
  material_rejected_all?: number
  events?: WorkerEvent[]
  manual?: ManualState | null
}

export type WorkerFilter = 'all' | 'pending' | 'running' | 'captured' | 'rejected' | 'review' | 'returned'
export type WorkerSort = 'source' | 'recent' | 'status'
export type CaptureMode = 'auto' | 'manual'

export interface StartCaptureResponse {
  status?: WorkerStatus
  run?: WorkerRun
}

export interface ManualActionResponse {
  ok: boolean
  task_id?: string
  action?: string
  manual?: ManualState | null
  status?: WorkerStatus
}

/** 与服务端 `/api/status` 的 queue_filter 取值保持一致；Worker 端在前端过滤。 */
export const WORKER_FILTER_VALUES: WorkerFilter[] = ['all', 'pending', 'running', 'captured', 'rejected', 'review', 'returned']
export const WORKER_SORT_VALUES: WorkerSort[] = ['source', 'recent', 'status']
/** 与服务端 REVIEW_RERUN_LIMIT 一致：一次开跑最多锁定 300 家门店。 */
export const START_RUN_LIMIT = 300
