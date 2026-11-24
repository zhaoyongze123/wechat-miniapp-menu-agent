/** 质量审核展示口径：来源、人工确认、处置桶与重试资格。 */

import type { RejectionRecord, ReviewBucket } from './types'

export const SOURCE_OPTIONS = [
  { value: 'all', label: '全部来源' },
  { value: 'materials', label: '素材清洗' },
  { value: 'capture', label: '采集任务' },
]

export const REVIEW_STATE_OPTIONS = [
  { value: 'all', label: '全部确认状态' },
  { value: 'reviewed', label: '已人工确认' },
  { value: 'pending', label: '未人工确认' },
]

/** 处置桶文案与 `menu_capture/failures.py::BUCKETS` 保持一致。 */
export const REVIEW_BUCKETS: { value: 'all' | ReviewBucket; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'material', label: '素材问题' },
  { value: 'store_unavailable', label: '门店不可采' },
  { value: 'technical', label: '技术重试' },
  { value: 'auth', label: '授权交互' },
  { value: 'environment', label: '环境异常' },
  { value: 'other', label: '其他·未分类' },
]

const BUCKET_VALUES = new Set<string>(
  REVIEW_BUCKETS.map((item) => item.value).filter((value) => value !== 'all'),
)

/** 后端返回 bucket 时直接使用；缺失或未知值归入“其他·未分类”。 */
export function reviewBucketOf(record: { bucket?: string }): ReviewBucket {
  const value = record.bucket ?? ''
  return BUCKET_VALUES.has(value) ? (value as ReviewBucket) : 'other'
}

export function bucketLabelOf(record: { bucket?: string; bucket_label?: string }): string {
  if (record.bucket_label) return record.bucket_label
  const value = reviewBucketOf(record)
  return REVIEW_BUCKETS.find((item) => item.value === value)?.label ?? '其他·未分类'
}

/** 可重新采集/重跑的任务状态；其余状态只展示，不提供操作。 */
export const RETRYABLE_STATUSES = new Set(['needs_review', 'needs_retry', 'error', 'blocked', 'captured'])

export function isRetryable(status: string | undefined): boolean {
  return Boolean(status && RETRYABLE_STATUSES.has(status))
}

/** 不合格记录在抽屉翻页与行高亮中的稳定键。 */
export function rejectionKey(record: RejectionRecord): string {
  return `${record.source_type}:${record.task_id}:${record.batch_id}:${record.store_id ?? ''}`
}

export function sourceLabelOf(record: Pick<RejectionRecord, 'source_type' | 'source_label'>): string {
  return record.source_label || (record.source_type === 'capture' ? '采集任务' : '素材清洗')
}

/** 素材来源可跳回门店修复；采集来源需要任务编号才能打回或重采。 */
export function canContinueMaterials(record: RejectionRecord): boolean {
  return record.source_type === 'materials' && Boolean(record.batch_id && record.store_id)
}

export function canReturnToMaterials(record: RejectionRecord): boolean {
  return record.source_type === 'capture' && Boolean(record.task_id) && record.can_return_to_materials !== false
}
