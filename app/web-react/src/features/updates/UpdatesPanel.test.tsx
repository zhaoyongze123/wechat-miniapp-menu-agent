import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../../components/ToastProvider'
import type {
  UpdateDownloadState,
  UpdateFlow,
  UpdateHistoryRow,
  UpdateInstallState,
  UpdateStatus,
  UpdateStoragePreview,
} from './types'

const mocks = vi.hoisted(() => ({
  status: { isPending: false, isError: false, data: undefined as UpdateStatus | undefined, error: null, refetch: vi.fn() },
  install: {
    isError: false,
    error: null as Error | null,
    data: undefined as UpdateInstallState | undefined,
  },
  history: { isError: false, data: [] as UpdateHistoryRow[], error: null, refetch: vi.fn() },
  cleanup: {
    isError: false,
    error: null as Error | null,
    data: undefined as UpdateStoragePreview | undefined,
    refetch: vi.fn(),
  },
  actions: {
    check: { mutate: vi.fn(), isPending: false },
    savePreferences: { mutate: vi.fn(), isPending: false },
    download: { mutate: vi.fn(), isPending: false },
    cancelDownload: { mutate: vi.fn(), isPending: false },
    install: { mutate: vi.fn(), isPending: false },
    update: { mutate: vi.fn(), isPending: false },
    cancelUpdate: { mutate: vi.fn(), isPending: false },
    scheduleInstall: { mutate: vi.fn(), isPending: false },
    cancelSchedule: { mutate: vi.fn(), isPending: false },
    cleanup: { mutate: vi.fn(), isPending: false },
  },
}))

vi.mock('./hooks', () => ({
  useUpdateStatus: () => mocks.status,
  useUpdateInstall: () => mocks.install,
  useUpdateHistory: () => mocks.history,
  useUpdateCleanup: () => mocks.cleanup,
  useUpdateActions: () => mocks.actions,
}))

import { UpdatesPanel } from './UpdatesPanel'

function renderPanel() {
  return render(
    <ToastProvider>
      <UpdatesPanel />
    </ToastProvider>,
  )
}

function downloadState(overrides: Partial<UpdateDownloadState> = {}): UpdateDownloadState {
  return {
    active: false,
    status: 'idle',
    version: '',
    asset_kind: '',
    asset_name: '',
    arch: '',
    sha256: '',
    etag: '',
    last_modified: '',
    downloaded: 0,
    total: 0,
    started_at: '',
    finished_at: '',
    error: '',
    path: '',
    resumable: false,
    ...overrides,
  }
}

function flow(overrides: Partial<UpdateFlow> = {}): UpdateFlow {
  return {
    active: false,
    stage: 'idle',
    version: '',
    mode: 'immediate',
    message: '',
    error: '',
    progress: 0,
    authorized_at: '',
    finished_at: '',
    ...overrides,
  }
}

function status(overrides: Partial<UpdateStatus> = {}): UpdateStatus {
  return {
    role: 'worker',
    repository: 'zhaoyongze123/wechat-miniapp-menu-agent',
    platform: 'windows',
    arch: 'x64',
    current_version: '0.2.2',
    channel: 'stable',
    preferences: { auto_check: true, auto_download: false, keep_versions: 3, channel: 'stable' },
    latest: {
      version: '0.3.0',
      tag: 'v0.3.0',
      channel: 'stable',
      name: 'MenuCapture 0.3.0',
      html_url: 'https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/releases/tag/v0.3.0',
      published_at: '2026-11-24T00:00:00Z',
      notes: '修复稳定性问题。',
      eligible: true,
      reason: '',
      git_commit: 'b'.repeat(40),
      built_at: '2026-11-24T00:00:00Z',
      min_supported_version: '0.2.0',
      rollback: 'supported',
      assets: [
        { kind: 'windows_installer', name: 'MenuCapture-0.3.0.exe', size: 1024, sha256: 'a'.repeat(64), url: 'https://github.com/x/releases/download/v0.3.0/a.exe', platform: 'windows', arch: 'x64' },
      ],
    },
    update_available: true,
    last_checked: '2026-11-24T09:00:00+00:00',
    stale: false,
    error: '',
    checking: false,
    download: downloadState(),
    schedule: { armed: false, version: '', mode: 'idle', requested_at: '', note: '', last_result: '' },
    flow: flow(),
    capabilities: { check: true, download: true, install: false, schedule: false, rollback: false, inventory: true, cleanup: true },
    inventory: {
      supported: true,
      install_root: 'D:\\menu-agent',
      current_version: '0.2.2',
      running_version: '0.2.2',
      releases: [
        { version: '0.2.2', built_at: '', git_commit: '', size_bytes: 2048, current: true, running: true, protected: true },
        { version: '0.2.1', built_at: '', git_commit: '', size_bytes: 1024, current: false, running: false, protected: true },
        { version: '0.2.0', built_at: '', git_commit: '', size_bytes: 1024, current: false, running: false, protected: true },
        { version: '0.1.9', built_at: '', git_commit: '', size_bytes: 1024, current: false, running: false, protected: false },
      ],
      cleanup_candidates: [
        { version: '0.1.9', built_at: '', git_commit: '', size_bytes: 1024, current: false, running: false, protected: false },
      ],
      cleanup_supported: false,
      cleanup_reason: '自动清理执行器尚未启用',
    },
    ...overrides,
  }
}

