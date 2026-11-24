import type { ReactNode } from 'react'

export function FilterToolbar({ children, label }: { children: ReactNode; label: string }) {
  return <div role="group" aria-label={label} className="flex min-w-0 flex-wrap items-end gap-2">{children}</div>
}
