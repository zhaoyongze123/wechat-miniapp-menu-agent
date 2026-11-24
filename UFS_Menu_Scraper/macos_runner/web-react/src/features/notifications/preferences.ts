export const NOTIFICATION_PREFERENCES_KEY = 'menu-capture.notification-preferences.v1'
const NOTIFICATION_EVENTS_KEY = 'menu-capture.notification-events.v1'
const MAX_EVENTS = 300
const volatileEvents = new Set<string>()
const volatileStates = new Map<string, { state: string; changedAt: number }>()
const volatileWaitStarts = new Map<string, number>()
let audioContext: AudioContext | null = null

export type NotificationEventKind = 'manual_wait' | 'task_complete' | 'materials_complete' | 'abnormal' | 'sync_failure' | 'device_offline'

export interface NotificationPreferences {
  enabled: Record<NotificationEventKind, boolean>
  desktop: boolean
  sound: boolean
  volume: number
  repeatWaitMinutes: 0 | 1 | 5
}

export const DEFAULT_NOTIFICATION_PREFERENCES: NotificationPreferences = {
  enabled: {
    manual_wait: true,
    task_complete: true,
    materials_complete: true,
    abnormal: true,
    sync_failure: true,
    device_offline: true,
  },
  desktop: false,
  sound: true,
  volume: 0.45,
  repeatWaitMinutes: 0,
}

/** 等待事件以设备、采集轮次和服务端 wait_id 共同标识，名称变化不影响去重。 */
export function manualWaitEventId(deviceId: string, runId: string, waitId: string): string {
  return `manual-wait:${deviceId}:${runId}:${waitId}`
}

export function readNotificationPreferences(): NotificationPreferences {
  try {
    const raw = localStorage.getItem(NOTIFICATION_PREFERENCES_KEY)
    if (!raw) return DEFAULT_NOTIFICATION_PREFERENCES
    const saved = JSON.parse(raw) as Partial<NotificationPreferences>
    const enabled = { ...DEFAULT_NOTIFICATION_PREFERENCES.enabled }
    for (const key of Object.keys(enabled) as NotificationEventKind[]) {
      const value = saved.enabled?.[key]
      if (typeof value === 'boolean') enabled[key] = value
    }
    return {
      enabled,
      desktop: saved.desktop === true,
      sound: saved.sound !== false,
      volume: clamp(Number(saved.volume ?? DEFAULT_NOTIFICATION_PREFERENCES.volume), 0, 1),
      repeatWaitMinutes: saved.repeatWaitMinutes === 1 || saved.repeatWaitMinutes === 5 ? saved.repeatWaitMinutes : 0,
    }
  } catch {
    return DEFAULT_NOTIFICATION_PREFERENCES
  }
}

export function saveNotificationPreferences(preferences: NotificationPreferences): void {
  try {
    localStorage.setItem(NOTIFICATION_PREFERENCES_KEY, JSON.stringify(preferences))
    window.dispatchEvent(new Event('menu-capture-notification-preferences'))
  } catch {
    // 浏览器禁止本地存储时，当前界面仍可工作；刷新后偏好不会保留。
  }
}

export interface NotificationEvent {
  id: string
  kind: NotificationEventKind
  title: string
  body: string
  route?: string
}

/** 多标签页通过 Web Locks 串行发放；不支持时以本地存储的先写校验降级。 */
export async function claimNotificationEvent(id: string): Promise<boolean> {
  if (typeof navigator !== 'undefined' && navigator.locks?.request) {
    return navigator.locks.request('menu-capture-notifications', { mode: 'exclusive' }, () => claimEventWithoutLock(id))
  }
  return claimEventWithoutLock(id)
}

export async function transitionNotificationState(key: string, next: string): Promise<{ previous: string | null; changedAt: number }> {
  if (typeof navigator !== 'undefined' && navigator.locks?.request) {
    return navigator.locks.request(`menu-capture-notification-state:${key}`, { mode: 'exclusive' }, () => writeTransition(key, next))
  }
  return writeTransition(key, next)
}

export async function getOrCreateWaitStart(id: string): Promise<number> {
  const key = `menu-capture.notification-wait-start.${id}`
  const readOrCreate = () => {
    const existing = Number(safeGet(key))
    if (existing > 0) return existing
    const inMemory = volatileWaitStarts.get(key)
    if (inMemory) return inMemory
    const value = Date.now()
    if (!safeSet(key, String(value))) volatileWaitStarts.set(key, value)
    return value
  }
  if (typeof navigator !== 'undefined' && navigator.locks?.request) {
    return navigator.locks.request(`menu-capture-notification-wait:${id}`, { mode: 'exclusive' }, readOrCreate)
  }
  return readOrCreate()
}

export async function activateNotificationAudio(): Promise<boolean> {
  const AudioContextConstructor = window.AudioContext
  if (!AudioContextConstructor) return false
  try {
    if (!audioContext || audioContext.state === 'closed') audioContext = new AudioContextConstructor()
    const context = audioContext
    if (context.state === 'running') return true
    // 自动播放被阻止时 resume 可能一直 pending；不能让通知领取锁永远等待。
    let timeout = 0
    try {
      await Promise.race([context.resume(), new Promise<void>((resolve) => { timeout = window.setTimeout(resolve, 1000) })])
    } finally { window.clearTimeout(timeout) }
    const ready = (context.state as AudioContextState) === 'running'
    if (ready) window.dispatchEvent(new Event('menu-capture-notification-preferences'))
    return ready
  } catch { return false }
}

