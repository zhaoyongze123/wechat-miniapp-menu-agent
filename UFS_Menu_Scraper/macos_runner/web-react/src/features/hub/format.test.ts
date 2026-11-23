import { describe, expect, it } from 'vitest'
import { assignmentStatusOf, deviceStateOf, formatStore, isOnline, storeStateOf } from './format'

describe('formatStore', () => {
  it('解析标准门店目录', () => {
    expect(formatStore('SH001__上海市__人民广场店__31.23,121.47')).toEqual({
      name: '人民广场店',
      code: 'SH001',
    })
  })

  it('兼容带 LCA 段的目录', () => {
    expect(formatStore('BJ002__LCA1__北京市__朝阳大悦城店__39.92,116.46')).toEqual({
      name: '朝阳大悦城店',
      code: 'BJ002',
    })
  })

  it('无法识别时原样返回，不截断真实目录名', () => {
    expect(formatStore('临时门店目录')).toEqual({ name: '临时门店目录', code: '' })
  })
})

describe('状态映射', () => {
  it('优先使用服务端 store_state', () => {
    expect(storeStateOf({ store_state: 'returned' }).label).toBe('待重新选码')
  })

  it('缺少 store_state 时按任务状态回退', () => {
    expect(storeStateOf({ status: 'captured' }).label).toBe('已截图待复核')
    expect(storeStateOf({ status: 'needs_review' }).label).toBe('待检查')
  })

  it('历史留档优先于分配状态', () => {
    expect(assignmentStatusOf('reclaimed', 'history').label).toBe('旧结果留档')
    expect(assignmentStatusOf('ready').label).toBe('已下载')
  })
})

describe('设备状态', () => {
  const now = Date.parse('2026-11-23T10:00:00Z')

  it('60 秒内的心跳视为在线', () => {
    expect(isOnline('2026-11-23T09:59:30Z', now)).toBe(true)
    expect(isOnline('2026-11-23T09:58:00Z', now)).toBe(false)
  })

  it('解除配对与移除状态优先', () => {
    expect(deviceStateOf({ revoked_at: '2026-11-20T00:00:00Z' }, now).key).toBe('revoked')
    expect(deviceStateOf({ archived_at: '2026-11-20T00:00:00Z' }, now).key).toBe('archived')
    expect(deviceStateOf({ last_seen: '2026-11-23T09:59:30Z' }, now).key).toBe('online')
  })
})
