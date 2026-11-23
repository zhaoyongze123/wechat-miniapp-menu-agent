import type { ReactNode } from 'react'

export type StateVariant = 'loading' | 'empty' | 'error' | 'info'

interface StatePanelProps {
  variant: StateVariant
  title: string
  description?: string
  action?: ReactNode
}

const TONE: Record<StateVariant, string> = {
  loading: 'border-shell-line/60 text-shell-muted',
  empty: 'border-shell-line/60 text-shell-muted',
  error: 'border-rose-500/40 text-rose-200',
  info: 'border-sky-500/40 text-sky-200',
}

/** 加载、空数据、无匹配、失败和权限提示的统一容器。 */
export function StatePanel({ variant, title, description, action }: StatePanelProps) {
  return (
    <div
      className={`flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed px-6 py-12 text-center ${TONE[variant]}`}
      role={variant === 'error' ? 'alert' : 'status'}
      aria-live={variant === 'error' ? 'assertive' : 'polite'}
    >
      {variant === 'loading' ? (
        <span
          className="mb-1 h-5 w-5 animate-spin rounded-full border-2 border-shell-line border-t-accent-400"
          aria-hidden="true"
        />
      ) : null}
      <strong className="text-base text-shell-text">{title}</strong>
      {description ? <span className="max-w-lg text-sm leading-relaxed">{description}</span> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  )
}
