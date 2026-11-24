/** 更新域 DTO：与 `menu_capture/updates.py` 的状态、清单和审计输出一致。 */

export type UpdateChannel = 'stable' | 'preview'

export interface UpdateAsset {
  kind: string
  name: string
  size: number
  sha256: string
  url: string
  platform: string
  arch: string
}

export interface UpdateRelease {
  version: string
  tag: string
  channel: UpdateChannel
  name: string
  html_url: string
  published_at: string
  notes: string
  eligible: boolean
  reason: string
  git_commit: string
  built_at: string
  min_supported_version: string
  rollback: string
  assets: UpdateAsset[]
}

export interface UpdatePreferences {
  auto_check: boolean
  channel: UpdateChannel
}

export interface UpdateInventoryRelease {
  version: string
  built_at: string
  git_commit: string
  size_bytes: number
  current: boolean
  running: boolean
  protected: boolean
}

export interface UpdateInventory {
  supported: boolean
  reason?: string
  install_root?: string
  current_version?: string
  running_version?: string
  releases: UpdateInventoryRelease[]
  cleanup_candidates: UpdateInventoryRelease[]
  cleanup_supported?: boolean
  cleanup_reason?: string
}

export interface UpdateCapabilities {
  check: boolean
  download: boolean
  install: boolean
  rollback: boolean
  inventory: boolean
}

export type UpdateDownloadStatus =
  | 'idle'
  | 'downloading'
  | 'verifying'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

/** 下载状态机：服务端只落盘到数据目录，安装执行器仍未启用。 */
export interface UpdateDownloadState {
  active: boolean
  status: UpdateDownloadStatus
  version: string
  asset_kind: string
  asset_name: string
  downloaded: number
  total: number
  started_at: string
  finished_at: string
  error: string
  path: string
  resumable: boolean
}

export interface UpdateStatus {
  role: string
  repository: string
  platform: string
  arch: string
  current_version: string
  channel: UpdateChannel
  preferences: UpdatePreferences
  latest: UpdateRelease | null
  update_available: boolean
  last_checked: string
  stale: boolean
  error: string
  checking: boolean
  download: UpdateDownloadState
  capabilities: UpdateCapabilities
  inventory: UpdateInventory
}

export interface UpdateHistoryRow {
  time: string
  action: string
  result: string
  message: string
  version: string
  platform: string
  role: string
}
