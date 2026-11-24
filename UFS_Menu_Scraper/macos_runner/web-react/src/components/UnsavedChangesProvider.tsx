import { createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { ConfirmActionDialog } from './ConfirmActionDialog'

interface GuardContextValue {
  register: (id: string, message: string) => void
  unregister: (id: string) => void
}

const GuardContext = createContext<GuardContextValue | null>(null)

/**
 * 未保存修改的全局守卫：
 * - 刷新/关闭标签页使用 beforeunload 原生提示；
 * - 站内链接在捕获阶段拦截，确认后才跳转（HashRouter 下直接改 hash）。
 * 只登记“有真实未保存变更”的页面，不改变正常导航。
 */
export function UnsavedChangesProvider({ children }: { children: ReactNode }) {
  const guards = useRef(new Map<string, string>())
  const [, setRevision] = useState(0)
  const [pendingHash, setPendingHash] = useState<string | null>(null)

  const register = useCallback((id: string, message: string) => {
    if (guards.current.get(id) === message) return
    guards.current.set(id, message)
    setRevision((value) => value + 1)
  }, [])

  const unregister = useCallback((id: string) => {
    if (!guards.current.delete(id)) return
    setRevision((value) => value + 1)
  }, [])

  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!guards.current.size) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [])

  useEffect(() => {
    const onClickCapture = (event: MouseEvent) => {
      if (!guards.current.size) return
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
      const anchor = (event.target as HTMLElement | null)?.closest('a[href]') as HTMLAnchorElement | null
      if (!anchor || anchor.target || anchor.hasAttribute('download')) return
      const url = new URL(anchor.href, window.location.href)
      if (url.origin !== window.location.origin) return
      const nextHash = url.hash && url.hash !== '#' ? url.hash : `#${url.pathname}${url.search}`
      if (nextHash === window.location.hash) return
      event.preventDefault()
      event.stopPropagation()
      setPendingHash(nextHash)
    }
    document.addEventListener('click', onClickCapture, true)
    return () => document.removeEventListener('click', onClickCapture, true)
  }, [])

  const message = guards.current.values().next().value as string | undefined
  // context 值必须稳定，否则使用方 useEffect 会随每次渲染重新登记/注销，形成无限循环。
  const value = useMemo<GuardContextValue>(() => ({ register, unregister }), [register, unregister])

  return (
    <GuardContext.Provider value={value}>
      {children}
      <ConfirmActionDialog
        open={pendingHash !== null}
        title="放弃未保存的修改？"
        description={
          <div className="space-y-1">
            <p>{message ?? '当前页面有未保存的修改。'}</p>
            <p>离开后这些修改将丢失；需要保留请先返回并保存。</p>
          </div>
        }
        confirmLabel="放弃并离开"
        tone="danger"
        onOpenChange={(next) => {
          if (!next) setPendingHash(null)
        }}
        onConfirm={() => {
          const hash = pendingHash
          setPendingHash(null)
          if (hash) window.location.hash = hash
        }}
      />
    </GuardContext.Provider>
  )
}

/** 页面有未保存变更时登记守卫；active 变为 false 自动解除。 */
export function useUnsavedChanges(active: boolean, message = '当前页面有未保存的修改。') {
  const context = useContext(GuardContext)
  const id = useId()
  useEffect(() => {
    if (!context) return
    if (!active) {
      context.unregister(id)
      return
    }
    context.register(id, message)
    return () => context.unregister(id)
  }, [active, context, id, message])
}
