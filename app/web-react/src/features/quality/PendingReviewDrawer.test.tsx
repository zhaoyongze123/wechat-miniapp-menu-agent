import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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
  const onReturn = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <PendingReviewDrawer
        record={{ ...record, ...overrides }}
        hasPrev={false}
        hasNext
        onOpenChange={vi.fn()}
        onPrev={vi.fn()}
        onNext={vi.fn()}
        onRerun={onRerun}
        onReject={onReject}
        onReturn={onReturn}
      />
    </QueryClientProvider>,
  )
  return { onReject, onRerun, onReturn }
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

  it('可打回素材清洗', () => {
    const { onReturn } = renderDrawer({ can_return_to_materials: true })
    fireEvent.click(screen.getByRole('button', { name: '打回素材清洗' }))
    expect(onReturn).toHaveBeenCalledWith(expect.objectContaining({ task_id: 'task-1' }))
  })

  it('不可打回时禁用并展示原因', () => {
    renderDrawer({
      can_return_to_materials: false,
      return_to_materials_disabled_reason: '该门店仍在采集电脑任务中',
    })
    const button = screen.getByRole('button', { name: '打回素材清洗' })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', '该门店仍在采集电脑任务中')
  })

  it('历史记录缺失轮次时如实提示，不伪造记录', () => {
    renderDrawer()
    fireEvent.click(screen.getByRole('radio', { name: '历史记录' }))
    expect(screen.getByText('没有可用的采集轮次')).toBeInTheDocument()
  })
})
