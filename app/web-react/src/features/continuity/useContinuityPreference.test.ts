import { describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { adjacentVisibleKey, nextVisibleKey, useContinuityPreference } from './useContinuityPreference'

describe('nextVisibleKey', () => {
  const rows = [{ id: 'a' }, { id: 'c' }]
  const keyOf = (row: { id: string }) => row.id

  it('continues in the captured filtered order and skips rows removed after success', () => {
    expect(nextVisibleKey(['a', 'b', 'c'], 'a', rows, keyOf)).toBe('c')
  })

  it('does not guess a destination if the current item was concurrently removed', () => {
    expect(nextVisibleKey(['a', 'b', 'c'], 'missing', rows, keyOf)).toBeNull()
  })

  it('returns no destination at the end of the active scope', () => {
    expect(nextVisibleKey(['a', 'c'], 'c', rows, keyOf)).toBeNull()
  })

  it('can move back within the captured scope after the current row leaves its filter', () => {
    expect(adjacentVisibleKey(['a', 'b', 'c'], 'b', rows, keyOf, -1)).toBe('a')
  })
})

describe('useContinuityPreference', () => {
  it('defaults to off and remembers an explicit choice in this browser', () => {
    const values = new Map<string, string>()
    const storage: Storage = {
      get length() { return values.size },
      clear: () => values.clear(),
      getItem: (key) => values.get(key) ?? null,
      key: (index) => Array.from(values.keys())[index] ?? null,
      removeItem: (key) => values.delete(key),
      setItem: (key, value) => values.set(key, String(value)),
    }
    Object.defineProperty(window, 'localStorage', { configurable: true, value: storage })
    const { result, unmount } = renderHook(() => useContinuityPreference('test.continuity'))
    expect(result.current[0]).toBe(false)
    act(() => result.current[1](true))
    expect(result.current[0]).toBe(true)
    unmount()

    const reopened = renderHook(() => useContinuityPreference('test.continuity'))
    expect(reopened.result.current[0]).toBe(true)
    reopened.unmount()
  })
})
