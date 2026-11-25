import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../../components/ToastProvider'
import { ReviewDispositionDialog } from './ReviewDispositionDialog'
import type { PendingReviewRecord } from './types'

const fetchPendingReviews = vi.fn()
vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return { ...actual, fetchPendingReviews: (...args: unknown[]) => fetchPendingReviews(...args) }
})

const record: PendingReviewRecord = {
  task_id: 'task-1',
  store_name: 'E0005__LCA0000193429__佛山市__示例酒店',
  status: 'needs_review',
  stage: 'manual',
  message: '页面未进入菜单',
  error_code: '',
  group: 'other',
  label: '其他未分类',
  bucket: 'other',
  bucket_label: '其他未分类',
  source_path: '',
  output_dir: '',
  reject_reason: '',
  reject_detail: '',
  updated_at: '2026-11-24T09:00:00+08:00',
  batch_id: 'batch-1',
  batch_name: '26Q3-3-100',
  workflow: { stage: 'auto', label: '自动阶段', capture_rounds: 0, source: '', manual_stage: '' },
  can_return_to_materials: true,
}

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <ReviewDispositionDialog open taskId="task-1" onOpenChange={vi.fn()} />
      </ToastProvider>
    </QueryClientProvider>,
  )
}

describe('ReviewDispositionDialog', () => {
  beforeEach(() => {
    fetchPendingReviews.mockReset()
  })

  it('在任务详情内直接提供处置动作，不跳转页面', async () => {
    fetchPendingReviews.mockResolvedValue({
      records: [record],
      batches: [],
      summary: { stores: 1, batches: 1, needs_review: 1, retryable: 1 },
    })
    renderDialog()
    expect(await screen.findByRole('button', { name: '标记不合格' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '打回素材清洗' })).toBeTruthy()
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('从处置动作直接打开不合格原因选择，保持原弹窗上下文', async () => {
    fetchPendingReviews.mockResolvedValue({
      records: [record],
      batches: [],
      summary: { stores: 1, batches: 1, needs_review: 1, retryable: 1 },
    })
    renderDialog()
    fireEvent.click(await screen.findByRole('button', { name: '标记不合格' }))
    expect(await screen.findByText('将从待检查清单移入不合格清单，并记录原因与说明。该操作只保存审核结论，不会自动重新采集。')).toBeTruthy()
  })

  it('门店已不在待检查队列时如实说明，不提供处置按钮', async () => {
    fetchPendingReviews.mockResolvedValue({
      records: [],
      batches: [],
      summary: { stores: 0, batches: 0, needs_review: 0, retryable: 0 },
    })
    renderDialog()
    expect(await screen.findByText('该门店已不在待检查队列')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '标记不合格' })).toBeNull()
  })
})