/** 声音与视觉提醒独立去重；只有音频节点成功启动才记为已发放。 */
const pendingSounds = new Set<string>()
export async function deliverNotificationSound(id: string, play: () => Promise<boolean>): Promise<boolean> {
  const key = `sound:${id}`
  const deliver = async () => {
    if (readEventLedger()[key] || volatileEvents.has(key) || pendingSounds.has(key)) return false
    pendingSounds.add(key)
    try {
      if (!(await play())) return false
      claimEventWithoutLock(key)
      return true
    } finally { pendingSounds.delete(key) }
  }
  if (typeof navigator !== 'undefined' && navigator.locks?.request) {
    return navigator.locks.request('menu-capture-notifications', { mode: 'exclusive' }, deliver)
  }
  return deliver()
}

export async function playNotificationSound(volume: number, kind: NotificationEventKind = 'task_complete', userInitiated = true): Promise<boolean> {
  if (volume <= 0) return false
  try {
    if (userInitiated && !(await activateNotificationAudio())) return false
    const context = audioContext
    if (!context || context.state !== 'running') return false
    const oscillator = context.createOscillator()
    const gain = context.createGain()
    const now = context.currentTime
    const isUrgent = kind === 'manual_wait' || kind === 'abnormal' || kind === 'sync_failure'
    oscillator.type = 'sine'
    oscillator.frequency.setValueAtTime(isUrgent ? 740 : 587, now)
    oscillator.frequency.setValueAtTime(isUrgent ? 587 : 740, now + 0.13)
    gain.gain.setValueAtTime(0, now)
    gain.gain.linearRampToValueAtTime(Math.max(0.001, Math.min(1, volume)) * 0.16, now + 0.025)
    gain.gain.setValueAtTime(Math.max(0.001, Math.min(1, volume)) * 0.16, now + 0.16)
    gain.gain.linearRampToValueAtTime(0, now + 0.22)
    oscillator.connect(gain)
    gain.connect(context.destination)
    oscillator.start(now)
    oscillator.stop(now + 0.23)
    oscillator.onended = () => { oscillator.disconnect(); gain.disconnect() }
    return true
  } catch {
    return false
  }
}

function claimEventWithoutLock(id: string): boolean {
  const now = Date.now()
  const ledger = readEventLedger()
  if (ledger[id] || volatileEvents.has(id)) return false
  const owner = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `${now}-${Math.random()}`
  const claim = `${now}:${owner}`
  ledger[id] = claim
  const entries = Object.entries(ledger).sort((a, b) => claimTime(b[1]) - claimTime(a[1])).slice(0, MAX_EVENTS)
  const next = Object.fromEntries(entries)
  if (!safeSet(NOTIFICATION_EVENTS_KEY, JSON.stringify(next))) {
    volatileEvents.add(id)
    return true
  }
  // Cross-tab fallback: if another tab wrote first, only the owner that remains in storage dispatches.
  return readEventLedger()[id] === claim
}

function writeTransition(key: string, next: string): { previous: string | null; changedAt: number } {
  const storageKey = `menu-capture.notification-state.${key}`
  let previous: string | null = null
  let changedAt = Date.now()
  const inMemory = volatileStates.get(storageKey)
  if (inMemory) {
    previous = inMemory.state
    changedAt = previous === next ? inMemory.changedAt : Math.max(changedAt, inMemory.changedAt + 1)
    volatileStates.set(storageKey, { state: next, changedAt })
    return { previous, changedAt }
  }
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) ?? 'null') as { state?: string; changedAt?: number } | null
    previous = typeof value?.state === 'string' ? value.state : null
    const storedAt = Number(value?.changedAt) || 0
    if (previous === next && storedAt > 0) changedAt = storedAt
    else {
      // 即时恢复又失败可能落在同一毫秒；递增逻辑时钟保证每次状态边缘都有不同事件身份。
      changedAt = Math.max(changedAt, storedAt + 1)
      if (!safeSet(storageKey, JSON.stringify({ state: next, changedAt }))) {
        volatileStates.set(storageKey, { state: next, changedAt })
      }
    }
  } catch {
    // Storage may be disabled; this tab still gets transition semantics in memory only.
    const fallback = volatileStates.get(storageKey)
    previous = fallback?.state ?? null
    changedAt = fallback && previous === next ? fallback.changedAt : Math.max(changedAt, (fallback?.changedAt ?? 0) + 1)
    volatileStates.set(storageKey, { state: next, changedAt })
  }
  return { previous, changedAt }
}

function readEventLedger(): Record<string, string> {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(NOTIFICATION_EVENTS_KEY) ?? '{}')
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
    return Object.fromEntries(Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === 'string'))
  } catch {
    return {}
  }
}

function claimTime(claim: string): number {
  const time = Number(claim.split(':', 1)[0])
  return Number.isFinite(time) ? time : 0
}

function safeGet(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}

function safeSet(key: string, value: string): boolean {
  try {
    localStorage.setItem(key, value)
    return true
  } catch {
    return false
  }
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Number.isFinite(value) ? value : min))
}
