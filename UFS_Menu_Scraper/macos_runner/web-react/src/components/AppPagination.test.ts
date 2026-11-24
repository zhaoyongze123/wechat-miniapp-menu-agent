import { describe, expect, it } from 'vitest'
import { pageWindow } from './AppPagination'

describe('pageWindow', () => {
  it('首页从 1 开始并保持固定宽度', () => {
    expect(pageWindow(1, 10)).toEqual([1, 2, 3, 4, 5, 6, 7])
  })

  it('末页不被截断', () => {
    expect(pageWindow(10, 10)).toEqual([4, 5, 6, 7, 8, 9, 10])
  })

  it('总页数少于窗口宽度时只返回真实页码', () => {
    expect(pageWindow(2, 3)).toEqual([1, 2, 3])
  })

  it('越界页码收敛到合法范围', () => {
    expect(pageWindow(99, 2)).toEqual([1, 2])
  })
})
