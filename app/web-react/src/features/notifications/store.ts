import { useEffect, useState } from 'react'
import type { NotificationEventKind } from './preferences'

export const NOTIFICATION_CENTER_KEY = 'menu-capture.notification-center.v1'
export const NOTIFICATION_MUTE_UNTIL_KEY = 'menu-capture.notification-mute-until.v1'
const CENTER_UPDATE_EVENT = 'menu-capture-notification-center'

const MAX_ITEMS = 200
const RETENTION_MS = 7 * 24 * 60 * 60 * 1000

export type NotificationSeverity = 'info' | 'warning' | 'danger' | 'success'
export type NotificationCategory = 'manual' | 'task' | 'device' | 'sync'

export interface NotificationItem {
  id: string
  dedupeKey: string
  kind: NotificationEventKind
  category: NotificationCategory
  severity: NotificationSeverity
  title: string
  body: string
  count: number
  createdAt: number
  updatedAt: number
  readAt: number | null
  resolvedAt: number | null
  route?: string | undefined
  actionLabel?: string | undefined
}

export interface PushNotificationInput {
  id?: string | undefined
  dedupeKey: string
  kind: NotificationEventKind
  category: NotificationCategory
  severity: NotificationSeverity
  title: string
  body: string
  route?: string | undefined
  actionLabel?: string | undefined
  /** 同 dedupeKey 在冷静期内再次触发时只更新计数与时间戳，不重新标未读/不重复打扰。 */
  cooldownMs?: number | undefined
}

export interface PushNotificationResult {
  item: NotificationItem
  /** 是否应向用户发出实时打扰（声音/桌面通知/Toast）。 */
  shouldAlert: boolean
}

let memoryItems: NotificationItem[] | null = null

function readRawItems(now = Date.now()): NotificationItem[] {
  const cutoff = now - RETENTION_MS
  try {
    const raw = localStorage.getItem(NOTIFICATION_CENTER_KEY)
    if (!raw) return memoryItems ? memoryItems.filter((item) => item.updatedAt >= cutoff) : []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return (parsed as NotificationItem[])
      .filter((item) => item && typeof item.id === 'string' && typeof item.updatedAt === 'number' && item.updatedAt >= cutoff)
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, MAX_ITEMS)
  } catch {
    return memoryItems ? memoryItems.filter((item) => item.updatedAt >= cutoff) : []
  }
}

function writeItems(items: NotificationItem[]): void {
  const trimmed = items
    .slice()
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_ITEMS)
  memoryItems = trimmed
  try {
    localStorage.setItem(NOTIFICATION_CENTER_KEY, JSON.stringify(trimmed))
  } catch {
    // 浏览器禁用本地存储时仅保留当前页内存态。
  }
  window.dispatchEvent(new Event(CENTER_UPDATE_EVENT))
}

export function listNotifications(now = Date.now()): NotificationItem[] {
  return readRawItems(now)
}

export function pushNotification(input: PushNotificationInput, now = Date.now()): PushNotificationResult {
  const items = readRawItems(now)
  const existingIndex = items.findIndex((item) => item.dedupeKey === input.dedupeKey && item.resolvedAt === null)
  if (existingIndex >= 0) {
    const existing = items[existingIndex] as NotificationItem
    const cooldownMs = input.cooldownMs ?? 0
    const inCooldown = cooldownMs > 0 && now - existing.updatedAt < cooldownMs
    const updated: NotificationItem = {
      ...existing,
      title: input.title,
      body: input.body,
      severity: input.severity,
      route: input.route ?? existing.route,
      actionLabel: input.actionLabel ?? existing.actionLabel,
      count: existing.count + 1,
      updatedAt: now,
      readAt: inCooldown ? existing.readAt : null,
    }
    const next = [...items]
    next[existingIndex] = updated
    writeItems(next)
    return { item: updated, shouldAlert: !inCooldown }
  }

  // 若最近刚有同 dedupeKey 的已恢复记录且仍在冷静期内，折叠回该条并重新激活为未解决，避免抖动刷屏。
  const recentResolvedIndex = input.cooldownMs
    ? items.findIndex((item) => item.dedupeKey === input.dedupeKey && item.resolvedAt !== null && now - item.updatedAt < input.cooldownMs!)
    : -1
  if (recentResolvedIndex >= 0) {
    const existing = items[recentResolvedIndex] as NotificationItem
    const updated: NotificationItem = {
      ...existing,
      title: input.title,
      body: input.body,
      severity: input.severity,
      route: input.route ?? existing.route,
      actionLabel: input.actionLabel ?? existing.actionLabel,
      count: existing.count + 1,
      updatedAt: now,
      resolvedAt: null,
    }
    const next = [...items]
    next[recentResolvedIndex] = updated
    writeItems(next)
    return { item: updated, shouldAlert: false }
  }

  const item: NotificationItem = {
    id: input.id ?? `${input.dedupeKey}:${now}`,
    dedupeKey: input.dedupeKey,
    kind: input.kind,
    category: input.category,
    severity: input.severity,
    title: input.title,
    body: input.body,
    count: 1,
    createdAt: now,
    updatedAt: now,
    readAt: null,
    resolvedAt: null,
    route: input.route,
    actionLabel: input.actionLabel,
  }
  writeItems([item, ...items])
  return { item, shouldAlert: true }
}

