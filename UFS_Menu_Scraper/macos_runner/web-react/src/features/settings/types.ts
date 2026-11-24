/** 设置域 DTO；字段与 `menu_capture/config.py` 及 `/api/config` 保持一致。 */

export type ModelProvider = 'openai_compatible' | 'anthropic'
export type ThinkingMode = 'auto' | 'on' | 'off'

export interface ModelSettings {
  provider: ModelProvider
  base_url: string
  model: string
  timeout_seconds: number
  /** null 表示不改变服务商默认行为。 */
  enable_thinking: boolean | null
  /** 只读标记，服务端不会返回密钥本身。 */
  api_key_configured: boolean
}

export interface CaptureSettings {
  window_width: number
  window_height: number
  overlap_ratio: number
  max_pages: number
  max_agent_steps: number
  max_task_seconds: number
  settle_seconds: number
}

export interface AppConfig {
  input_dir: string
  output_dir: string
  unqualified_dir: string
  flat_store_output: boolean
  model: ModelSettings
  capture: CaptureSettings
  debug_logging: { max_storage_mb: number }
  materials: { analysis_concurrency: number }
  people_count: number
  auto_authorize: boolean
}

export interface SavePayload {
  input_dir: string
  output_dir: string
  model: {
    provider: ModelProvider
    base_url: string
    model: string
    timeout_seconds: number
    enable_thinking: boolean | null
    api_key?: string
    clear_api_key?: boolean
  }
  capture: CaptureSettings
  debug_logging: { max_storage_mb: number }
  materials: { analysis_concurrency: number }
  people_count: number
  auto_authorize: boolean
}

export type DirectoryKind = 'materials' | 'input' | 'output'

export interface DirectoryPick {
  cancelled: boolean
  path?: string
}

export interface ModelTestResult {
  ok: boolean
  message: string
  checked_at: string
}

/** `GET /api/debug/summary`：只报告占用，不触发任何任务或模型调用。 */
export interface DebugSummary {
  max_storage_mb: number
  used_bytes: number
  managed_bytes: number
  reserved_bytes: number
  total_bytes: number
  file_count: number
  dropped_records: number
  dropped_evidence: number
}

export interface SharedConnectPayload {
  url: string
  code: string
  name: string
}
