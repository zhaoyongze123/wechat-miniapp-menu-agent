import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'

export type ToastTone = 'success' | 'danger' | 'info'

interface ToastItem {
  id: string
  tone: ToastTone
  message: string
}

interface ToastContextValue {
  push: (message: string, tone?: ToastTone) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

/** 全局状态反馈：成功、失败和提示共用同一区域，读屏通过 aria-live 播报。 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const timers = useRef(new Map<string, number>())

  const push = useCallback((message: string, tone: ToastTone = 'info') => {
    const id = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`
    setItems((current) => [...current, { id, tone, message }])
    const timer = window.setTimeout(() => {
      setItems((current) => current.filter((item) => item.id !== id))
      timers.current.delete(id)
    }, 4000)
    timers.current.set(id, timer)
  }, [])

  const value = useMemo<ToastContextValue>(() => ({ push }), [push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        className="pointer-events-none fixed bottom-5 right-5 z-[60] flex w-[min(360px,90vw)] flex-col gap-2"
        role="status"
        aria-live="polite"
      >
        {items.map((item) => (
          <div
            key={item.id}
            className={[
              'pointer-events-auto rounded-lg border px-4 py-3 text-sm shadow-lg backdrop-blur',
              item.tone === 'success'
                ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-100'
                : item.tone === 'danger'
                  ? 'border-rose-500/40 bg-rose-500/15 text-rose-100'
                  : 'border-sky-500/40 bg-sky-500/15 text-sky-100',
            ].join(' ')}
          >
            {item.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  const value = useContext(ToastContext)
  if (!value) throw new Error('useToast 必须在 ToastProvider 内使用')
  return value
}
