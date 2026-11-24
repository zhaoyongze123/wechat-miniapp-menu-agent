import type { StatusMeta } from '../hub/format'
import { STORE_STATE, formatStore, formatTime } from '../hub/format'
import type { CaptureMode, ManualState, WorkerFilter, WorkerRun, WorkerSort, WorkerTask } from './types'

/** 门店状态与 Hub 工作台共用同一服务端口径（menu_capture.capture.results.store_state）。 */
export { STORE_STATE, formatStore, formatTime }

export const RUN_STATUS: Record<string, StatusMeta> = {
  running: { label: '采集中', tone: 'info' },
  pause_requested: { label: '暂停中', tone: 'warning' },
  resuming: { label: '恢复中', tone: 'info' },
  terminating: { label: '终止中', tone: 'warning' },
  paused: { label: '已暂停', tone: 'warning' },
  blocked: { label: '已阻塞', tone: 'danger' },
  interrupted: { label: '已中断', tone: 'danger' },
  terminated: { label: '已终止', tone: 'secondary' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
}

export function runStatusOf(status: string | undefined): StatusMeta {
  if (!status) return { label: '未开始', tone: 'secondary' }
  return RUN_STATUS[status] ?? { label: status, tone: 'secondary' }
}

export const CAPTURE_MODE_LABEL: Record<CaptureMode, string> = {
  auto: '全自动 Agent',
  manual: '半自动人工',
}

export function captureModeLabel(mode: string | undefined): string {
  if (mode === 'auto' || mode === 'manual') return CAPTURE_MODE_LABEL[mode]
  return mode || '未锁定'
}

/**
 * 不合格原因与服务端 `materials.REJECTION_REASONS` 保持一致。
 * 原因属于前后端契约枚举，没有读取接口，需与后端同步维护。
 */
export const REJECTION_REASON_OPTIONS = [
  { value: 'blur', label: '图片模糊 / 无法识别' },
  { value: 'invalid_qr', label: '二维码无效' },
  { value: 'store_mismatch', label: '二维码门店不一致' },
  { value: 'store_closed', label: '门店已关店 / 停业' },
  { value: 'miniapp_unavailable', label: '小程序到期或未绑定' },
  { value: 'scan_order_unsupported', label: '门店不支持扫码点餐' },
  { value: 'not_ordering', label: '非点餐码' },
  { value: 'qr_external_web', label: '二维码指向外部网页（非小程序）' },
  { value: 'menu_unavailable', label: '门店未配置菜单 / 无可用菜品' },
  { value: 'phone_only', label: '需在手机上打开（桌面无法采集）' },
  { value: 'authorization_unavailable', label: '需要验证码/扫码授权（自动化无法进入）' },
  { value: 'network_unavailable', label: '网络异常，重试后仍无法进入' },
  { value: 'duplicate', label: '重复素材' },
  { value: 'other', label: '其他原因' },
]

export function reasonLabel(value: string | undefined, fallback = '未记录原因'): string {
  if (!value) return fallback
  return REJECTION_REASON_OPTIONS.find((option) => option.value === value)?.label ?? fallback
}

const WORKER_STATE_KEYS = ['pending', 'running', 'captured', 'review', 'rejected', 'returned'] as const

/** 服务端 store_state 是唯一事实源；缺失时按任务状态回退，不推断采集结果。 */
export function stateKeyOf(task: WorkerTask): string {
  if (task.store_state && WORKER_STATE_KEYS.includes(task.store_state as (typeof WORKER_STATE_KEYS)[number])) {
    return task.store_state
  }
  if (task.reject_reason) return 'rejected'
  if (task.status === 'captured') return 'captured'
  if (task.status === 'running') return 'running'
  if (task.status && ['needs_review', 'needs_retry', 'error', 'blocked', 'failed'].includes(task.status)) return 'review'
  return 'pending'
}

export function stateMetaOf(task: WorkerTask): StatusMeta {
  return STORE_STATE[stateKeyOf(task)] ?? { label: stateKeyOf(task), tone: 'secondary' }
}

/** 可与服务端 `/api/tasks/{id}/retry` 对齐的单店重试口径。 */
export const RETRYABLE_STATUSES = new Set(['needs_review', 'needs_retry', 'error', 'blocked', 'captured'])

export function canRetry(task: WorkerTask): boolean {
  return RETRYABLE_STATUSES.has(task.status)
}

const STATUS_ORDER: Record<string, number> = {
  running: 0,
  review: 1,
  rejected: 2,
  returned: 3,
  captured: 4,
  pending: 5,
}

export interface WorkerFilterInput {
  filter: WorkerFilter
  q: string
  batch: string
}

/** 前端过滤与旧工作台一致：状态取 store_state，批次取 batch_id，搜索匹配门店名与批次名。 */
export function filterTasks(tasks: WorkerTask[], input: WorkerFilterInput): WorkerTask[] {
  const needle = input.q.trim().toLowerCase()
  return tasks.filter((task) => {
    if (input.batch && task.batch_id !== input.batch) return false
    if (input.filter !== 'all' && stateKeyOf(task) !== input.filter) return false
    if (!needle) return true
    const haystack = `${task.source_name} ${task.batch_name ?? ''} ${task.reject_label ?? ''}`.toLowerCase()
    return haystack.includes(needle)
  })
}

export function sortTasks(tasks: WorkerTask[], sort: WorkerSort): WorkerTask[] {
  const copy = [...tasks]
  if (sort === 'recent') {
    copy.sort((a, b) => Date.parse(b.updated_at ?? '') - Date.parse(a.updated_at ?? ''))
    return copy
  }
  if (sort === 'status') {
    copy.sort((a, b) => (STATUS_ORDER[stateKeyOf(a)] ?? 9) - (STATUS_ORDER[stateKeyOf(b)] ?? 9))
    return copy
  }
  copy.sort((a, b) => a.source_name.localeCompare(b.source_name, 'zh-CN'))
  return copy
}

export function pageTasks(tasks: WorkerTask[], page: number, size: number): { rows: WorkerTask[]; page: number; pages: number } {
  const pages = Math.max(1, Math.ceil(tasks.length / size))
  const current = Math.min(Math.max(1, page), pages)
  const start = (current - 1) * size
  return { rows: tasks.slice(start, start + size), page: current, pages }
}

export interface RunProgress {
  processed: number
  total: number
  percent: number
  remaining: number
}

export function progressOf(run: WorkerRun | null | undefined): RunProgress {
  const total = Math.max(0, run?.total ?? 0)
  const processed = Math.min(Math.max(0, run?.processed ?? 0), total)
  return {
    processed,
    total,
    percent: total ? Math.round((processed / total) * 100) : 0,
    remaining: Math.max(0, run?.remaining ?? total - processed),
  }
}

/** 只有暂停/阻塞/中断的批次允许继续；终止与完成都不再恢复。 */
export function canResume(run: WorkerRun | null | undefined): boolean {
  return Boolean(run && ['paused', 'blocked', 'interrupted'].includes(run.status))
}

/** 半自动切换仅允许 Windows 全自动暂停批次（后端同样校验）。 */
export function canSwitchToManual(run: WorkerRun | null | undefined): boolean {
  return Boolean(run && run.status === 'paused' && run.capture_mode === 'auto')
}

export function manualStageLabel(manual: ManualState | null | undefined): string {
  if (!manual) return ''
  if (manual.waiting) return '等待人工处理'
  if (manual.stage === 'processing') return '正在保存结果'
  if (manual.stage === 'closed') return '本店已结束'
  return manual.stage || '—'
}
