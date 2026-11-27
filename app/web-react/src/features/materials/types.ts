/** 素材清洗 DTO：字段与 `menu_capture/materials.py` 的真实响应一致，缺失值不做推断。 */

export type StoreDecision = 'pending' | 'confirmed' | 'skipped'
export type ReworkCategory = 'pending' | 'rejected' | 'unpublished'
export type UploadMode = 'append' | 'replace'

export interface Box {
  x: number
  y: number
  width: number
  height: number
}

export interface BatchProgress {
  done: number
  total: number
}

export interface BatchSummaryCounts {
  total_stores: number
  total_images: number
  selected_stores: number
  review_stores: number
  confirmed_stores: number
  skipped_stores: number
  pending_stores: number
  detection_stats?: Record<string, number | { hit_stores?: number; total_ms?: number }>
}

export interface WorkflowStage {
  stage: string
  label: string
  capture_rounds: number
  source: string
  manual_stage: string
}

export interface ReleaseRecord {
  release_id: string
  batch_id: string
  batch_name: string
  snapshot_id: string
  version: string
  description: string
  published_at: string
  store_count: number
  active: boolean
}

export interface DraftChanges {
  added: number
  removed: number
  changed: number
}

export interface BatchManagementFlags {
  name: string
  description: string
  deleted_at: string
  workflow: WorkflowStage
  versions_count: number
  current_active: boolean
  can_delete: boolean
  delete_block_reason: string
  can_purge: boolean
  purge_block_reason: string
  can_clear_selection: boolean
  clear_selection_block_reason: string
}

/** 批次列表/管理列表的行：批次摘要 + 版本摘要 + 管理能力标记。 */
export interface MaterialBatch extends BatchManagementFlags {
  batch_id: string
  source_kind: string
  status: string
  progress: BatchProgress
  summary: BatchSummaryCounts
  error: string
  created_at: string
  updated_at: string
  release: ReleaseRecord | null
  releases: ReleaseRecord[]
  suggested_version: string
  has_changes: boolean
  draft_changes: DraftChanges
}

export interface BatchOption {
  batch_id: string
  name: string
  created_at: string
  workflow: WorkflowStage
  current_active: boolean
}

export interface JobEvent {
  id: number
  job_id: string
  time: string
  level: string
  store_id: string
  store_name: string
  message: string
}

export interface JobCounts {
  total: number
  done: number
  failed: number
  remaining: number
  running: number
}

export interface MaterialJob {
  id: string
  batch_id: string
  kind: string
  status: string
  total_stores: number
  done_stores: number
  failed_stores: number
  concurrency: number
  current_store_id: string
  current_store_name: string
  stage: string
  message: string
  error: string
  owner: string
  heartbeat_at: string
  started_at: string
  finished_at: string
  created_at: string
  updated_at: string
  counts?: JobCounts
  queue_position?: number
  eta_seconds?: number
  batch_name?: string
  events?: JobEvent[]
}

export interface StoreRejection {
  code: string
  label: string
  detail: string
  marked_at: string
  reviewed_at: string
}

export interface SelectedQr {
  image_index: number
  candidate_index: number | null
  box: Box
  crop_margin_ratio: number
  box_space?: string
}

export interface MaterialStoreSummary {
  store_id: string
  source_name: string
  source_folder: string
  analysis_status: string
  reason: string
  recommendation: boolean
  decision: StoreDecision
  recommendation_level: string
  wechat_validation_status: string
  rejection: StoreRejection | null
  selected: SelectedQr | null
  /** 跨批次工作台场景下的可选批次上下文，单批次场景缺省。 */
  batch_id?: string
  batch_name?: string
  batch_status?: string
  queue_key?: string
}

export interface QrCandidate {
  box: Box
  payload_sha256: string | null
  short_edge_px: number
  selected: boolean
  purpose: string
  purpose_reason: string
  source: string
  purpose_source: string
  kind?: string
  quality?: 'ok' | 'rejected'
  quality_reason?: string
  qr_likeness?: boolean
  qr_contained?: boolean
  qr_centered?: boolean
  qr_fill?: number
  retry?: boolean
  sharpness_score?: number
  quality_score?: number
  candidate_index: number
  recommendation: boolean
}

export interface StoreImage {
  path: string
  sha256: string
  detection_count: number
  candidates: QrCandidate[]
  ordering_text_seen?: boolean
  possible_missed_code?: boolean
  is_multi_qr_image?: boolean
  error?: string
  display_name?: string
  image_index: number
}

export interface MaterialStoreDetail extends MaterialStoreSummary {
  batch_id: string
  images: StoreImage[]
}

/** `GET /api/materials/batches/{batch_id}` 的响应。 */
export interface MaterialBatchDetail {
  batch_id: string
  source_kind: string
  source_root: string
  status: string
  progress: BatchProgress
  summary: BatchSummaryCounts
  error: string
  snapshot_id: string | null
  job: MaterialJob | null
  workflow: WorkflowStage
  name: string
  release: ReleaseRecord | null
  releases: ReleaseRecord[]
  suggested_version: string
  has_changes: boolean
  draft_changes: DraftChanges
  stores: MaterialStoreSummary[]
}

export interface BatchesResponse {
  batches: MaterialBatch[]
}

export interface BatchOptionsResponse {
  batches: BatchOption[]
}

export interface BatchManagementResponse {
  batches: MaterialBatch[]
}

export interface BeginUploadResponse {
  batch_id: string
  status: string
  file_count: number
  total_bytes: number
}

export interface UploadFileResponse {
  batch_id: string
  file_count: number
  total_bytes: number
}

export interface StartAnalysisResponse extends MaterialBatchDetail {
  job: MaterialJob
}

export interface JobsResponse {
  jobs: MaterialJob[]
}

export interface AcceptRecommendationsResponse {
  confirmed: number
}

export interface PublishResponse {
  snapshot_id: string
  release: ReleaseRecord
  input_dir: string
  task_scan: { added: number; [key: string]: unknown }
  already_published: boolean
}

export interface BeginStoreUploadResponse {
  upload_id: string
  status: string
}

export interface UploadedStoreFile {
  path: string
  display_name: string
  sha256: string
}

export interface CommitStoreUploadResponse {
  store: MaterialStoreDetail
  uploaded: number
  mode: UploadMode
}

export interface ReworkRecord {
  batch_id: string
  batch_name: string
  batch_status?: string
  store_id: string
  store_name: string
  source_folder: string
  decision: StoreDecision
  category: ReworkCategory
  reason_code: string
  reason_label: string
  reason_detail: string
  updated_at: string
  version: string
  publishable: boolean
}

export interface ReworkSummary {
  stores: number
  pending: number
  rejected: number
  unpublished: number
  batches: number
}

export interface ReworkResponse {
  records: ReworkRecord[]
  summary: ReworkSummary
}

export interface ReworkPublishResult {
  batch_id: string
  batch_name: string
  snapshot_id: string
  version: string
  already_published: boolean
  task_scan: { added: number; [key: string]: unknown }
}

export interface ReworkPublishResponse {
  results: ReworkPublishResult[]
  errors: { batch_id: string; batch_name: string; error: string }[]
}

export interface DecisionPayload {
  decision: 'confirm' | 'skip' | 'pending'
  image_index?: number
  candidate_index?: number
  box?: Box
  reject_reason?: string
  reject_detail?: string
}

export interface BatchListParams {
  deleted: boolean
}
