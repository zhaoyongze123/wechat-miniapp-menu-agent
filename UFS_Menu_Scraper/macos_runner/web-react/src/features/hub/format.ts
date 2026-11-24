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

const STORE_COORDINATES = /^[[(]?\s*[+-]?\d{1,3}(?:\.\s*\d+)?\s*[,，]\s*[+-]?\d{1,3}(?:\.\s*\d+)?\s*[\])]?$/
const STORE_CITY = /[市州区县旗盟]$/
/** LCA 段在部分批次中存在，也可能是 `LCA-123` / `LCA 123` 等空格变体。 */
const STORE_LCA = /^LCA[-_ ]?\d+$/i
/** 门店编码通常为字母数字组合（如 `E0005`、`SH001`），纯数字目录名同样视为编码。 */
const STORE_CODE = /^(?=.*\d)[A-Za-z0-9_-]{2,24}$/

/**
 * 门店名来自素材目录名，常见格式为 `编码__[LCA]__城市__名称__坐标`。
 * 真实目录可能包含全角下划线、多余空格、坐标括号或空格变体，因此按尾部坐标与城市段定位，
 * 而不是依赖固定下标。无法可靠解析时原样返回，避免把目录名截断成错误信息。
 */
export function formatStore(value: string): { name: string; code: string } {
  const raw = String(value || '').trim()
  if (!raw) return { name: '', code: '' }

  // 生产目录实际使用全角下划线 `＿`，且可能只出现一次；统一归一为 `__` 后再分段，
  // 避免把单个 ASCII `_` 误当分隔符，也兼容连续全角下划线。
  const normalized = raw.replace(/\u3000/g, ' ').replace(/＿+/g, '__')
  const parts = normalized
    .split(/_{2,}/)
    .map((part) => part.replace(/\s+/g, ' ').trim())
    .filter(Boolean)

  // 单段目录名通常已经是门店名，直接展示。
  if (parts.length < 2) return { name: raw, code: '' }

  let coordinateIndex = -1
  for (let index = parts.length - 1; index >= 0; index -= 1) {
    if (STORE_COORDINATES.test(parts[index] ?? '')) {
      coordinateIndex = index
      break
    }
  }

  const tokens = (coordinateIndex >= 0 ? parts.slice(0, coordinateIndex) : parts)
    .filter((token) => !STORE_LCA.test(token))

  let code = ''
  if (tokens.length >= 2 && STORE_CODE.test(tokens[0] ?? '')) code = tokens.shift() ?? ''

  // 城市段之后可能是名称，名称本身也可能带空格（如“上海 人民广场店”）。
  let cityIndex = -1
  for (let index = tokens.length - 1; index >= 0; index -= 1) {
    if (STORE_CITY.test(tokens[index] ?? '')) {
      cityIndex = index
      break
    }
  }
  if (cityIndex >= 0 && cityIndex < tokens.length - 1) {
    const name = tokens.slice(cityIndex + 1).join(' ').trim()
    if (name) return { name, code }
  }

  // 没有可识别城市段时，退化为坐标前最后一段，至少避免整段原始目录名撑满列表。
  if (tokens.length >= 1) {
    const name = tokens[tokens.length - 1] ?? ''
    // 只有编码、没有名称时无法可靠解析，保留原始目录名。
    const onlyCode = tokens.length === 1 && !code && STORE_CODE.test(name)
    if (name && !onlyCode) return { name, code }
  }

  return { name: raw, code: '' }
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
