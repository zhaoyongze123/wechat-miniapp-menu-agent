import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../../components/ToastProvider'
import type { UpdateDownloadState, UpdateInstallState, UpdateStatus } from './types'

const mocks = vi.hoisted(() => ({
  status: { isPending: false, isError: false, data: undefined as UpdateStatus | undefined, error: null, refetch: vi.fn() },
  install: {
    isError: false,
    error: null as Error | null,
    data: undefined as UpdateInstallState | undefined,
  },
  history: { isError: false, data: [], error: null, refetch: vi.fn() },
  actions: {
    check: { mutate: vi.fn(), isPending: false },
    savePreferences: { mutate: vi.fn(), isPending: false },
    download: { mutate: vi.fn(), isPending: false },
    cancelDownload: { mutate: vi.fn(), isPending: false },
    install: { mutate: vi.fn(), isPending: false },
  },
}))

vi.mock('./hooks', () => ({
  useUpdateStatus: () => mocks.status,
  useUpdateInstall: () => mocks.install,
  useUpdateHistory: () => mocks.history,
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

function status(overrides: Partial<UpdateStatus> = {}): UpdateStatus {
  return {
    role: 'worker',
    repository: 'zhaoyongze123/wechat-miniapp-menu-agent',
    platform: 'windows',
    arch: 'x64',
    current_version: '0.2.2',
    channel: 'stable',
    preferences: { auto_check: true, channel: 'stable' },
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
    capabilities: { check: true, download: true, install: false, rollback: false, inventory: true },
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

beforeEach(() => {
  mocks.status.data = status()
  mocks.status.isPending = false
  mocks.status.isError = false
  mocks.install.data = undefined
  mocks.install.isError = false
  mocks.install.error = null
  mocks.history.data = []
  mocks.history.isError = false
  vi.clearAllMocks()
})

describe('UpdatesPanel', () => {
  it('展示当前版本、最新版本、发布说明与资产校验信息', () => {
    renderPanel()

    expect(screen.getAllByText('0.2.2').length).toBeGreaterThan(0)
    expect(screen.getByText('0.3.0')).toBeInTheDocument()
    expect(screen.getByText('发现可更新版本')).toBeInTheDocument()
    expect(screen.getByText('修复稳定性问题。')).toBeInTheDocument()
    expect(screen.getByText(/MenuCapture-0.3.0.exe/)).toBeInTheDocument()
    expect(screen.getByText(/下载与安装都以服务端真实能力为准/)).toBeInTheDocument()
  })

  it('手动检查调用真实动作，下载入口不伪造安装能力', () => {
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: '手动检查更新' }))

    expect(mocks.actions.check.mutate).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: '下载更新包' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /安装/ })).not.toBeInTheDocument()
  })

  it('点击下载调用真实下载动作', () => {
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: '下载更新包' }))

    expect(mocks.actions.download.mutate).toHaveBeenCalledTimes(1)
  })

  it('下载中显示真实进度并允许取消', () => {
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

    expect(screen.getByRole('progressbar', { name: '更新包下载进度' })).toHaveAttribute('aria-valuenow', '50')
    expect(screen.getByText(/50%/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '取消下载' }))

    expect(mocks.actions.cancelDownload.mutate).toHaveBeenCalledTimes(1)
  })

  it('下载失败后展示原因并允许继续续传', () => {
    mocks.status.data = status({
      download: downloadState({
        status: 'failed',
        version: '0.3.0',
        asset_name: 'MenuCapture-0.3.0.exe',
        downloaded: 300,
        total: 1024,
        error: '网络中断',
        resumable: true,
      }),
    })
    renderPanel()

    expect(screen.getByText('网络中断')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '继续下载' })).toBeInTheDocument()
  })

  it('下载完成后不再重复提供下载入口', () => {
    mocks.status.data = status({
      download: downloadState({
        status: 'completed',
        version: '0.3.0',
        asset_name: 'MenuCapture-0.3.0.exe',
        downloaded: 1024,
        total: 1024,
        path: 'D:\\menu-agent\\runtime\\data\\updates\\0.3.0\\MenuCapture-0.3.0.exe',
      }),
    })
    renderPanel()

    expect(screen.getByText(/已下载 MenuCapture-0.3.0.exe 并通过 SHA-256 校验/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /下载/ })).not.toBeInTheDocument()
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
        path: 'D:\\menu-agent\\runtime\\data\\updates\\0.2.9\\MenuCapture-0.2.9.exe',
      }),
    })
    renderPanel()

    expect(screen.getByText(/已下载 0\.2\.9；当前最新为 0\.3\.0/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '下载更新包' })).toBeInTheDocument()
  })

  it('下载完成且安装能力可用时，确认后才提交真实安装请求', () => {
    mocks.status.data = status({
      capabilities: { check: true, download: true, install: true, rollback: true, inventory: true },
      download: downloadState({
        status: 'completed',
        version: '0.3.0',
        asset_kind: 'windows_installer',
        asset_name: 'MenuCapture-0.3.0.exe',
        downloaded: 1024,
        total: 1024,
        path: 'D:\\menu-agent\\runtime\\data\\updates\\0.3.0\\MenuCapture-0.3.0.exe',
      }),
    })
    mocks.install.data = installState({ supported: true, reason: '' })
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: '安装并重启' }))
    expect(mocks.actions.install.mutate).not.toHaveBeenCalled()
    expect(screen.getByText(/安装由独立进程静默完成/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '确认安装并重启' }))
    expect(mocks.actions.install.mutate).toHaveBeenCalledWith('0.3.0', expect.anything())
  })

  it('安装能力未启用时只说明原因，不提供安装按钮', () => {
    mocks.status.data = status({
      download: downloadState({
        status: 'completed',
        version: '0.3.0',
        asset_kind: 'windows_installer',
        asset_name: 'MenuCapture-0.3.0.exe',
        downloaded: 1024,
        total: 1024,
        path: 'D:\\menu-agent\\runtime\\data\\updates\\0.3.0\\MenuCapture-0.3.0.exe',
      }),
    })
    mocks.install.data = installState()
    renderPanel()

    expect(screen.getByText('安装执行器未启用')).toBeInTheDocument()
    expect(screen.getByText(/显式启用后才能使用/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '安装并重启' })).not.toBeInTheDocument()
  })

  it('安装进行中展示真实阶段并禁用重复提交', () => {
    mocks.status.data = status({
      capabilities: { check: true, download: true, install: true, rollback: true, inventory: true },
      download: downloadState({
        status: 'completed',
        version: '0.3.0',
        asset_kind: 'windows_installer',
        asset_name: 'MenuCapture-0.3.0.exe',
        downloaded: 1024,
        total: 1024,
      }),
    })
    mocks.install.data = installState({
      supported: true,
      reason: '',
      active: true,
      status: 'installing',
      target_version: '0.3.0',
      previous_version: '0.2.2',
      message: '安装包校验通过（签名状态：valid），正在运行安装程序',
    })
    renderPanel()

    expect(screen.getByText(/正在安装/)).toBeInTheDocument()
    expect(screen.getByText(/目标 0\.3\.0/)).toBeInTheDocument()
    expect(screen.getByText(/签名状态：valid/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '安装并重启' })).not.toBeInTheDocument()
  })

  it('版本清点显示保护状态与清理候选，但不提供删除按钮', () => {
    renderPanel()

    expect(screen.getByText('D:\\menu-agent')).toBeInTheDocument()
    expect(screen.getAllByText('受保护')).toHaveLength(3)
    expect(screen.getByText(/可清理候选 1 个：0.1.9/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /删除/ })).not.toBeInTheDocument()
  })

  it('非 Windows 或缺少版本目录时如实说明不支持', () => {
    mocks.status.data = status({
      inventory: {
        supported: false,
        reason: '仅 Windows 采集端支持程序版本目录清点',
        releases: [],
        cleanup_candidates: [],
      },
    })
    renderPanel()

    expect(screen.getByText('当前环境不支持程序版本清点')).toBeInTheDocument()
    expect(screen.getByText(/仅 Windows 采集端支持/)).toBeInTheDocument()
  })
})