function installState(overrides: Partial<UpdateInstallState> = {}): UpdateInstallState {
  return {
    supported: false,
    reason: '安装执行器需在 Windows 采集端并显式启用后才能使用',
    active: false,
    status: 'idle',
    target_version: '',
    previous_version: '',
    started_at: '',
    finished_at: '',
    message: '',
    error: '',
    ...overrides,
  }
}

function storagePreview(overrides: Partial<UpdateStoragePreview> = {}): UpdateStoragePreview {
  return {
    generated_at: '2026-11-24T09:00:00Z',
    blocked: false,
    blocked_reason: '',
    confirm: 'cleanup',
    scopes: {
      releases: {
        supported: true,
        reason: '',
        items: [{ version: '0.1.9', size_bytes: 1024, path: 'D:\\menu-agent\\releases\\0.1.9' }],
        reclaimable_bytes: 1024,
      },
      downloads: { supported: true, reason: '', kept: ['0.3.0'], items: [], reclaimable_bytes: 0 },
      logs: { supported: true, reason: '只清理超过 7 天的 CLI 转录', items: [], reclaimable_bytes: 0 },
    },
    reclaimable_bytes: 1024,
    ...overrides,
  }
}

const INSTALL_CAPABILITIES = { check: true, download: true, install: true, schedule: true, rollback: true, inventory: true, cleanup: true }

beforeEach(() => {
  mocks.status.data = status()
  mocks.status.isPending = false
  mocks.status.isError = false
  mocks.install.data = undefined
  mocks.install.isError = false
  mocks.install.error = null
  mocks.history.data = []
  mocks.history.isError = false
  mocks.cleanup.data = storagePreview()
  mocks.cleanup.isError = false
  mocks.cleanup.error = null
  vi.clearAllMocks()
})

