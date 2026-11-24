import { describe, expect, it } from 'vitest'
import {
  bucketLabelOf,
  canContinueMaterials,
  canReturnToMaterials,
  isRetryable,
  rejectionKey,
  reviewBucketOf,
} from './format'
import type { RejectionRecord } from './types'

const base: RejectionRecord = {
  source_type: 'capture',
  source_label: '采集任务',
  record_id: 'task-1',
  store_key: 'A0001__上海市__示例门店__1,2',
  store_name: '示例门店',
  source_folder: 'A0001',
  source_item: 'frame.png',
  reason_code: 'blur',
  reason_label: '图片模糊 / 无法识别',
  detail: '',
  evidence: 'evidence.png',
  marked_at: '2026-01-01 10:00:00',
  reviewed_at: '',
  status: 'needs_review',
  stage: 'captured',
  batch_id: 'batch-1',
  batch_name: '批次一',
  task_id: 'task-1',
  run_id: 'run-1',
}

describe('质量审核口径', () => {
  it('未知处置桶统一归入其他', () => {
    expect(reviewBucketOf({ bucket: 'technical' })).toBe('technical')
    expect(reviewBucketOf({ bucket: 'legacy-code' })).toBe('other')
    expect(reviewBucketOf({})).toBe('other')
  })

  it('处置桶标签优先使用后端文案', () => {
    expect(bucketLabelOf({ bucket: 'auth', bucket_label: '授权交互' })).toBe('授权交互')
    expect(bucketLabelOf({ bucket: 'auth' })).toBe('授权交互')
    expect(bucketLabelOf({})).toBe('其他·未分类')
  })

  it('只有可重试状态允许重新采集', () => {
    expect(isRetryable('needs_review')).toBe(true)
    expect(isRetryable('captured')).toBe(true)
    expect(isRetryable('pending')).toBe(false)
    expect(isRetryable(undefined)).toBe(false)
  })

  it('不合格记录键区分来源与门店', () => {
    expect(rejectionKey(base)).toBe('capture:task-1:batch-1:')
    expect(rejectionKey({ ...base, source_type: 'materials', store_id: 'store-1' })).toBe(
      'materials:task-1:batch-1:store-1',
    )
  })

  it('素材来源需要批次和门店才能继续处理', () => {
    const material = { ...base, source_type: 'materials' as const, store_id: 'store-1' }
    expect(canContinueMaterials(material)).toBe(true)
    expect(canContinueMaterials({ ...material, store_id: '' })).toBe(false)
    expect(canReturnToMaterials(base)).toBe(true)
    expect(canReturnToMaterials({ ...base, task_id: '' })).toBe(false)
  })
})
