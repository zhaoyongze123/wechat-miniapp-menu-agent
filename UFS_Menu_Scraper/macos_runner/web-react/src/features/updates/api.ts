import { apiFetch } from '../../api/client'
import type {
  UpdateDownloadState,
  UpdateHistoryRow,
  UpdateInstallState,
  UpdatePreferences,
  UpdateStatus,
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