describe('UpdatesPanel', () => {
  it('首屏展示当前版本、目标版本与更新说明', () => {
    renderPanel()

    expect(screen.getAllByText('0.2.2').length).toBeGreaterThan(0)
    expect(screen.getByText('0.3.0')).toBeInTheDocument()
    expect(screen.getByText('有新版本可用')).toBeInTheDocument()
    expect(screen.getByText(/当前版本 0\.2\.2，可更新至 0\.3\.0/)).toBeInTheDocument()
    expect(screen.getAllByText('修复稳定性问题。').length).toBeGreaterThan(0)
  })

  it('无安装能力时不提供立即更新，只说明原因', () => {
    renderPanel()

    expect(screen.queryByRole('button', { name: '立即更新' })).not.toBeInTheDocument()
    expect(screen.getByText(/当前环境不支持应用内安装/)).toBeInTheDocument()
  })

  it('安装能力可用时一次点击立即更新，不再二次确认', () => {
    mocks.status.data = status({ capabilities: INSTALL_CAPABILITIES })
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: '立即更新' }))

    expect(mocks.actions.update.mutate).toHaveBeenCalledWith('immediate', expect.anything())
    expect(screen.queryByText('确认安装并重启')).not.toBeInTheDocument()
  })

  it('空闲后更新只授权一次，由后台等待采集结束', () => {
    mocks.status.data = status({ capabilities: INSTALL_CAPABILITIES })
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: '空闲后更新' }))

    expect(mocks.actions.update.mutate).toHaveBeenCalledWith('idle', expect.anything())
  })

  it('更新中展示统一下载进度并可取消', () => {
    mocks.status.data = status({
      capabilities: INSTALL_CAPABILITIES,
      download: downloadState({ active: true, status: 'downloading', version: '0.3.0', downloaded: 512, total: 1024 }),
      flow: flow({ active: true, stage: 'downloading', version: '0.3.0', progress: 50, message: '正在下载并校验更新包' }),
    })
    renderPanel()

    const bars = screen.getAllByRole('progressbar', { name: '更新包下载进度' })
    expect(bars[0]).toHaveAttribute('aria-valuenow', '50')
    fireEvent.click(screen.getByRole('button', { name: '取消更新' }))

    expect(mocks.actions.cancelUpdate.mutate).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/可以关闭页面，后台会继续完成更新/)).toBeInTheDocument()
  })

  it('等待空闲时展示真实原因，安装开始后不允许取消', () => {
    const waiting = status({
      capabilities: INSTALL_CAPABILITIES,
      download: downloadState({ status: 'completed', version: '0.3.0', asset_kind: 'windows_installer', asset_name: 'MenuCapture-0.3.0.exe', downloaded: 1024, total: 1024 }),
      flow: flow({ active: true, stage: 'waiting', version: '0.3.0', message: '采集或人工标注正在执行' }),
    })
    const { rerender } = renderPanel()
    mocks.status.data = waiting
    rerender(
      <ToastProvider>
        <UpdatesPanel />
      </ToastProvider>,
    )

    expect(screen.getByText(/采集或人工标注正在执行/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '取消更新' })).toBeInTheDocument()

    mocks.status.data = status({
      capabilities: INSTALL_CAPABILITIES,
      flow: flow({ active: true, stage: 'restarting', version: '0.3.0', message: '正在重启服务' }),
    })
    rerender(
      <ToastProvider>
        <UpdatesPanel />
      </ToastProvider>,
    )

    expect(screen.queryByRole('button', { name: '取消更新' })).not.toBeInTheDocument()
    expect(screen.getByText(/安装已开始，无法取消/)).toBeInTheDocument()
  })

  it('更新失败时展示真实错误并提供重试', () => {
    mocks.status.data = status({
      capabilities: INSTALL_CAPABILITIES,
      flow: flow({ stage: 'failed', version: '0.3.0', error: '健康检查超时' }),
    })
    renderPanel()

    expect(screen.getByText('更新失败')).toBeInTheDocument()
    expect(screen.getByText('健康检查超时')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试更新' }))

    expect(mocks.actions.update.mutate).toHaveBeenCalledWith('immediate', expect.anything())
  })

  it('高级设置中保留手动检查与下载入口', () => {
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: '手动检查更新' }))
    fireEvent.click(screen.getByRole('button', { name: '仅下载更新包' }))

    expect(mocks.actions.check.mutate).toHaveBeenCalledTimes(1)
    expect(mocks.actions.download.mutate).toHaveBeenCalledTimes(1)
  })

  it('下载中显示真实进度并允许取消下载', () => {
    mocks.status.data = status({
      download: downloadState({
        active: true,
        status: 'downloading',
        version: '0.3.0',
        asset_name: 'MenuCapture-0.3.0.exe',
        downloaded: 512,
        total: 1024,
      }),
    })
    renderPanel()

    const bars = screen.getAllByRole('progressbar', { name: '更新包下载进度' })
    expect(bars[0]).toHaveAttribute('aria-valuenow', '50')
    fireEvent.click(screen.getByRole('button', { name: '取消下载' }))

    expect(mocks.actions.cancelDownload.mutate).toHaveBeenCalledTimes(1)
  })

  it('下载失败后可继续续传', () => {
    mocks.status.data = status({
      download: downloadState({
        status: 'failed',
        version: '0.3.0',
        asset_name: 'MenuCapture-0.3.0.exe',
        error: '网络中断',
        resumable: true,
      }),
    })
    renderPanel()

    expect(screen.getByText('网络中断')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '继续下载' })).toBeInTheDocument()
  })

  it('已下载当前版本时不再提供下载入口', () => {
    mocks.status.data = status({
      download: downloadState({
        status: 'completed',
        version: '0.3.0',
        asset_kind: 'windows_installer',
        asset_name: 'MenuCapture-0.3.0.exe',
        downloaded: 1024,
        total: 1024,
      }),
    })
    renderPanel()

    expect(screen.getByText(/已下载 MenuCapture-0\.3\.0\.exe 并通过 SHA-256 校验/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '仅下载更新包' })).not.toBeInTheDocument()
  })

  it('已下载旧版本不会阻止下载新版本', () => {
    mocks.status.data = status({
      download: downloadState({
        status: 'completed',
        version: '0.2.9',
        asset_kind: 'windows_installer',
        asset_name: 'MenuCapture-0.2.9.exe',
        downloaded: 1024,
        total: 1024,
      }),
    })
    renderPanel()

    expect(screen.getByText(/已下载 0\.2\.9；当前最新为 0\.3\.0/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '仅下载更新包' })).toBeInTheDocument()
  })

  it('安装执行器未启用时只在高级设置说明原因', () => {
    mocks.install.data = installState()
    renderPanel()

    expect(screen.getByText(/显式启用后才能使用/)).toBeInTheDocument()
  })

  it('存储治理仍需确认后才提交删除', () => {
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: '清理可清理项' }))
    fireEvent.click(screen.getByRole('button', { name: '确认清理' }))

    expect(mocks.actions.cleanup.mutate).toHaveBeenCalledWith(['releases'], expect.anything())
  })

  it('更新记录使用真实动作名称', () => {
    mocks.history.data = [
      { time: '2026-11-24T09:00:00Z', action: 'update', result: 'ok', message: '已授权一次完成下载与安装', version: '0.3.0', platform: 'windows', role: 'worker' },
    ]
    renderPanel()

    expect(screen.getByText('一次授权更新')).toBeInTheDocument()
  })

  it('状态读取失败时提供重试，而不是伪造版本', () => {
    mocks.status.data = undefined
    mocks.status.isError = true
    mocks.status.error = new Error('连接失败') as never
    renderPanel()

    expect(screen.getByText('更新状态读取失败')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })
})
