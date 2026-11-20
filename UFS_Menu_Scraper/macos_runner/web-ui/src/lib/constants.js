// 显示文案与业务状态映射集中维护：组件只消费映射结果，避免各页面各写一套口径。
export const HISTORY_STATUS = {
  queued: { label: '排队中', tone: 'muted' },
  running: { label: '运行中', tone: 'running' },
  pause_requested: { label: '暂停中', tone: 'running' },
  resuming: { label: '恢复中', tone: 'running' },
  terminating: { label: '终止中', tone: 'running' },
  paused: { label: '已暂停', tone: 'warn' },
  blocked: { label: '待恢复', tone: 'warn' },
  interrupted: { label: '已中断', tone: 'warn' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'danger' },
  terminated: { label: '已终止', tone: 'danger' },
  stopped: { label: '已停止', tone: 'danger' },
}

export const KIND_LABEL = { capture: '采集运行', materials: '素材分析' }

export function statusOf(status) {
  return HISTORY_STATUS[status] || { label: status || '未知', tone: 'muted' }
}
