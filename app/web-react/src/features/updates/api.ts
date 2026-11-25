import { apiFetch } from '../../api/client'
import type {
  UpdateCleanupResult,
  UpdateDownloadState,
  UpdateFlow,
  UpdateHistoryRow,
  UpdateInstallState,
  UpdateMode,
  UpdatePreferences,
  UpdateSchedule,
  UpdateStatus,
  UpdateStoragePreview,
} from './types'

export function fetchUpdateStatus(signal?: AbortSignal): Promise<UpdateStatus> {
  return apiFetch<UpdateStatus>('/api/updates/status', { signal })
}

export function checkUpdates(): Promise<UpdateStatus> {
  return apiFetch<UpdateStatus>('/api/updates/check', { method: 'POST', json: {} })
}

export function saveUpdatePreferences(payload: Partial<UpdatePreferences>): Promise<UpdatePreferences> {
  return apiFetch<UpdatePreferences>('/api/updates/preferences', { method: 'PUT', json: payload })
}

export function startUpdateDownload(): Promise<UpdateDownloadState> {
  return apiFetch<UpdateDownloadState>('/api/updates/download', { method: 'POST', json: {} })
}

export function cancelUpdateDownload(): Promise<UpdateDownloadState> {
  return apiFetch<UpdateDownloadState>('/api/updates/download/cancel', { method: 'POST', json: {} })
}

/** 一次授权：服务端自动下载、等待空闲、安装、重启与健康检查。 */
export function authorizeUpdate(mode: UpdateMode): Promise<UpdateFlow> {
  return apiFetch<UpdateFlow>('/api/updates/update', { method: 'POST', json: { mode } })
}

/** 尚未开始切换前可取消；安装已启动时服务端会拒绝。 */
export function cancelUpdateFlow(): Promise<UpdateFlow> {
  return apiFetch<UpdateFlow>('/api/updates/update/cancel', { method: 'POST', json: {} })
}

export function fetchUpdateHistory(limit = 50, signal?: AbortSignal): Promise<UpdateHistoryRow[]> {
  return apiFetch<UpdateHistoryRow[]>(`/api/updates/history?limit=${limit}`, { signal })
}

export function fetchUpdateInstall(signal?: AbortSignal): Promise<UpdateInstallState> {
  return apiFetch<UpdateInstallState>('/api/updates/install', { signal })
}

/** confirm 必须与目标版本一致，服务端据此拒绝误触。 */
export function startUpdateInstall(version: string): Promise<UpdateInstallState> {
  return apiFetch<UpdateInstallState>('/api/updates/install', {
    method: 'POST',
    json: { version, confirm: version },
  })
}

/** 空闲后更新：服务端保存安排，采集或标注结束后由后台尝试安装。 */
export function scheduleUpdateInstall(): Promise<UpdateSchedule> {
  return apiFetch<UpdateSchedule>('/api/updates/install/schedule', { method: 'POST', json: {} })
}

export function cancelUpdateInstallSchedule(): Promise<UpdateSchedule> {
  return apiFetch<UpdateSchedule>('/api/updates/install/schedule/cancel', { method: 'POST', json: {} })
}

export function fetchUpdateCleanup(signal?: AbortSignal): Promise<UpdateStoragePreview> {
  return apiFetch<UpdateStoragePreview>('/api/updates/cleanup', { signal })
}

/** 删除是破坏性操作：confirm 由服务端固定校验，安装/下载进行中会被拒绝。 */
export function runUpdateCleanup(scopes: string[]): Promise<UpdateCleanupResult> {
  return apiFetch<UpdateCleanupResult>('/api/updates/cleanup', {
    method: 'POST',
    json: { confirm: 'cleanup', scopes },
  })
}
