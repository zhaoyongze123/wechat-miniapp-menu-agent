import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from '../../api/client'
import { fetchActiveTasks } from '../task-center/api'
import { NotificationRuntime } from './NotificationRuntime'

const { push } = vi.hoisted(() => ({ push: vi.fn() }))

vi.mock('../../api/client', () => ({ apiFetch: vi.fn() }))
vi.mock('../task-center/api', () => ({ fetchActiveTasks: vi.fn(), fetchTaskDetail: vi.fn() }))
vi.mock('../../components/ToastProvider', () => ({ useToast: () => ({ push }) }))

const manualState = {
  device_id: 'device-1',
  device_name: '采集电脑',
  run_name: '测试任务',
  run_status: 'running',
  manual: {
    waiting: true,
    wait_id: 'wait-1',
    run_id: 'run-1',
    decision: null,
    store_name: '测试门店',
    task_id: 'task-1',
  },
}

const started = vi.fn()

class FakeOscillator {
  type = 'sine'
  frequency = { setValueAtTime: vi.fn() }
  onended: (() => void) | null = null
  connect = vi.fn()
  start = started
  stop = vi.fn()
  disconnect = vi.fn()
}

class FakeGain {
  gain = { setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn() }
  connect = vi.fn()
  disconnect = vi.fn()
}

class FakeAudioContext {
  state: 'suspended' | 'running' = 'suspended'
  currentTime = 0
  destination = {}
  createOscillator() { return new FakeOscillator() }
  createGain() { return new FakeGain() }
  resume() { this.state = 'running'; return Promise.resolve() }
}

beforeEach(() => {
  localStorage.clear()
  started.mockClear()
  push.mockClear()
  vi.mocked(apiFetch).mockReset()
  vi.mocked(fetchActiveTasks).mockReset()
  vi.mocked(apiFetch).mockResolvedValue(manualState as never)
  // 远程控制页是受限路由：只应访问 /api/manual/state。
  window.history.replaceState({}, '', '/remote-control')
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
  ;(window as unknown as { AudioContext: unknown }).AudioContext = FakeAudioContext
})

afterEach(() => {
  cleanup()
  delete (window as unknown as { AudioContext?: unknown }).AudioContext
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
  window.history.replaceState({}, '', '/')
})

function renderRuntime() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <NotificationRuntime />
    </QueryClientProvider>,
  )
}

describe('NotificationRuntime 远程控制提醒', () => {
  it('远程页不请求受限接口，后台无桌面通知权限也能在音频激活后提醒且只提醒一次', async () => {
    renderRuntime()

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/manual/state', expect.any(Object)))
    // 音频未激活时播放失败，不登记声音事件，也不依赖桌面通知权限。
    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1))
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(started).not.toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
    expect(apiFetch).not.toHaveBeenCalledWith('/api/shared/status?include_history=false', expect.anything())
    expect(fetchActiveTasks).not.toHaveBeenCalled()

    // 一次用户手势激活 Web Audio 后，后台等待事件立即补发声音。
    await act(async () => {
      fireEvent.pointerDown(window)
      await Promise.resolve()
    })
    await waitFor(() => expect(started).toHaveBeenCalledTimes(1))
    const ledger = JSON.parse(localStorage.getItem('menu-capture.notification-events.v1') ?? '{}') as Record<string, string>
    expect(ledger['sound:manual-wait:device-1:run-1:wait-1']).toBeTruthy()

    // 同一事件在后续轮询/偏好事件中不重复提醒；后台也不弹出应用内提示。
    await act(async () => {
      window.dispatchEvent(new Event('menu-capture-notification-preferences'))
      await Promise.resolve()
    })
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(started).toHaveBeenCalledTimes(1)
    expect(push).not.toHaveBeenCalled()
  })
})
