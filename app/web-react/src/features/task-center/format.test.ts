import { describe, expect, it } from 'vitest'
import { formatTime, shortTime, statusOf } from './format'

describe('任务状态口径', () => {
  it('未结束状态映射为青色提示', () => {
    expect(statusOf('running')).toEqual({ label: '运行中', tone: 'info' })
    expect(statusOf('pause_requested').label).toBe('暂停中')
  })

  it('未知状态保留原文而不是伪装成完成', () => {
    expect(statusOf('mystery')).toEqual({ label: 'mystery', tone: 'secondary' })
    expect(statusOf(undefined).label).toBe('未知')
  })
})

describe('时间格式化', () => {
  it('缺失或非法时间显示占位符', () => {
    expect(formatTime(undefined)).toBe('—')
    expect(formatTime('not-a-date')).toBe('not-a-date')
  })

  it('事件时间只取时分秒', () => {
    expect(shortTime('2026-11-23T10:11:12+08:00')).toBe('10:11:12')
    expect(shortTime(undefined)).toBe('')
  })
})
