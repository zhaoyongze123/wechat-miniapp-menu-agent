import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'

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
        className="pointer-events-none fixed right-5 bottom-5 z-[60] flex w-[min(380px,90vw)] flex-col gap-2"
        role="status"
        aria-live="polite"
      >
        {items.map((item) => (
          <Alert
            key={item.id}
            color={item.tone}
            variant="soft"
            title={item.message}
            className="pointer-events-auto shadow-lg"
          />
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
