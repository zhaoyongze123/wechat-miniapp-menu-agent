import { beforeEach, describe, expect, it } from 'vitest'
import { deviceStateOf } from '../hub/format'
import {
  clearResolvedNotifications,
  isNotificationMuted,
  listNotifications,
  markAllNotificationsRead,
  pushNotification,
  resolveNotification,
  setNotificationMuteMinutes,
} from './store'

describe('NotificationStore 通知中心存储与自愈', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('同 dedupeKey 折叠计数并在冷静期内抑制重复打扰', () => {
    const base = Date.now()
    const first = pushNotification(
      {
        dedupeKey: 'hub-device:w1',
        kind: 'device_offline',
        category: 'device',
        severity: 'warning',
        title: '采集电脑已离线',
        body: '设备 A 持续未收到心跳',
        cooldownMs: 300_000,
      },
      base,
    )
    expect(first.shouldAlert).toBe(true)
    expect(first.item.count).toBe(1)

    const second = pushNotification(
      {
        dedupeKey: 'hub-device:w1',
        kind: 'device_offline',
        category: 'device',
        severity: 'warning',
        title: '采集电脑已离线',
        body: '设备 A 持续未收到心跳',
        cooldownMs: 300_000,
      },
      base + 60_000,
    )
    expect(second.shouldAlert).toBe(false)
    expect(second.item.count).toBe(2)
    expect(listNotifications(base + 60_000)).toHaveLength(1)
  })

  it('状态恢复时自动标记已恢复并消除未读红点', () => {
    const base = Date.now()
    pushNotification(
      {
        dedupeKey: 'hub-device:w1',
        kind: 'device_offline',
        category: 'device',
        severity: 'warning',
        title: '采集电脑已离线',
        body: '设备 A 离线',
      },
      base,
    )
    expect(resolveNotification('hub-device:w1', '设备 A 已恢复在线。', base + 1_000)).toBe(true)
    const items = listNotifications(base + 1_000)
    expect(items[0]?.resolvedAt).toBe(base + 1_000)
    expect(items[0]?.readAt).toBe(base + 1_000)
    expect(items[0]?.body).toBe('设备 A 已恢复在线。')

    clearResolvedNotifications(base + 1_000)
    expect(listNotifications(base + 1_000)).toHaveLength(0)
  })

  it('支持快捷免打扰与全部已读', () => {
    const base = Date.now()
    pushNotification(
      {
        dedupeKey: 'task:capture:r1',
        kind: 'task_complete',
        category: 'task',
        severity: 'success',
        title: '采集任务已完成',
        body: '已截图 10 家',
      },
      base,
    )
    markAllNotificationsRead(base + 1_000)
    expect(listNotifications(base + 1_000)[0]?.readAt).toBe(base + 1_000)

    const until = setNotificationMuteMinutes(30)
    expect(until).toBeGreaterThan(Date.now())
    expect(isNotificationMuted()).toBe(true)
    setNotificationMuteMinutes(0)
    expect(isNotificationMuted()).toBe(false)
  })

  it('deviceStateOf 优先采用服务端 connection_state', () => {
    expect(deviceStateOf({ connection_state: 'degraded', last_seen: '2020-01-01T00:00:00Z' }).key).toBe('degraded')
    expect(deviceStateOf({ connection_state: 'online', last_seen: '2020-01-01T00:00:00Z' }).key).toBe('online')
    expect(deviceStateOf({ connection_state: 'offline', last_seen: new Date().toISOString() }).key).toBe('offline')
  })
})
