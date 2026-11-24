import { useEffect, useState } from 'react'

/** 连续处理是本浏览器的个人偏好，默认关闭；存储不可用时仍可在当前页面开关。 */
export function useContinuityPreference(key: string) {
  const [enabled, setEnabled] = useState(() => {
    if (typeof window === 'undefined') return false
    try {
      return window.localStorage.getItem(key) === 'on'
    } catch {
      return false
    }
  })

  useEffect(() => {
    if (typeof window === 'undefined') return
    try {
      window.localStorage.setItem(key, enabled ? 'on' : 'off')
    } catch {
      // 浏览器隐私策略禁用存储时，保留当前页面状态即可。
    }
  }, [enabled, key])

  return [enabled, setEnabled] as const
}

/** 从当前项在最近一次可见顺序中的后面开始找仍存在的下一项。 */
export function nextVisibleKey<T>(
  orderedKeys: readonly string[],
  currentKey: string,
  available: readonly T[],
  keyOf: (item: T) => string,
): string | null {
  const currentIndex = orderedKeys.indexOf(currentKey)
  if (currentIndex < 0) return null
  const stillAvailable = new Set(available.map(keyOf))
  for (let index = currentIndex + 1; index < orderedKeys.length; index += 1) {
    const candidate = orderedKeys[index]
    if (candidate && stillAvailable.has(candidate)) return candidate
  }
  return null
}

export function adjacentVisibleKey<T>(
  orderedKeys: readonly string[],
  currentKey: string,
  available: readonly T[],
  keyOf: (item: T) => string,
  direction: -1 | 1,
): string | null {
  const currentIndex = orderedKeys.indexOf(currentKey)
  if (currentIndex < 0) return null
  const stillAvailable = new Set(available.map(keyOf))
  for (let index = currentIndex + direction; index >= 0 && index < orderedKeys.length; index += direction) {
    const candidate = orderedKeys[index]
    if (candidate && stillAvailable.has(candidate)) return candidate
  }
  return null
}
