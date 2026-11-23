import { describe, expect, it } from 'vitest'
import { HUB_SELECTION_LIMIT, addSelection, removeSelection } from './selection'

describe('Hub 跨页选择', () => {
  it('跨页累加并去重，保持原有顺序', () => {
    expect(addSelection(['a', 'b'], ['b', 'c'])).toEqual({ ids: ['a', 'b', 'c'], overflow: 0 })
  })

  it('默认上限为 300 家', () => {
    expect(HUB_SELECTION_LIMIT).toBe(300)
  })

  it('达到上限后不再加入，并报告溢出数量', () => {
    const current = Array.from({ length: 299 }, (_, index) => `id-${index}`)
    const result = addSelection(current, ['extra-1', 'extra-2', 'id-0'])
    expect(result.ids).toHaveLength(300)
    expect(result.ids.at(-1)).toBe('extra-1')
    expect(result.overflow).toBe(1)
  })

  it('移除指定门店且不影响其他选择', () => {
    expect(removeSelection(['a', 'b', 'c'], ['b', 'x'])).toEqual(['a', 'c'])
  })
})
