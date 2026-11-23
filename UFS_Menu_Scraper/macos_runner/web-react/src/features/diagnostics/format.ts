import type { StatusTone } from '../../components/StatusBadge'
import type { CheckStatus, ConnectionStatus } from './types'

const STATUS_META: Record<CheckStatus, { label: string; tone: StatusTone }> = {
  ready: { label: '正常', tone: 'success' },
  checking: { label: '检测中', tone: 'info' },
  warning: { label: '需要处理', tone: 'warning' },
  error: { label: '异常', tone: 'danger' },
  unknown: { label: '未知', tone: 'secondary' },
}

/** 未知状态保留原文，不把后端新增状态误报为“正常”。 */
export function checkStatusMeta(status: string | undefined): { label: string; tone: StatusTone } {
  return STATUS_META[(status ?? 'unknown') as CheckStatus] ?? { label: status ?? '未知', tone: 'secondary' }
}

const CONNECTION_META: Record<ConnectionStatus, { label: string; tone: StatusTone }> = {
  ready: { label: '环境已就绪', tone: 'success' },
  checking: { label: '正在检查', tone: 'info' },
  warning: { label: '需要处理', tone: 'warning' },
  error: { label: '环境异常', tone: 'danger' },
  unknown: { label: '状态未知', tone: 'secondary' },
}

export function connectionStatusMeta(status: string | undefined): { label: string; tone: StatusTone } {
  return CONNECTION_META[(status ?? 'unknown') as ConnectionStatus] ?? { label: status ?? '状态未知', tone: 'secondary' }
}

const TEST_TARGETS = new Set(['model_service', 'windows_desktop', 'uipath_desktop', 'wechat'])

/** 测试入口只接受服务端列出的检查项；模块没有显式 target 时按模块映射。 */
export function testTargetOf(checkId: string | undefined, moduleId?: string): string | null {
  const explicit = checkId ?? ''
  if (TEST_TARGETS.has(explicit)) return explicit
  const fallback: Record<string, string> = {
    engine: 'windows_desktop',
    wechat: 'wechat',
    model: 'model_service',
  }
  const target = moduleId ? fallback[moduleId] : undefined
  return target && TEST_TARGETS.has(target) ? target : null
}

const SETTINGS_PANES = new Set(['accessibility', 'screen_recording', 'automation', 'full_disk_access'])

export function settingsPaneOf(target: string | undefined): string | null {
  return target && SETTINGS_PANES.has(target) ? target : null
}
