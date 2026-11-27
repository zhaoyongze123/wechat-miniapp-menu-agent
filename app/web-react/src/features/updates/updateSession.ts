import type { UpdateFlowStage } from './types'

const STORAGE_KEY = 'menu-capture.active-update-session'
const SESSION_EVENT = 'menu-capture:update-session-change'
/** 超过 30 分钟的残留标记自动失效，避免异常中断后长期锁住界面。 */
const SESSION_TTL_MS = 30 * 60_000

export interface ActiveUpdateSession {
  targetVersion: string
  fromVersion: string
  role: string
  stage: UpdateFlowStage
  message: string
  progress: number
  startedAt: number
  updatedAt: number
}

function notifySessionChange() {
  try {
    window.dispatchEvent(new Event(SESSION_EVENT))
  } catch {
    // 非浏览器环境忽略事件派发。
  }
}

export function readActiveUpdateSession(): ActiveUpdateSession | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<ActiveUpdateSession>
    if (!parsed || typeof parsed.startedAt !== 'number' || typeof parsed.updatedAt !== 'number') {
      return null
    }
    if (Date.now() - parsed.updatedAt > SESSION_TTL_MS) {
      window.localStorage.removeItem(STORAGE_KEY)
      return null
    }
    return {
      targetVersion: typeof parsed.targetVersion === 'string' ? parsed.targetVersion : '',
      fromVersion: typeof parsed.fromVersion === 'string' ? parsed.fromVersion : '',
      role: typeof parsed.role === 'string' ? parsed.role : '',
      stage: (parsed.stage as UpdateFlowStage) || 'installing',
      message: typeof parsed.message === 'string' ? parsed.message : '',
      progress: typeof parsed.progress === 'number' ? parsed.progress : 0,
      startedAt: parsed.startedAt,
      updatedAt: parsed.updatedAt,
    }
  } catch {
    return null
  }
}

export function hasActiveUpdateSession(): boolean {
  return readActiveUpdateSession() !== null
}

export function writeActiveUpdateSession(
  patch: Partial<Omit<ActiveUpdateSession, 'startedAt' | 'updatedAt'>>,
): ActiveUpdateSession | null {
  try {
    const now = Date.now()
    const existing = readActiveUpdateSession()
    const sameTarget = existing != null
      && (!patch.targetVersion || !existing.targetVersion || existing.targetVersion === patch.targetVersion)
    const next: ActiveUpdateSession = {
      targetVersion: patch.targetVersion ?? existing?.targetVersion ?? '',
      fromVersion: patch.fromVersion ?? existing?.fromVersion ?? '',
      role: patch.role ?? existing?.role ?? '',
      stage: patch.stage ?? existing?.stage ?? 'installing',
      message: patch.message ?? existing?.message ?? '',
      progress: patch.progress ?? existing?.progress ?? 0,
      startedAt: sameTarget && existing ? existing.startedAt : now,
      updatedAt: now,
    }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
    notifySessionChange()
    return next
  } catch {
    return null
  }
}

export function clearActiveUpdateSession(): void {
  try {
    if (window.localStorage.getItem(STORAGE_KEY) !== null) {
      window.localStorage.removeItem(STORAGE_KEY)
      notifySessionChange()
    }
  } catch {
    // 存储不可用时静默跳过。
  }
}

export function subscribeActiveUpdateSession(listener: () => void): () => void {
  const onCustom = () => listener()
  const onStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY) listener()
  }
  window.addEventListener(SESSION_EVENT, onCustom)
  window.addEventListener('storage', onStorage)
  return () => {
    window.removeEventListener(SESSION_EVENT, onCustom)
    window.removeEventListener('storage', onStorage)
  }
}
