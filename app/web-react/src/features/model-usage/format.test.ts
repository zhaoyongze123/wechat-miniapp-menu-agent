import { describe, expect, it } from 'vitest'
import {
  callStatusLabel,
  callStatusTone,
  compactId,
  coverageText,
  elapsedText,
  formatInteger,
  groupRowLabel,
  hasUnknownUsage,
  localDate,
  modelText,
  sourceLabel,
  summaryLine,
  tokenText,
  usageStateLabel,
} from './format'
import type { UsageAggregate } from './types'

const summary: UsageAggregate = {
  calls: 12,
  success_calls: 9,
  error_calls: 1,
  running_calls: 0,
  interrupted_calls: 2,
  known_usage_calls: 8,
  partial_usage_calls: 1,
  unknown_usage_calls: 3,
  input_tokens: 1000,
  output_tokens: 500,
  total_tokens: 1500,
  cached_tokens: 0,
  cache_write_tokens: 0,
  reasoning_tokens: 0,
  image_tokens: 0,
  elapsed_ms: 12000,
}

describe('usage summaries', () => {
  it('formats counts and call outcomes', () => {
    expect(formatInteger(12345)).toBe('12,345')
    expect(formatInteger(undefined)).toBe('0')
    expect(summaryLine(summary)).toBe('调用 12 次 · 成功 9 · 失败 1 · 进行中 0 · 中断 2')
  })

  it('does not present missing usage as zero', () => {
    expect(tokenText(1500, 0)).toBe('—')
    expect(tokenText(1500, 8)).toBe('1,500')
    expect(tokenText(undefined, 8)).toBe('0')
  })

  it('describes usage coverage', () => {
    expect(coverageText(summary)).toBe('用量：完整 8 · 部分 1 · 未返回 3')
    expect(hasUnknownUsage(summary)).toBe(true)
    expect(hasUnknownUsage({ ...summary, unknown_usage_calls: 0 })).toBe(false)
  })
})

describe('usage labels', () => {
  it('maps sources, states and call statuses', () => {
    expect(sourceLabel('material_analysis')).toBe('素材分析')
    expect(sourceLabel('custom')).toBe('custom')
    expect(usageStateLabel('partial')).toBe('用量部分')
    expect(callStatusLabel('interrupted')).toBe('中断')
    expect(callStatusTone('error')).toBe('danger')
    expect(callStatusTone('unknown')).toBe('secondary')
  })

  it('labels group rows by dimension', () => {
    expect(groupRowLabel('day', { ...summary, key: '2026-11-24' })).toBe('2026-11-24')
    expect(groupRowLabel('run', { ...summary, key: 'abc123', label: '11-24 10:00 · abc12345' })).toBe(
      '11-24 10:00 · abc12345',
    )
    expect(groupRowLabel('task', { ...summary, key: 't1', label: '示例门店' })).toBe('示例门店')
    expect(groupRowLabel('model', { ...summary, key: 'openai/gpt-5' })).toBe('openai/gpt-5')
  })

  it('formats identifiers, elapsed time and model names', () => {
    expect(compactId('abcdef123456')).toBe('abcdef12…')
    expect(compactId('ab')).toBe('ab')
    expect(elapsedText(820)).toBe('820 ms')
    expect(elapsedText(15300)).toBe('15.3 s')
    expect(elapsedText(null)).toBe('—')
    expect(modelText({ provider: 'openai', model: 'gpt-5', returned_model: 'gpt-5-2026' })).toBe(
      'openai/gpt-5 → gpt-5-2026',
    )
    expect(modelText({ provider: 'openai', model: 'gpt-5', returned_model: 'gpt-5' })).toBe('openai/gpt-5')
  })

  it('produces a local ISO date', () => {
    expect(localDate(0)).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })
})
