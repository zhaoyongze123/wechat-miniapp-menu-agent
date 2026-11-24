import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '../../components/ToastProvider'
import type { UpdateStatus } from './types'

const mocks = vi.hoisted(() => ({
  status: { isPending: false, isError: false, data: undefined as UpdateStatus | undefined, error: null, refetch: vi.fn() },
  history: { isError: false, data: [], error: null, refetch: vi.fn() },
  actions: {
    check: { mutate: vi.fn(), isPending: false },
    savePreferences: { mutate: vi.fn(), isPending: false },
  },
}))

vi.mock('./hooks', () => ({
  useUpdateStatus: () => mocks.status,
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

function status(overrides: Partial<UpdateStatus> = {}): UpdateStatus {
  return {
    role: 'worker',
    repository: 'zhaoyongze123/wechat-miniapp-menu-agent',
    platform: 'windows',
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
    capabilities: { check: true, download: false, install: false, rollback: false, inventory: true },
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

beforeEach(() => {
  mocks.status.data = status()
  mocks.status.isPending = false
  mocks.status.isError = false
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
    expect(screen.getByText(/当前阶段仅提供版本发现与发布清单校验/)).toBeInTheDocument()
  })

  it('手动检查调用真实动作，不伪造下载或安装入口', () => {
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: '手动检查更新' }))

    expect(mocks.actions.check.mutate).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: /下载/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /安装/ })).not.toBeInTheDocument()
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
