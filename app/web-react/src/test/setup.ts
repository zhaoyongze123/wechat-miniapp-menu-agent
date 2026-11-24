import '@testing-library/jest-dom/vitest'

// Node 25 在未配置 --localstorage-file 时会暴露一个空的全局 localStorage，
// 遮蔽 jsdom 的实现并导致 Storage 方法缺失；CI 使用的 Node 22 不受影响。
if (typeof globalThis.localStorage?.clear !== 'function') {
  const data = new Map<string, string>()
  const storage = {
    get length() {
      return data.size
    },
    clear() {
      data.clear()
    },
    getItem(key: string) {
      return data.get(key) ?? null
    },
    key(index: number) {
      return Array.from(data.keys())[index] ?? null
    },
    removeItem(key: string) {
      data.delete(key)
    },
    setItem(key: string, value: string) {
      data.set(key, String(value))
    },
  } as Storage
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })
}
