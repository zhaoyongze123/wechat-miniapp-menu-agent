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
  /** 自动下载只下载并校验，安装仍需用户手动触发或安排空闲后更新。 */
  auto_download: boolean
  /** 已安装程序保留数量（含当前版本），2-5，默认 3。 */
  keep_versions: number
  channel: UpdateChannel
}

/** 空闲后更新安排：由后台在采集/标注结束后尝试安装，可随时取消。 */
export interface UpdateSchedule {
  armed: boolean
  version: string
  requested_at: string
  note: string
  last_result: string
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
  /** 是否支持安排空闲后自动安装。 */
  schedule: boolean
  rollback: boolean
  inventory: boolean
  cleanup: boolean
}

/** 存储治理预览：服务端只读扫描，删除必须再次提交确认标记。 */
export interface UpdateCleanupItem {
  version?: string
  name?: string
  size_bytes: number
  path?: string
}

export interface UpdateCleanupScope {
  supported: boolean
  reason: string
  kept?: string[]
  items: UpdateCleanupItem[]
  reclaimable_bytes: number
}

export interface UpdateStoragePreview {
  generated_at: string
  blocked: boolean
  blocked_reason: string
  confirm: string
  scopes: {
    releases: UpdateCleanupScope
    downloads: UpdateCleanupScope
    logs: UpdateCleanupScope
  }
  reclaimable_bytes: number
}

export interface UpdateCleanupResult {
  scopes: string[]
  deleted: Array<UpdateCleanupItem & { scope: string }>
  failures: Array<{ scope: string; version?: string; name?: string; error: string }>
  freed_bytes: number
  preview: UpdateStoragePreview
}

export type UpdateDownloadStatus =
  | 'idle'
  | 'downloading'
  | 'verifying'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

/** 下载状态机：服务端只落盘到数据目录，安装由独立执行器完成。 */
export interface UpdateDownloadState {
  active: boolean
  status: UpdateDownloadStatus
  version: string
  asset_kind: string
  asset_name: string
  arch: string
  sha256: string
  etag: string
  last_modified: string
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
  schedule: UpdateSchedule
  capabilities: UpdateCapabilities
  inventory: UpdateInventory
}

/** 安装执行器状态：字段与 `InstallController.status()` 的 journal 输出一致。 */
export interface UpdateInstallState {
  supported: boolean
  reason: string
  active: boolean
  status: 'idle' | 'starting' | 'installing' | 'verifying' | 'restarting' | 'rolling_back' | 'completed' | 'failed' | 'rolled_back'
  target_version: string
  previous_version: string
  started_at: string
  finished_at: string
  message: string
  error: string
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
