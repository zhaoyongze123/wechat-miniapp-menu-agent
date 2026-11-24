import type { ReactNode } from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'

interface BulkActionBarProps {
  /** 当前已选数量；为 0 时不渲染操作条。 */
  count: number
  /** 选择范围说明，例如“跨页保留”或“最多 300 家”。 */
  hint?: string
  onClear?: () => void
  children?: ReactNode
}

/** 批量操作条：固定在列表上方，数量、范围与操作同时可见，清空选择独立于清除筛选。 */
export function BulkActionBar({ count, hint, onClear, children }: BulkActionBarProps) {
  if (count <= 0) return null
  return (
    <div
      role="region"
      aria-label="批量操作"
      className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-subtle bg-surface-secondary px-3 py-2"
    >
      <span className="text-sm text-default">已选 {count} 项</span>
      {hint ? <span className="text-2xs text-tertiary">{hint}</span> : null}
      <div className="ml-auto flex flex-wrap items-center gap-2">
        {children}
        {onClear ? (
          <Button color="secondary" variant="ghost" size="sm" onClick={onClear}>
            清空选择
          </Button>
        ) : null}
      </div>
    </div>
  )
}
