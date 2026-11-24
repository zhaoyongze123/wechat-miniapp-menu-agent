import type { StatusTone } from '../../components/StatusBadge'
import type { UsageAggregate, UsageCall, UsageGroup, UsageGroupBy, UsageSource } from './types'

export const SOURCE_OPTIONS: { value: UsageSource; label: string }[] = [
  { value: 'all', label: '全部用途' },
  { value: 'capture', label: '采集' },
  { value: 'annotation', label: '人工标注' },
  { value: 'material_analysis', label: '素材分析' },
  { value: 'connection_test', label: '连接测试' },
  { value: 'agent', label: '智能体' },
  { value: 'other', label: '其他' },
]

export const GROUP_OPTIONS: { value: UsageGroupBy; label: string }[] = [
  { value: 'day', label: '按天' },
  { value: 'run', label: '按采集队列' },
  { value: 'task', label: '按门店任务' },
  { value: 'model', label: '按模型' },
]

const SOURCE_LABELS: Record<string, string> = Object.fromEntries(SOURCE_OPTIONS.map((item) => [item.value, item.label]))

export function sourceLabel(source?: string | null): string {
  if (!source) return '未记录用途'
  return SOURCE_LABELS[source] ?? source
}

export function groupByLabel(groupBy?: string | null): string {
  return GROUP_OPTIONS.find((item) => item.value === groupBy)?.label ?? '按天'
}

const CALL_STATUS: Record<string, { label: string; tone: StatusTone }> = {
  success: { label: '成功', tone: 'success' },
  error: { label: '失败', tone: 'danger' },
  running: { label: '进行中', tone: 'info' },
  interrupted: { label: '中断', tone: 'warning' },
}

export function callStatusLabel(status?: string | null): string {
  if (!status) return '未知'
  return CALL_STATUS[status]?.label ?? status
}

export function callStatusTone(status?: string | null): StatusTone {
  if (!status) return 'secondary'
  return CALL_STATUS[status]?.tone ?? 'secondary'
}

const USAGE_STATE: Record<string, string> = {
  complete: '用量完整',
  partial: '用量部分',
  missing: '未返回用量',
}

export function usageStateLabel(state?: string | null): string {
  if (!state) return '用量未知'
  return USAGE_STATE[state] ?? state
}

/** 本地时区的 YYYY-MM-DD，与服务端 local_day 口径一致。 */
export function localDate(daysAgo = 0): string {
  const date = new Date()
  date.setDate(date.getDate() - daysAgo)
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

export function compactId(value?: string | null, length = 8): string {
  if (!value) return '—'
  return value.length <= length ? value : `${value.slice(0, length)}…`
}

export function formatInteger(value?: number | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '0'
  return value.toLocaleString('zh-CN')
}

/** Token 汇总按“有返回用量的调用数”判断：没有任何已知值时显示 —，避免把缺失当 0。 */
export function tokenText(value?: number | null, knownCalls?: number | null): string {
  if (!knownCalls) return '—'
  return formatInteger(value)
}

export function coverageText(summary: UsageAggregate): string {
  return `用量：完整 ${formatInteger(summary.known_usage_calls)} · 部分 ${formatInteger(summary.partial_usage_calls)} · 未返回 ${formatInteger(summary.unknown_usage_calls)}`
}

export function hasUnknownUsage(summary: UsageAggregate): boolean {
  return (summary.unknown_usage_calls ?? 0) > 0
}

export function summaryLine(summary: UsageAggregate): string {
  return `调用 ${formatInteger(summary.calls)} 次 · 成功 ${formatInteger(summary.success_calls)} · 失败 ${formatInteger(summary.error_calls)} · 进行中 ${formatInteger(summary.running_calls)} · 中断 ${formatInteger(summary.interrupted_calls)}`
}

export function groupRowLabel(groupBy: UsageGroupBy, group: UsageGroup): string {
  if (groupBy === 'day') return group.key
  if (groupBy === 'model') return group.key
  return group.label || group.key
}

export function groupRowHint(groupBy: UsageGroupBy, group: UsageGroup): string {
  if (groupBy === 'day') return '查看当天队列'
  if (groupBy === 'run') return '查看队列内门店'
  if (groupBy === 'task') return '查看调用明细'
  return group.provider || group.model || ''
}

export function elapsedText(value?: number | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  if (value < 1000) return `${Math.round(value)} ms`
  return `${(value / 1000).toFixed(1)} s`
}

export function stepText(value?: string | null): string {
  return value == null || value === '' ? '—' : String(value)
}

export function modelText(call: Pick<UsageCall, 'provider' | 'model' | 'returned_model'>): string {
  const base = `${call.provider}/${call.model}`
  if (call.returned_model && call.returned_model !== call.model) return `${base} → ${call.returned_model}`
  return base
}
