import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { UpdateProgressBanner, UpdatingReconnectOverlay } from './UpdateStatusRuntime'
import { clearActiveUpdateSession, writeActiveUpdateSession } from './updateSession'

const mockRole = {
  role: 'worker' as const,
  status: undefined,
  loading: false,
  error: '',
  reload: vi.fn(),
}

vi.mock('../../app/RoleProvider', () => ({
  useRole: () => mockRole,
}))

vi.mock('./hooks', () => ({
  useUpdateStatus: vi.fn(),
}))

import { useUpdateStatus } from './hooks'

function renderWithProviders(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('UpdateStatusRuntime', () => {
  beforeEach(() => {
    window.localStorage.clear()
    clearActiveUpdateSession()
    mockRole.role = 'worker'
    mockRole.error = ''
    mockRole.loading = false
    mockRole.reload.mockReset()
  })

  it('在线更新阶段在顶部显示轻量进度条', () => {
    vi.mocked(useUpdateStatus).mockReturnValue({
      data: {
        role: 'hub',
        current_version: '0.5.8',
        flow: {
          active: true,
          stage: 'downloading',
          version: '0.5.9',
          mode: 'immediate',
          message: '正在拉取新版本镜像层',
          error: '',
          progress: 65,
          authorized_at: '',
          finished_at: '',
        },
        download: { active: true },
        latest: { version: '0.5.9' },
      },
      isError: false,
      isPending: false,
      refetch: vi.fn(),
    } as never)

    renderWithProviders(<UpdateProgressBanner />)
    expect(screen.getByText('正在更新')).toBeInTheDocument()
    expect(screen.getByText(/正在更新至 v0\.5\.9/)).toBeInTheDocument()
    expect(screen.getByText('65%')).toBeInTheDocument()
  })

  it('服务重启断连期间展示友好的正在更新重连遮罩而不是报错', () => {
    writeActiveUpdateSession({
      targetVersion: '0.5.9',
      fromVersion: '0.5.8',
      role: 'worker',
      stage: 'restarting',
      message: '正在验证新版本健康状态',
      progress: 100,
    })
    mockRole.error = '读取服务角色超时'
    vi.mocked(useUpdateStatus).mockReturnValue({
      data: undefined,
      isError: true,
      isPending: false,
      refetch: vi.fn(),
    } as never)

    renderWithProviders(<UpdatingReconnectOverlay />)
    expect(screen.getByText('正在更新中')).toBeInTheDocument()
    expect(screen.getByText('正在更新至 v0.5.9，服务重启中…')).toBeInTheDocument()
    expect(screen.getByText(/这是正常的更新重启过程，并非服务异常崩溃/)).toBeInTheDocument()
    expect(screen.getByText('立即重试连接')).toBeInTheDocument()
  })

  it('没有活跃更新会话时普通断连不会弹出更新遮罩', () => {
    mockRole.error = '读取服务角色超时'
    vi.mocked(useUpdateStatus).mockReturnValue({
      data: undefined,
      isError: true,
      isPending: false,
      refetch: vi.fn(),
    } as never)

    const { container } = renderWithProviders(<UpdatingReconnectOverlay />)
    expect(container).toBeEmptyDOMElement()
  })
})
