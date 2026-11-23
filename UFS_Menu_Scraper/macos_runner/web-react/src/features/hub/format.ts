import type { StatusTone } from '../../components/StatusBadge'

export interface StatusMeta {
  label: string
  tone: StatusTone
}

/** 门店唯一状态口径与服务端 capture_results.store_state 一致。 */
export const STORE_STATE: Record<string, StatusMeta> = {
  pending: { label: '待采集', tone: 'secondary' },
  running: { label: '采集中', tone: 'info' },
  captured: { label: '已截图待复核', tone: 'success' },
  review: { label: '待检查', tone: 'warning' },
  rejected: { label: '不合格', tone: 'danger' },
  returned: { label: '待重新选码', tone: 'info' },
}

export function storeStateOf(task: { store_state?: string; status?: string; reject_reason?: string }): StatusMeta {
  const meta = task.store_state ? STORE_STATE[task.store_state] : undefined
  if (meta) return meta
  if (task.reject_reason) return STORE_STATE.rejected as StatusMeta
  if (task.status === 'captured') return STORE_STATE.captured as StatusMeta
  if (task.status === 'running') return STORE_STATE.running as StatusMeta
  if (task.status && ['needs_review', 'needs_retry', 'error', 'blocked', 'failed'].includes(task.status)) {
    return STORE_STATE.review as StatusMeta
  }
  return STORE_STATE.pending as StatusMeta
}

export const ASSIGNMENT_STATUS: Record<string, StatusMeta> = {
  assigned: { label: '待下载', tone: 'secondary' },
  ready: { label: '已下载', tone: 'info' },
  pending_upload: { label: '待上传', tone: 'warning' },
  completed: { label: '后台已确认', tone: 'success' },
  reclaimed: { label: '已收回', tone: 'secondary' },
}

export function assignmentStatusOf(status: string | undefined, disposition?: string): StatusMeta {
  if (disposition === 'history') return { label: '旧结果留档', tone: 'secondary' }
  if (!status) return { label: '状态待确认', tone: 'secondary' }
  return ASSIGNMENT_STATUS[status] ?? { label: status, tone: 'secondary' }
}

/** 设备在线判定与旧工作台一致：最近一次心跳在 60 秒内。 */
export function isOnline(lastSeen: string | undefined, now = Date.now()): boolean {
  const seen = Date.parse(lastSeen ?? '')
  return Number.isFinite(seen) && now - seen < 60_000
}

export type DeviceState = 'online' | 'offline' | 'revoked' | 'archived'

export interface DeviceStateMeta {
  key: DeviceState
  label: string
  tone: StatusTone
}

export function deviceStateOf(worker: { revoked_at?: string; archived_at?: string; last_seen?: string }, now = Date.now()): DeviceStateMeta {
  if (worker.archived_at) return { key: 'archived', label: '已移除', tone: 'secondary' }
  if (worker.revoked_at) return { key: 'revoked', label: '已解除配对', tone: 'danger' }
  if (isOnline(worker.last_seen, now)) return { key: 'online', label: '在线', tone: 'success' }
  return { key: 'offline', label: '离线', tone: 'warning' }
}

export const CONFLICT_REASON: Record<string, string> = {
  materials: '素材返工中，需先完成选码',
  revision: '批次版本已更新',
  unavailable: '当前状态不可分配',
  source: '二维码源文件缺失或已变化',
  assigned: '已分配给其他采集电脑',
}

export function conflictReasonText(reason: string, workerName?: string): string {
  if (reason === 'assigned' && workerName) return `已分配给 ${workerName}`
  return CONFLICT_REASON[reason] ?? reason
}

/**
 * 门店名来自素材目录名：`编码__城市__名称__坐标`（部分批次多一段 LCA）。
 * 解析失败时原样返回，避免把无法识别的目录名截断成错误信息。
 */
export function formatStore(value: string): { name: string; code: string } {
  const raw = String(value || '')
  const parts = raw.split(/__+/).map((part) => part.trim())
  const hasLca = /^LCA\d+$/i.test(parts[1] ?? '')
  const nameIndex = hasLca ? 3 : 2
  if (parts.length <= nameIndex || !/^[A-Z]+\d+$/i.test(parts[0] ?? '')) return { name: raw, code: '' }
  const tail = parts.at(-1) ?? ''
  const coordinates = /^[+-]?\d+(?:\.\s*\d+)?\s*[,，]\s*[+-]?\d+(?:\.\s*\d+)?$/.test(tail)
  if (!hasLca && (!coordinates || !/[市区县旗州盟]$/.test(parts[1] ?? ''))) return { name: raw, code: '' }
  return {
    name: parts.slice(nameIndex, coordinates ? -1 : undefined).join(' ').trim() || raw,
    code: parts[0] ?? '',
  }
}

export function formatTime(value: string | undefined, fallback = '—'): string {
  if (!value) return fallback
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
