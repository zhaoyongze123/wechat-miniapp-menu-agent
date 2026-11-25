/** 合格成品目录 DTO：与 `menu_capture/shared/delivery.py` 输出一致。 */

export type DeliveryState = 'unconfigured' | 'initializing' | 'syncing' | 'partial' | 'synced' | 'error'

export interface DeliveryCounts {
  qualified: number
  ready: number
  pending: number
  failed: number
  images: number
  bytes: number
}

export interface DeliveryExcludedReason {
  reason: string
  count: number
}

export interface DeliveryPendingItem {
  name: string
  directory: string
  source: 'output' | 'legacy_sources'
  images: number
  reason: string
}

/** 首次存量初始化与每次核对的可解释摘要，不把未核实目录伪装成已交付。 */
export interface DeliveryInventory {
  discovered: {
    shared_results: number
    output: number
    legacy_sources: number
    total: number
  }
  included: number
  legacy_included: number
  excluded: {
    count: number
    reasons: DeliveryExcludedReason[]
  }
  /** 仅凭文件系统无法确认资格的历史目录，等待人工核实，绝不直接纳入交付目录。 */
  pending_verification: {
    count: number
    stores: number
    items: DeliveryPendingItem[]
  }
  verified?: {
    stores: number
    images: number
    bytes: number
    content_digest: string
  }
}

export interface DeliveryStatus {
  supported: boolean
  configured: boolean
  state: DeliveryState
  message: string
  container_root: string
  /** 宿主机真实路径，用于复制给用户在文件管理器中打开。 */
  host_path: string
  /** 只有 Hub 与交付目录同机时才为 true，避免提供打不开的按钮。 */
  open_supported: boolean
  open_reason: string
  counts: DeliveryCounts
  inventory: DeliveryInventory
  content_digest: string
  last_sync_at: string
  initialized_at: string
  generation: number
  initializing: boolean
}

export type DeliveryStoreStatus = 'ready' | 'pending' | 'failed'

export type DeliverySource = 'shared_results' | 'output' | 'legacy_sources' | 'other'

export interface DeliveryStore {
  task_id: string
  batch_id: string
  batch_name: string
  store_name: string
  directory_name: string
  /** 业务上是否已合格且截图就绪；未就绪时 reason 给出真实原因。 */
  ready: boolean
  reason: string
  status: DeliveryStoreStatus
  error: string
  images: number
  bytes: number
  synced_at: string
  source: DeliverySource
  legacy: boolean
}

export interface DeliveryStoresResponse {
  total: number
  page: number
  size: number
  items: DeliveryStore[]
  batches: Array<{ batch_id: string; name: string }>
}