/** 状态恢复时将对应的活跃告警自动标记为已恢复，并标记已读以消除过时红点。 */
export function resolveNotification(dedupeKey: string, resolvedNote?: string, now = Date.now()): boolean {
  const items = readRawItems(now)
  let changed = false
  const next = items.map((item) => {
    if (item.dedupeKey !== dedupeKey || item.resolvedAt !== null) return item
    changed = true
    return {
      ...item,
      body: resolvedNote ?? item.body,
      resolvedAt: now,
      updatedAt: now,
      readAt: item.readAt ?? now,
    }
  })
  if (changed) writeItems(next)
  return changed
}

export function markNotificationRead(id: string, now = Date.now()): void {
  const items = readRawItems(now)
  let changed = false
  const next = items.map((item) => {
    if (item.id !== id || item.readAt !== null) return item
    changed = true
    return { ...item, readAt: now }
  })
  if (changed) writeItems(next)
}

export function markAllNotificationsRead(now = Date.now()): void {
  const items = readRawItems(now)
  let changed = false
  const next = items.map((item) => {
    if (item.readAt !== null) return item
    changed = true
    return { ...item, readAt: now }
  })
  if (changed) writeItems(next)
}

export function removeNotification(id: string): void {
  const items = readRawItems()
  const next = items.filter((item) => item.id !== id)
  if (next.length !== items.length) writeItems(next)
}

export function clearResolvedNotifications(now = Date.now()): void {
  const items = readRawItems(now)
  const next = items.filter((item) => item.resolvedAt === null && item.readAt === null)
  if (next.length !== items.length) writeItems(next)
}

export function getMuteUntil(): number {
  try {
    const raw = Number(localStorage.getItem(NOTIFICATION_MUTE_UNTIL_KEY) ?? '0')
    return Number.isFinite(raw) && raw > Date.now() ? raw : 0
  } catch {
    return 0
  }
}

export function isNotificationMuted(now = Date.now()): boolean {
  try {
    const raw = Number(localStorage.getItem(NOTIFICATION_MUTE_UNTIL_KEY) ?? '0')
    return Number.isFinite(raw) && raw > now
  } catch {
    return false
  }
}

export function setNotificationMuteMinutes(minutes: number): number {
  const until = minutes > 0 ? Date.now() + minutes * 60_000 : 0
  try {
    if (until > 0) localStorage.setItem(NOTIFICATION_MUTE_UNTIL_KEY, String(until))
    else localStorage.removeItem(NOTIFICATION_MUTE_UNTIL_KEY)
  } catch {
    // 忽略本地存储受限错误。
  }
  window.dispatchEvent(new Event(CENTER_UPDATE_EVENT))
  return until
}

export function useNotificationCenter() {
  const [items, setItems] = useState<NotificationItem[]>(() => readRawItems())
  const [muteUntil, setMuteUntil] = useState<number>(() => getMuteUntil())

  useEffect(() => {
    const refresh = () => {
      setItems(readRawItems())
      setMuteUntil(getMuteUntil())
    }
    window.addEventListener(CENTER_UPDATE_EVENT, refresh)
    window.addEventListener('storage', refresh)
    const timer = window.setInterval(() => {
      const current = getMuteUntil()
      setMuteUntil((prev) => (prev === current ? prev : current))
    }, 30_000)
    return () => {
      window.removeEventListener(CENTER_UPDATE_EVENT, refresh)
      window.removeEventListener('storage', refresh)
      window.clearInterval(timer)
    }
  }, [])

  const unreadCount = items.filter((item) => item.readAt === null).length
  const activeAlertCount = items.filter(
    (item) => item.resolvedAt === null && (item.severity === 'danger' || item.severity === 'warning'),
  ).length

  return {
    items,
    unreadCount,
    activeAlertCount,
    muteUntil,
    muted: muteUntil > Date.now(),
    markRead: markNotificationRead,
    markAllRead: markAllNotificationsRead,
    remove: removeNotification,
    clearResolved: clearResolvedNotifications,
    setMuteMinutes: setNotificationMuteMinutes,
  }
}
