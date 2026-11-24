import { describe, expect, it } from 'vitest'
import { checkStatusMeta, connectionStatusMeta, settingsPaneOf, testTargetOf } from './format'

describe('环境诊断口径', () => {
  it('未知状态不被误报为正常', () => {
    expect(checkStatusMeta('ready').tone).toBe('success')
    expect(checkStatusMeta('warning').tone).toBe('warning')
    expect(checkStatusMeta('brand-new')).toEqual({ label: 'brand-new', tone: 'secondary' })
    expect(connectionStatusMeta(undefined).label).toBe('状态未知')
  })

  it('测试入口只接受服务端支持的检查项', () => {
    expect(testTargetOf('windows_desktop')).toBe('windows_desktop')
    expect(testTargetOf(undefined, 'engine')).toBe('windows_desktop')
    expect(testTargetOf(undefined, 'model')).toBe('model_service')
    expect(testTargetOf('engine', 'engine')).toBe('windows_desktop')
    expect(testTargetOf('custom_check')).toBeNull()
  })

  it('系统设置只接受已知授权面板', () => {
    expect(settingsPaneOf('accessibility')).toBe('accessibility')
    expect(settingsPaneOf('screen_recording')).toBe('screen_recording')
    expect(settingsPaneOf('unknown')).toBeNull()
  })
})
