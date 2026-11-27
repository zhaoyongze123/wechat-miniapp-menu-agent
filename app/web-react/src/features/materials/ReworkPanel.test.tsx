import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../../components/ToastProvider'
import { UnsavedChangesProvider } from '../../components/UnsavedChangesProvider'
import { ReworkPanel } from './ReworkPanel'
import type { MaterialStoreDetail, ReworkResponse } from './types'

const fetchReworkMock = vi.fn()
const publishReworkMock = vi.fn()
const returnReworkMock = vi.fn()
const fetchStoreMock = vi.fn()

vi.mock('../projects/ProjectProvider', () => ({
  useProject: () => ({ projectId: 'proj-1' }),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    fetchRework: (...args: unknown[]) => fetchReworkMock(...args),
    publishRework: (...args: unknown[]) => publishReworkMock(...args),
    returnReworkStore: (...args: unknown[]) => returnReworkMock(...args),
    fetchStore: (...args: unknown[]) => fetchStoreMock(...args),
  }
})

const REWORK_DATA: ReworkResponse = {
  records: [
    {
      batch_id: 'b1',
      batch_name: '批次五',
      batch_status: 'completed',
      store_id: 's1',
      store_name: '1001_老盛昌汤包馆',
      source_folder: '1001_老盛昌汤包馆',
      decision: 'pending',
      category: 'pending',
      reason_code: '',
      reason_label: '',
      reason_detail: '',
      updated_at: '2026-11-27T10:00:00Z',
      version: 'v1.0.0',
      publishable: false,
    },
    {
      batch_id: 'b1',
      batch_name: '批次五',
      batch_status: 'completed',
      store_id: 's2',
      store_name: '1002_喜茶临港店',
      source_folder: '1002_喜茶临港店',
      decision: 'confirmed',
      category: 'unpublished',
      reason_code: '',
      reason_label: '',
      reason_detail: '',
      updated_at: '2026-11-27T10:05:00Z',
      version: 'v1.0.0',
      publishable: true,
    },
    {
      batch_id: 'b2',
      batch_name: '批次七',
      batch_status: 'completed',
      store_id: 's3',
      store_name: '1003_霸王茶姬',
      source_folder: '1003_霸王茶姬',
      decision: 'confirmed',
      category: 'unpublished',
      reason_code: '',
      reason_label: '',
      reason_detail: '',
      updated_at: '2026-11-27T10:06:00Z',
      version: 'v1.0.0',
      publishable: true,
    },
  ],
  summary: {
    stores: 3,
    pending: 1,
    rejected: 0,
    unpublished: 2,
    batches: 2,
  },
}

const STORE_DETAIL_S1: MaterialStoreDetail = {
  batch_id: 'b1',
  store_id: 's1',
  source_name: '1001_老盛昌汤包馆',
  source_folder: '1001_老盛昌汤包馆',
  analysis_status: 'selected',
  reason: '',
  recommendation: false,
  decision: 'pending',
  recommendation_level: '',
  wechat_validation_status: '',
  rejection: null,
  selected: null,
  images: [],
}

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <UnsavedChangesProvider>
          <ReworkPanel />
        </UnsavedChangesProvider>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('ReworkPanel 跨批次处理', () => {
  it('支持待处理行直接打开详情工作台，且不对待处理行显示打回按钮', async () => {
    fetchReworkMock.mockResolvedValue(REWORK_DATA)
    fetchStoreMock.mockResolvedValue(STORE_DETAIL_S1)

    renderPanel()

    await screen.findByText('1001_老盛昌汤包馆')
    const handleButtons = screen.getAllByRole('button', { name: '处理' })
    expect(handleButtons).toHaveLength(1)
    // 只有非待处理的 2 条记录显示「打回」按钮
    expect(screen.getAllByRole('button', { name: '打回' })).toHaveLength(2)

    fireEvent.click(handleButtons[0]!)
    await waitFor(() => {
      expect(fetchStoreMock).toHaveBeenCalledWith('b1', 's1', expect.anything(), 'proj-1')
    })
    expect(await screen.findByRole('region', { name: '门店素材审核工作台' })).toBeTruthy()
  })

  it('支持通过显性按钮一键跨批次增量发布全部待发布门店并展示按批次分组预览', async () => {
    fetchReworkMock.mockResolvedValue(REWORK_DATA)
    publishReworkMock.mockResolvedValue({
      results: [
        { batch_id: 'b1', batch_name: '批次五', snapshot_id: 'snap-1', version: 'v1.0.1', already_published: false, task_scan: { added: 1 } },
        { batch_id: 'b2', batch_name: '批次七', snapshot_id: 'snap-2', version: 'v1.0.1', already_published: false, task_scan: { added: 1 } },
      ],
      errors: [],
    })

    renderPanel()

    const publishAllBtn = await screen.findByRole('button', { name: '发布全部待发布（2）' })
    fireEvent.click(publishAllBtn)

    expect(await screen.findByText('跨批次增量发布 2 家门店')).toBeTruthy()
    expect(screen.getAllByText('批次五').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('批次七').length).toBeGreaterThanOrEqual(1)

    fireEvent.click(screen.getByRole('button', { name: '确认发布（2 家）' }))
    await waitFor(() => {
      expect(publishReworkMock).toHaveBeenCalledWith(['b1:s2', 'b2:s3'], '跨批次返工发布', 'proj-1')
    })
  })
})
