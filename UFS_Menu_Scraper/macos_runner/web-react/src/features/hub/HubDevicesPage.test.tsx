import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../../components/ToastProvider'
import { HubDevicesPage } from './HubDevicesPage'
import type { HubSharedStatus } from './types'

const WORKER_A = 'w'.repeat(32)
const WORKER_B = 'x'.repeat(32)

const shared: HubSharedStatus = {
  role: 'hub',
  workers: [
    {
      id: WORKER_A,
      name: '设计电脑A',
      last_seen: '2026-11-24 09:00:00',
      pending_uploads: 0,
      in_rollout: true,
      software: {
        current_version: '0.2.2',
        platform: 'windows',
        latest_version: '0.2.3',
        update_available: true,
        channel: 'stable',
        checked_at: '2026-11-24T01:00:00+00:00',
        download_status: 'completed',
        install_status: 'idle',
        reported_at: '2026-11-24 09:00:00',
      },
    },
    {
      id: WORKER_B,
      name: '设计电脑B',
      last_seen: '2026-11-24 08:30:00',
      pending_uploads: 0,
      in_rollout: false,
      software: null,
    },
  ],
  assignments: [],
  update_summary: {
    reported: 1,
    unreported: 1,
    update_available: 1,
    target_version: '0.2.3',
    versions: { '0.2.2': 1 },
    rollout_mode: 'batch',
    rollout_scope: 1,
  },
  update_rollout: {
    mode: 'batch',
    worker_ids: [WORKER_A],
    updated_at: '2026-11-24 09:10:00',
    target_version: '0.2.3',
    workers: [
      { id: WORKER_A, name: '设计电脑A', archived_at: '' },
      { id: WORKER_B, name: '设计电脑B', archived_at: '' },
    ],
  },
}

const { saveUpdateRollout } = vi.hoisted(() => ({ saveUpdateRollout: vi.fn() }))

vi.mock('../../app/RoleProvider', () => ({
  useRole: () => ({ role: 'hub', loading: false, error: '', reload: vi.fn() }),
}))
vi.mock('./hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks')>()
  return {
    ...actual,
    useHubSharedStatus: () => ({ data: shared, isError: false, error: null, refetch: vi.fn() }),
  }
})
vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, saveUpdateRollout }
})

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <HubDevicesPage />
      </ToastProvider>
    </QueryClientProvider>,
  )
}

afterEach(cleanup)

describe('HubDevicesPage 集中更新状态', () => {
  it('展示各设备版本、更新状态与汇总', () => {
    renderPage()

    expect(screen.getByText(/集中更新目标/)).toBeInTheDocument()
    expect(screen.getAllByText('0.2.2').length).toBeGreaterThan(0)
    expect(screen.getByText('可更新至 0.2.3')).toBeInTheDocument()
    expect(screen.getByText('未上报')).toBeInTheDocument()
    expect(screen.getByText('待更新')).toBeInTheDocument()
    expect(screen.getByText('版本未上报')).toBeInTheDocument()
  })

  it('展示分批推送范围并允许改回全部设备', async () => {
    saveUpdateRollout.mockResolvedValue({
      mode: 'all',
      worker_ids: [],
      updated_at: '2026-11-24 09:20:00',
      target_version: '0.2.3',
      workers: shared.update_rollout!.workers,
    })
    renderPage()

    expect(screen.getByText(/把目标版本 0.2.3 推送给指定采集电脑/)).toBeInTheDocument()
    expect(screen.getByText('本批不推送')).toBeInTheDocument()
    expect(screen.getByText(/当前生效：1 台设备接收更新/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '全部设备' }))
    fireEvent.click(screen.getByRole('button', { name: '保存推送范围' }))

    await waitFor(() => expect(saveUpdateRollout).toHaveBeenCalledWith('all', []))
  })
})
