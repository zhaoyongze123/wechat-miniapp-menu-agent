import { apiFetch } from '../../api/client'
import type { ConnectionsSnapshot, EnvironmentSnapshot, SettingsPaneResult } from './types'

/** 只读快照；页面不会自动轮询，用户点击“检查连接”时才执行真实验证。 */
export function fetchConnections(signal?: AbortSignal): Promise<ConnectionsSnapshot> {
  return apiFetch<ConnectionsSnapshot>('/api/connections', { signal })
}

/** 执行一轮完整连接检查；采集运行中服务端返回 409。 */
export function connectConnections(force = true): Promise<ConnectionsSnapshot> {
  return apiFetch<ConnectionsSnapshot>('/api/connections/connect', {
    method: 'POST',
    json: { force },
  })
}

export function runEnvironmentTest(id: string): Promise<EnvironmentSnapshot> {
  return apiFetch<EnvironmentSnapshot>('/api/environment/test', { method: 'POST', json: { id } })
}

/** macOS 系统设置深链；Windows 上服务端会返回 400。 */
export function openEnvironmentSettings(pane: string): Promise<SettingsPaneResult> {
  return apiFetch<SettingsPaneResult>('/api/environment/settings', {
    method: 'POST',
    json: { pane },
  })
}
