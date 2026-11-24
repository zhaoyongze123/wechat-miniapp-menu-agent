import { describe, expect, it } from 'vitest'
import {
  annotationStatusLabel,
  annotationStatusTone,
  categoryLabel,
  categoryOptions,
  countTaskBuckets,
  effectText,
  formatMapping,
  formatStepMapping,
  rejectionReasonLabel,
  taskBucket,
  taskClaimable,
  taskStatusLabel,
} from './format'
import type { AnnotationFrame, AnnotationStep, AnnotationTask } from './types'

const frame: AnnotationFrame = {
  name: 'shots/a.png',
  url: '/api/annotation/frame?session=s1&name=shots%2Fa.png',
  image_width: 2560,
  image_height: 1600,
  window_width: 1280,
  window_height: 800,
  scale_x: 2,
  scale_y: 2,
}

describe('annotation status', () => {
  it('maps known statuses and falls back to raw value', () => {
    expect(annotationStatusLabel('ready')).toBe('等待标注')
    expect(annotationStatusLabel('custom')).toBe('custom')
    expect(annotationStatusLabel(null)).toBe('未知状态')
    expect(annotationStatusTone('error')).toBe('danger')
    expect(annotationStatusTone('finished')).toBe('success')
    expect(annotationStatusTone('custom')).toBe('secondary')
  })
})

describe('task buckets', () => {
  it('prioritizes rejection and completed annotation', () => {
    expect(taskBucket({ id: 'a', reject_reason: 'blur' })).toBe('rejected')
    expect(taskBucket({ id: 'b', stage: 'annotation_complete' })).toBe('annotated')
    expect(taskBucket({ id: 'c', status: 'pending' })).toBe('pending')
    expect(taskBucket({ id: 'd', status: 'needs_review' })).toBe('review')
    expect(taskBucket({ id: 'e', status: 'captured' })).toBe('captured')
    expect(taskBucket({ id: 'f', status: 'running' })).toBe('other')
  })

  it('excludes rejected and annotated tasks from claiming', () => {
    expect(taskClaimable({ id: 'a', reject_reason: 'blur' })).toBe(false)
    expect(taskClaimable({ id: 'b', stage: 'annotation_complete' })).toBe(false)
    expect(taskClaimable({ id: 'c', status: 'pending' })).toBe(true)
    expect(taskClaimable({ id: '', status: 'pending' })).toBe(false)
  })

  it('counts every bucket including all', () => {
    const tasks: AnnotationTask[] = [
      { id: '1', status: 'pending' },
      { id: '2', status: 'needs_review' },
      { id: '3', status: 'captured' },
      { id: '4', stage: 'annotation_complete' },
      { id: '5', reject_reason: 'blur' },
      { id: '6', status: 'running' },
    ]
    expect(countTaskBuckets(tasks)).toEqual({
      all: 6,
      pending: 1,
      review: 1,
      captured: 1,
      annotated: 1,
      rejected: 1,
      other: 1,
    })
  })

  it('labels task status by rejection and annotation stage', () => {
    expect(taskStatusLabel({ id: 'a', reject_reason: 'blur' })).toBe('不合格')
    expect(taskStatusLabel({ id: 'b', stage: 'annotation_complete' })).toBe('已标注')
    expect(taskStatusLabel({ id: 'c', status: 'captured' })).toBe('已采集')
  })
})

describe('annotation formatting', () => {
  it('describes mapping in screenshot pixels', () => {
    expect(formatMapping(null, frame)).toBe('在截图上拖动框选目标')
    expect(formatMapping({ x: 100, y: 200, width: 40, height: 20 }, frame)).toBe(
      '中心 (120, 210) · 框 40 × 20 px · 截图 2560 × 1600',
    )
  })

  it('uses server center points in history', () => {
    const step: AnnotationStep = {
      index: 1,
      center_points: { x: 12.34, y: 56.78 },
      box_px: { x: 10, y: 50, width: 4, height: 6 },
      image_size: { width: 1280, height: 800 },
    }
    expect(formatStepMapping(step)).toBe('中心 (12.3, 56.8) · 框 4 × 6 px · 窗口 1280 × 800')
  })

  it('renders effect outcome with diff score', () => {
    expect(effectText({ index: 1, effect: { changed: true, diff_score: 0.1234 } })).toBe('画面已变化（差异 0.123）')
    expect(effectText({ index: 1, effect: { changed: false } })).toBe('画面基本未变')
    expect(effectText({ index: 1 })).toBe('未判定变化')
  })

  it('maps categories and rejection codes', () => {
    expect(categoryLabel('dish')).toBe('菜品')
    expect(categoryLabel('unknown_code')).toBe('unknown_code')
    expect(categoryOptions(['dish'])).toEqual([{ value: 'dish', label: '菜品' }])
    expect(rejectionReasonLabel('invalid_qr')).toBe('二维码无效')
    expect(rejectionReasonLabel('missing')).toBe('missing')
  })
})
