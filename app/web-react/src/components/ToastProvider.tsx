import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'

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

/** 成功短暂、普通提示适中、失败停留更久；所有提示都可手动关闭。 */
const DURATION: Record<ToastTone, number> = { success: 3000, info: 4500, danger: 8000 }
const MAX_VISIBLE = 4

/** 全局状态反馈：成功、失败和提示共用同一区域，读屏通过 aria-live 播报。 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const itemsRef = useRef<ToastItem[]>([])
  const timers = useRef(new Map<string, number>())

  const replaceItems = useCallback((next: ToastItem[]) => {
    itemsRef.current = next
    setItems(next)
  }, [])

  const scheduleDismiss = useCallback(
    (id: string, tone: ToastTone) => {
      const existing = timers.current.get(id)
      if (existing) window.clearTimeout(existing)
      const timer = window.setTimeout(() => {
        timers.current.delete(id)
        replaceItems(itemsRef.current.filter((item) => item.id !== id))
      }, DURATION[tone])
      timers.current.set(id, timer)
    },
    [replaceItems],
  )

  const dismiss = useCallback(
    (id: string) => {
      const timer = timers.current.get(id)
      if (timer) window.clearTimeout(timer)
      timers.current.delete(id)
      replaceItems(itemsRef.current.filter((item) => item.id !== id))
    },
    [replaceItems],
  )

  const push = useCallback(
    (message: string, tone: ToastTone = 'info') => {
      // 同一提示重复触发时只保留一个实例，并重新计时，避免刷屏。
      const duplicate = itemsRef.current.find((item) => item.message === message && item.tone === tone)
      if (duplicate) {
        scheduleDismiss(duplicate.id, tone)
        return
      }
      const id =
        typeof crypto !== 'undefined' && 'randomUUID' in crypto
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random()}`
      const next = [...itemsRef.current, { id, tone, message }]
      const overflow = next.slice(0, Math.max(0, next.length - MAX_VISIBLE))
      for (const item of overflow) {
        const timer = timers.current.get(item.id)
        if (timer) window.clearTimeout(timer)
        timers.current.delete(item.id)
      }
      replaceItems(next.slice(-MAX_VISIBLE))
      scheduleDismiss(id, tone)
    },
    [replaceItems, scheduleDismiss],
  )

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
          <div key={item.id} className="pointer-events-auto relative">
            <Alert
              color={item.tone}
              variant="soft"
              title={item.message}
              className="pr-12 shadow-lg"
            />
            <Button
              type="button"
              color="secondary"
              variant="ghost"
              size="sm"
              aria-label="关闭提示"
              className="absolute top-1.5 right-1.5"
              onClick={() => dismiss(item.id)}
            >
              ×
            </Button>
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
