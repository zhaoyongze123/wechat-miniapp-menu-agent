import { describe, expect, it } from 'vitest'
import {
  debugUsageText,
  formFromConfig,
  formatBytes,
  payloadFromForm,
  thinkingModeOf,
  thinkingValueOf,
  validateForm,
} from './format'
import type { AppConfig } from './types'

const config: AppConfig = {
  input_dir: '/data/input',
  output_dir: '/data/output',
  unqualified_dir: '',
  flat_store_output: false,
  model: {
    provider: 'openai_compatible',
    base_url: 'https://example.com/v1',
    model: 'vision-model',
    timeout_seconds: 90,
    enable_thinking: null,
    api_key_configured: true,
  },
  capture: {
    window_width: 430,
    window_height: 900,
    overlap_ratio: 0.35,
    max_pages: 120,
    max_agent_steps: 24,
    max_task_seconds: 900,
    settle_seconds: 1.2,
  },
  debug_logging: { max_storage_mb: 512 },
  materials: { analysis_concurrency: 2 },
  people_count: 1,
  auto_authorize: true,
}

describe('设置表单口径', () => {
  it('思考模式三态与后端 null 互相转换', () => {
    expect(thinkingModeOf(null)).toBe('auto')
    expect(thinkingModeOf(true)).toBe('on')
    expect(thinkingModeOf(false)).toBe('off')
    expect(thinkingValueOf('auto')).toBeNull()
    expect(thinkingValueOf('on')).toBe(true)
    expect(thinkingValueOf('off')).toBe(false)
  })

  it('留空密钥不进入保存载荷，清除密钥显式提交', () => {
    const form = formFromConfig(config)
    expect(payloadFromForm(form).model.api_key).toBeUndefined()
    expect(payloadFromForm(form).model.clear_api_key).toBeUndefined()

    const typed = { ...form, api_key: '  sk-test  ' }
    expect(payloadFromForm(typed).model.api_key).toBe('sk-test')

    const cleared = { ...form, clear_api_key: true }
    expect(payloadFromForm(cleared).model.clear_api_key).toBe(true)
  })

  it('保存载荷保留界面上未展示的采集参数', () => {
    const payload = payloadFromForm(formFromConfig(config))
    expect(payload.capture.max_pages).toBe(120)
    expect(payload.capture.overlap_ratio).toBe(0.35)
    expect(payload.debug_logging.max_storage_mb).toBe(512)
    expect(payload.materials.analysis_concurrency).toBe(2)
  })

  it('范围校验与服务端一致', () => {
    const form = formFromConfig(config)
    expect(validateForm(form)).toBe('')
    expect(validateForm({ ...form, timeout_seconds: '9' })).toContain('10–300')
    expect(validateForm({ ...form, max_storage_mb: '32' })).toContain('64–4096')
    expect(validateForm({ ...form, base_url: 'ftp://example.com' })).toContain('http(s)')
    expect(validateForm({ ...form, base_url: 'http://example.com' })).toContain('HTTPS')
    expect(validateForm({ ...form, base_url: 'http://127.0.0.1:8000/v1' })).toBe('')
  })

  it('调试占用按上限换算并提示自动清理', () => {
    expect(formatBytes(900)).toBe('900 B')
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(
      debugUsageText({ used_bytes: 10, total_bytes: 2 * 1024 * 1024, max_storage_mb: 1, file_count: 2 }),
    ).toContain('已触发自动清理')
    expect(debugUsageText({ used_bytes: 10, total_bytes: 2048, max_storage_mb: 512, file_count: 2 })).toContain(
      '2 个文件',
    )
  })
})
