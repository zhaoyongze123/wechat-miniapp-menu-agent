import { apiFetch } from '../../api/client'
import type {
  AppConfig,
  DebugSummary,
  DirectoryKind,
  DirectoryPick,
  ModelTestResult,
  SavePayload,
  SharedConnectPayload,
} from './types'

/** 设置只走服务端配置；目录选择由本机服务弹出系统对话框。 */
export function fetchConfig(signal?: AbortSignal): Promise<AppConfig> {
  return apiFetch<AppConfig>('/api/config', { signal })
}

/** 密钥仅在本字段非空时提交；留空表示保留已保存的值。 */
export function saveConfig(payload: SavePayload): Promise<AppConfig> {
  return apiFetch<AppConfig>('/api/config', { method: 'POST', json: payload })
}

export function selectDirectory(kind: DirectoryKind): Promise<DirectoryPick> {
  return apiFetch<DirectoryPick>('/api/select-directory', { method: 'POST', json: { kind } })
}

/** 使用已保存配置测试模型连通性，成功结果由环境诊断缓存。 */
export function testModelConnection(): Promise<ModelTestResult> {
  return apiFetch<ModelTestResult>('/api/test-model', { method: 'POST', json: {} })
}

export function fetchDebugSummary(signal?: AbortSignal): Promise<DebugSummary> {
  return apiFetch<DebugSummary>('/api/debug/summary', { signal })
}

/** 只清理调试日志与调试截图，门店素材与输出不受影响。 */
export function clearDebugLogs(): Promise<{ ok?: boolean }> {
  return apiFetch<{ ok?: boolean }>('/api/debug/clear', { method: 'POST', json: {} })
}

/** Windows 采集端绑定共享后台；运行中服务端返回 409。 */
export function connectSharedWorkbench(payload: SharedConnectPayload): Promise<{ ok?: boolean; role?: string }> {
  return apiFetch<{ ok?: boolean; role?: string }>('/api/shared/connect', {
    method: 'POST',
    json: payload,
  })
}
