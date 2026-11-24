import type { ReactNode } from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'

interface FilterToolbarProps {
  children: ReactNode
  label: string
  /** 活动筛选条件数量；大于 0 时提供一键清除。 */
  activeCount?: number
  onClear?: () => void
  /** 当前筛选范围说明，例如“当前批次 · 未发布”。 */
  summary?: string
}

/**
 * 筛选工具条：统一布局、范围说明与清除入口。
 * 只清除筛选条件，不触碰批量选择与分页之外的业务状态。
 */
export function FilterToolbar({ children, label, activeCount = 0, onClear, summary }: FilterToolbarProps) {
  return (
    <div role="group" aria-label={label} className="space-y-2">
      <div className="flex min-w-0 flex-wrap items-end gap-2">{children}</div>
      {summary || (activeCount > 0 && onClear) ? (
        <div className="flex flex-wrap items-center gap-2 text-2xs text-tertiary">
          {summary ? <span>{summary}</span> : null}
          {activeCount > 0 && onClear ? (
            <>
              <span>已应用 {activeCount} 个筛选条件</span>
              <Button color="secondary" variant="ghost" size="sm" onClick={onClear}>
                清除筛选
              </Button>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
