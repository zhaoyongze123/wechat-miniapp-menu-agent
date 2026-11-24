import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PendingReviewDrawer } from './PendingReviewDrawer'
import type { PendingReviewRecord } from './types'

const record: PendingReviewRecord = {
  task_id: 'task-1',
  store_name: 'E0005__LCA0000193429__佛山市__示例酒店__113.288719,22.954428',
  status: 'needs_review',
  stage: 'manual',
  message: '页面未进入菜单',
  error_code: '',
  group: 'other',
  label: '其他未分类',
  bucket: 'other',
  bucket_label: '其他未分类',
  source_path: '/var/lib/menu-capture/output/example.png',
  output_dir: '',
  reject_reason: '',
  reject_detail: '',
  updated_at: '2026-11-24T09:00:00+08:00',
  batch_id: 'batch-1',
  batch_name: '26Q3-3-100',
  workflow: { stage: 'auto', label: '自动阶段', capture_rounds: 0, source: '', manual_stage: '' },
}

function renderDrawer(overrides: Partial<PendingReviewRecord> = {}) {
  const onReject = vi.fn()
  const onRerun = vi.fn()
  render(
    <PendingReviewDrawer
      record={{ ...record, ...overrides }}
      hasPrev={false}
      hasNext
      onOpenChange={vi.fn()}
      onPrev={vi.fn()}
      onNext={vi.fn()}
      onRerun={onRerun}
      onReject={onReject}
    />,
  )
  return { onReject, onRerun }
}

describe('PendingReviewDrawer', () => {
  it('待检查门店可直接标记不合格', () => {
    const { onReject } = renderDrawer()
    fireEvent.click(screen.getByRole('button', { name: '标记不合格' }))
    expect(onReject).toHaveBeenCalledWith(expect.objectContaining({ task_id: 'task-1' }))
  })

  it('仍保留重跑入口', () => {
    const { onRerun } = renderDrawer()
    fireEvent.click(screen.getByRole('button', { name: '重跑（分配采集电脑）' }))
    expect(onRerun).toHaveBeenCalled()
  })
})
