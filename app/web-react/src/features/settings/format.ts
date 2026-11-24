import type { AppConfig, SavePayload, ThinkingMode } from './types'

/** 表单统一保存为字符串，允许用户清空后重新输入；保存前再按服务端范围校验。 */
export interface SettingsFormState {
  input_dir: string
  output_dir: string
  provider: AppConfig['model']['provider']
  base_url: string
  model: string
  timeout_seconds: string
  enable_thinking: ThinkingMode
  api_key: string
  clear_api_key: boolean
  people_count: string
  auto_authorize: boolean
  analysis_concurrency: string
  max_storage_mb: string
  max_pages: string
  max_agent_steps: string
  max_task_seconds: string
  settle_seconds: string
  window_width: string
  window_height: string
  overlap_ratio: string
}

export function formFromConfig(config: AppConfig): SettingsFormState {
  return {
    input_dir: config.input_dir,
    output_dir: config.output_dir,
    provider: config.model.provider,
    base_url: config.model.base_url,
    model: config.model.model,
    timeout_seconds: String(config.model.timeout_seconds),
    enable_thinking: thinkingModeOf(config.model.enable_thinking),
    api_key: '',
    clear_api_key: false,
    people_count: String(config.people_count),
    auto_authorize: config.auto_authorize,
    analysis_concurrency: String(config.materials.analysis_concurrency),
    max_storage_mb: String(config.debug_logging.max_storage_mb),
    max_pages: String(config.capture.max_pages),
    max_agent_steps: String(config.capture.max_agent_steps),
    max_task_seconds: String(config.capture.max_task_seconds),
    settle_seconds: String(config.capture.settle_seconds),
    window_width: String(config.capture.window_width),
    window_height: String(config.capture.window_height),
    overlap_ratio: String(config.capture.overlap_ratio),
  }
}

export function thinkingModeOf(value: boolean | null): ThinkingMode {
  if (value === null) return 'auto'
  return value ? 'on' : 'off'
}

export function thinkingValueOf(mode: ThinkingMode): boolean | null {
  if (mode === 'on') return true
  if (mode === 'off') return false
  return null
}

const RANGES: Array<[keyof SettingsFormState, number, number, string]> = [
  ['timeout_seconds', 10, 300, '请求超时需在 10–300 秒之间'],
  ['people_count', 1, 12, '就餐人数需在 1–12 之间'],
  ['analysis_concurrency', 1, 4, '分析并发需在 1–4 之间'],
  ['max_storage_mb', 64, 4096, '调试日志上限需在 64–4096 MB 之间'],
  ['max_pages', 2, 500, '最大页数需在 2–500 之间'],
  ['max_agent_steps', 2, 60, '最大 Agent 步数需在 2–60 之间'],
  ['max_task_seconds', 60, 7200, '单任务上限需在 60–7200 秒之间'],
  ['settle_seconds', 0.3, 10, '页面稳定等待需在 0.3–10 秒之间'],
]

/** 与服务端字段校验保持一致，返回第一条可读错误；空字符串表示通过。 */
export function validateForm(form: SettingsFormState): string {
  for (const [key, min, max, message] of RANGES) {
    const value = Number(form[key])
    if (!Number.isFinite(value) || value < min || value > max) return message
  }
  const url = form.base_url.trim()
  if (url && !/^https?:\/\/[^\s]+$/i.test(url)) return '接口地址应为 http(s) URL'
  if (/^http:\/\//i.test(url) && !/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::|\/|$)/i.test(url)) {
    return '远程模型接口需要 HTTPS；HTTP 仅供本机服务使用'
  }
  return ''
}

/**
 * 生成保存载荷：未在界面展示的采集参数也按当前值回传，
 * 避免 hub 保存模型配置时把本机采集参数重置为默认值。
 */
export function payloadFromForm(form: SettingsFormState): SavePayload {
  const url = form.base_url.trim()
  return {
    input_dir: form.input_dir.trim(),
    output_dir: form.output_dir.trim(),
    model: {
      provider: form.provider,
      base_url: url,
      model: form.model.trim(),
      timeout_seconds: Number(form.timeout_seconds),
      enable_thinking: thinkingValueOf(form.enable_thinking),
      ...(form.api_key.trim() ? { api_key: form.api_key.trim() } : {}),
      ...(form.clear_api_key ? { clear_api_key: true } : {}),
    },
    capture: {
      window_width: Number(form.window_width),
      window_height: Number(form.window_height),
      overlap_ratio: Number(form.overlap_ratio),
      max_pages: Number(form.max_pages),
      max_agent_steps: Number(form.max_agent_steps),
      max_task_seconds: Number(form.max_task_seconds),
      settle_seconds: Number(form.settle_seconds),
    },
    debug_logging: { max_storage_mb: Number(form.max_storage_mb) },
    materials: { analysis_concurrency: Number(form.analysis_concurrency) },
    people_count: Number(form.people_count),
    auto_authorize: form.auto_authorize,
  }
}

export function formatBytes(value: number | undefined): string {
  const bytes = Number(value) || 0
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

export function debugUsageText(summary: {
  used_bytes: number
  total_bytes: number
  max_storage_mb: number
  file_count: number
}): string {
  const limit = `${summary.max_storage_mb} MB`
  const dropped = summary.total_bytes > summary.max_storage_mb * 1024 * 1024
  return `已用 ${formatBytes(summary.total_bytes)} / 上限 ${limit} · ${summary.file_count} 个文件${dropped ? ' · 已触发自动清理' : ''}`
}
