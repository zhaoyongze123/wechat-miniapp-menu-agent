/** 质量审核 DTO：与 `menu_capture/rejections.py`、`server.py::review_queue` 的真实响应一致。 */

import type { WorkflowStage } from '../materials/types'

export type RejectionSource = 'all' | 'materials' | 'capture'
export type ReviewState = 'all' | 'reviewed' | 'pending'

export interface RejectionRecord {
  source_type: 'materials' | 'capture'
  source_label: string
  record_id: string
  store_key: string
  store_name: string
  source_folder: string
  source_item: string
  reason_code: string
  reason_label: string
  detail: string
  evidence: string
  marked_at: string
  reviewed_at: string
  status: string
  stage: string
  batch_id: string
  batch_name: string
  task_id: string
  run_id: string
  /** 仅素材清洗来源携带，用于跳回门店修复。 */
  store_id?: string
}

export interface RejectionSummary {
  stores: number
  records: number
  materials: number
  capture: number
  reviewed: number
}

export interface RejectionBatch {
  id: string
  name: string
}

export interface RejectionsResponse {
  summary: RejectionSummary
  records: RejectionRecord[]
  batches: RejectionBatch[]
}

export interface RejectionQuery {
  batchId: string
  source: RejectionSource
  reason: string
  q: string
}

/** 人工复核只切换确认标记，不改变任务状态与不合格原因。 */
export interface RejectionReviewPayload {
  source_type: 'materials' | 'capture'
  reviewed: boolean
  task_id?: string
  batch_id?: string
  store_id?: string
}

export type ReviewBucket =
  | 'material'
  | 'store_unavailable'
  | 'technical'
  | 'auth'
  | 'environment'
  | 'other'

export interface PendingReviewRecord {
  task_id: string
  store_name: string
  status: string
  stage: string
  message: string
  error_code: string
  group: string
  label: string
  bucket: ReviewBucket | string
  bucket_label: string
  source_path: string
  output_dir: string
  reject_reason: string
  reject_detail: string
  updated_at: string
  batch_id: string
  batch_name: string
  workflow: WorkflowStage
}

export interface PendingReviewBatch {
  id: string
  name: string
  workflow: WorkflowStage
}

export interface PendingReviewsResponse {
  records: PendingReviewRecord[]
  batches: PendingReviewBatch[]
  summary: {
    stores: number
    batches: number
    needs_review: number
    retryable: number
  }
}
