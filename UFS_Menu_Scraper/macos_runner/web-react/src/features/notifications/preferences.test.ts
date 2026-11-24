import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  claimNotificationEvent,
  DEFAULT_NOTIFICATION_PREFERENCES,
  manualWaitEventId,
  readNotificationPreferences,
  saveNotificationPreferences,
  transitionNotificationState,
} from './preferences'

const values = new Map<string, string>()
const storage = {
  getItem: (key: string) => values.get(key) ?? null,
  setItem: (key: string, value: string) => { values.set(key, String(value)) },
  removeItem: (key: string) => { values.delete(key) },
  clear: () => values.clear(),
  key: (index: number) => [...values.keys()][index] ?? null,
  get length() { return values.size },
}

beforeEach(() => {
  values.clear()
  vi.stubGlobal('localStorage', storage)
})
afterEach(() => vi.unstubAllGlobals())

describe('浏览器通知偏好和事件身份', () => {
  it('重复等待默认关闭，桌面通知需明确启用', () => {
    expect(readNotificationPreferences()).toMatchObject({
      repeatWaitMinutes: 0,
      desktop: false,
      sound: true,
    })
  })

  it('保存当前浏览器偏好，旧/异常字段不破坏默认项', () => {
    saveNotificationPreferences({
      ...DEFAULT_NOTIFICATION_PREFERENCES,
      volume: 0.8,
      repeatWaitMinutes: 5,
      enabled: { ...DEFAULT_NOTIFICATION_PREFERENCES.enabled, sync_failure: false },
    })
    expect(readNotificationPreferences()).toMatchObject({
      volume: 0.8,
      repeatWaitMinutes: 5,
      enabled: { manual_wait: true, sync_failure: false },
    })
    localStorage.setItem('menu-capture.notification-preferences.v1', JSON.stringify({ volume: 3, repeatWaitMinutes: 99 }))
    expect(readNotificationPreferences()).toMatchObject({ volume: 1, repeatWaitMinutes: 0, enabled: DEFAULT_NOTIFICATION_PREFERENCES.enabled })
  })

  it('人工等待身份包含 device_id、run_id 和 wait_id', () => {
    const first = manualWaitEventId('device-1', 'run-1', 'wait-1')
    expect(manualWaitEventId('device-1', 'run-1', 'wait-1')).toBe(first)
    expect(manualWaitEventId('device-2', 'run-1', 'wait-1')).not.toBe(first)
    expect(manualWaitEventId('device-1', 'run-2', 'wait-1')).not.toBe(first)
    expect(manualWaitEventId('device-1', 'run-1', 'wait-2')).not.toBe(first)
  })

  it('相同业务事件只可领取一次，新的 wait_id 可独立领取', async () => {
    const event = manualWaitEventId('device-1', 'run-1', 'wait-1')
    expect(await claimNotificationEvent(event)).toBe(true)
    expect(await claimNotificationEvent(event)).toBe(false)
    expect(await claimNotificationEvent(manualWaitEventId('device-1', 'run-1', 'wait-2'))).toBe(true)
  })

  it('任务/连接状态转换保留首次变化时间，只有新状态产生新转换', async () => {
    const initial = await transitionNotificationState('test-worker', 'healthy')
    const failed = await transitionNotificationState('test-worker', 'offline')
    const stillFailed = await transitionNotificationState('test-worker', 'offline')
    const recovered = await transitionNotificationState('test-worker', 'healthy')
    expect(initial.previous).toBeNull()
    expect(failed.previous).toBe('healthy')
    expect(stillFailed.previous).toBe('offline')
    expect(stillFailed.changedAt).toBe(failed.changedAt)
    expect(recovered.previous).toBe('offline')
    expect(recovered.changedAt).toBeGreaterThan(failed.changedAt)
  })
})
