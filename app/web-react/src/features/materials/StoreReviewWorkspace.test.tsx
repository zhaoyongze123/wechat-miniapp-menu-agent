import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../../components/ToastProvider'
import { UnsavedChangesProvider } from '../../components/UnsavedChangesProvider'
import { StoreReviewWorkspace } from './StoreReviewWorkspace'
import type { MaterialStoreDetail, MaterialStoreSummary } from './types'

const decideMock = vi.fn()
const reanalyzeMock = vi.fn()
const fetchStoreMock = vi.fn()

vi.mock('../projects/ProjectProvider', () => ({
  useProject: () => ({ projectId: 'proj-1' }),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    fetchStore: (...args: unknown[]) => fetchStoreMock(...args),
    decideStore: (...args: unknown[]) => decideMock(...args),
    reanalyzeStore: (...args: unknown[]) => reanalyzeMock(...args),
  }
})

const STORES: MaterialStoreSummary[] = [
  {
    store_id: 's1',
    source_name: '1001_老盛昌汤包馆',
    source_folder: '1001_老盛昌汤包馆',
    analysis_status: 'selected',
    reason: '高置信点餐码',
    recommendation: true,
    decision: 'pending',
    recommendation_level: 'high',
    wechat_validation_status: 'verified',
    rejection: null,
    selected: null,
  },
  {
    store_id: 's2',
    source_name: '1002_喜茶临港店',
    source_folder: '1002_喜茶临港店',
    analysis_status: 'no_candidates',
    reason: '未检测到二维码',
    recommendation: false,
    decision: 'pending',
    recommendation_level: 'none',
    wechat_validation_status: 'unverified',
    rejection: null,
    selected: null,
  },
]

const DETAIL_S1: MaterialStoreDetail = {
  ...STORES[0]!,
  batch_id: 'b1',
  images: [
    {
      path: 'img1.jpg',
      sha256: 'sha1',
      detection_count: 2,
      image_index: 0,
      ordering_text_seen: true,
      candidates: [
        {
          candidate_index: 0,
          box: { x: 100, y: 100, width: 200, height: 200 },
          payload_sha256: 'p1',
          short_edge_px: 200,
          selected: false,
          purpose: 'ordering',
          purpose_reason: '周边识别到扫码点餐',
          source: 'zxing',
          purpose_source: 'local_ocr',
          quality: 'ok',
          recommendation: true,
        },
        {
          candidate_index: 1,
          box: { x: 400, y: 100, width: 180, height: 180 },
          payload_sha256: 'p2',
          short_edge_px: 180,
          selected: false,
          purpose: 'enterprise_wechat',
          purpose_reason: '企业微信域名',
          source: 'zxing',
          purpose_source: 'payload_domain',
          quality: 'ok',
          recommendation: false,
        },
      ],
    },
  ],
}

afterEach(() => {
  vi.clearAllMocks()
})

function renderWorkspace(overrides?: Partial<React.ComponentProps<typeof StoreReviewWorkspace>>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <UnsavedChangesProvider>
          <StoreReviewWorkspace
            batchId="b1"
            batchName="测试批次"
            batchStatus="completed"
            stores={STORES}
            filteredStores={STORES}
            activeStoreId="s1"
            filter="all"
            search=""
            continuousReview={true}
            hasPrev={false}
            hasNext={true}
            prevStoreId={null}
            nextStoreId="s2"
            onFilterChange={vi.fn()}
            onSearchChange={vi.fn()}
            onContinuousReviewChange={vi.fn()}
            onSelectStore={vi.fn()}
            onNavigate={vi.fn()}
            onDecisionSaved={vi.fn()}
            onClose={vi.fn()}
            {...overrides}
          />
        </UnsavedChangesProvider>
      </ToastProvider>
    </QueryClientProvider>,
  )
}

describe('StoreReviewWorkspace', () => {
  it('preselects recommended candidate without auto-submitting, and confirms on Enter', async () => {
    fetchStoreMock.mockResolvedValue(DETAIL_S1)
    decideMock.mockResolvedValue({ ...DETAIL_S1, decision: 'confirmed' })
    const onDecisionSaved = vi.fn()

    renderWorkspace({ onDecisionSaved })

    await waitFor(() => {
      expect(screen.getByText('候选 1')).toBeInTheDocument()
    })
    // Recommended candidate is preselected, never auto-submitted.
    expect(decideMock).not.toHaveBeenCalled()
    expect(screen.getByText('已预选')).toBeInTheDocument()

    // Pressing Enter confirms the preselected recommendation.
    fireEvent.keyDown(window, { key: 'Enter' })
    await waitFor(() => {
      expect(decideMock).toHaveBeenCalledWith(
        'b1',
        's1',
        { decision: 'confirm', image_index: 0, candidate_index: 0 },
        'proj-1',
      )
    })
    expect(onDecisionSaved).toHaveBeenCalledWith('s1')
  })

  it('supports keyboard shortcuts J/K/M and suppresses them inside search input', async () => {
    fetchStoreMock.mockResolvedValue(DETAIL_S1)
    const onNavigate = vi.fn()

    renderWorkspace({ onNavigate })
    await waitFor(() => {
      expect(screen.getByText('候选 1')).toBeInTheDocument()
    })

    // Typing J inside search input does NOT navigate.
    const searchInput = screen.getByPlaceholderText('搜索门店名或目录')
    fireEvent.keyDown(searchInput, { key: 'j' })
    expect(onNavigate).not.toHaveBeenCalled()

    // Pressing J on window navigates to next store.
    fireEvent.keyDown(window, { key: 'j' })
    expect(onNavigate).toHaveBeenCalledWith(1)

    // Pressing M toggles manual crop mode.
    fireEvent.keyDown(window, { key: 'm' })
    expect(screen.getByText('人工框选预览')).toBeInTheDocument()
  })

  it('shows detector error banner distinct from normal zero-candidate store', async () => {
    fetchStoreMock.mockResolvedValue({
      ...DETAIL_S1,
      store_id: 's2',
      source_name: '1002_喜茶临港店',
      analysis_status: 'failed',
      reason: 'ZXing 检测服务异常超时',
      images: [{ path: 'err.jpg', sha256: 'e1', detection_count: 0, candidates: [], image_index: 0, error: '图片解码超时' }],
    })

    renderWorkspace({ activeStoreId: 's2' })
    await waitFor(() => {
      expect(screen.getByText('检测服务异常')).toBeInTheDocument()
    })
    expect(screen.getByText('图片解码超时')).toBeInTheDocument()
  })
})
