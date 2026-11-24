export const KIND_LABEL: Record<string, string> = {
  capture: '采集运行',
  materials: '素材分析',
}

export interface StatusMeta {
  label: string
  tone: 'success' | 'danger' | 'warning' | 'info' | 'secondary'
}

/** 与旧任务中心一致的状态口径：同一状态在任何页面都得到相同文案与色调。 */
export const HISTORY_STATUS: Record<string, StatusMeta> = {
  queued: { label: '排队中', tone: 'secondary' },
  running: { label: '运行中', tone: 'info' },
  pause_requested: { label: '暂停中', tone: 'info' },
  resuming: { label: '恢复中', tone: 'info' },
  terminating: { label: '终止中', tone: 'info' },
  paused: { label: '已暂停', tone: 'warning' },
  blocked: { label: '待恢复', tone: 'warning' },
  interrupted: { label: '已中断', tone: 'warning' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'danger' },
  terminated: { label: '已终止', tone: 'danger' },
  stopped: { label: '已停止', tone: 'danger' },
}

export function statusOf(status: string | undefined): StatusMeta {
  if (!status) return { label: '未知', tone: 'secondary' }
  return HISTORY_STATUS[status] ?? { label: status, tone: 'secondary' }
}

export function formatTime(value: string | undefined): string {
  if (!value) return '—'
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

/** 素材事件只显示时分秒，日期由列表标题行承担。 */
export function shortTime(value: string | undefined): string {
  if (!value) return ''
  return value.length >= 19 ? value.slice(11, 19) : value
}
