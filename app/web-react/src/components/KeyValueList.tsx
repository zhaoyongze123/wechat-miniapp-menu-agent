import type { ReactNode } from 'react'
import { CellText } from './CellText'
import { CopyButton } from './CopyButton'

export interface KeyValueItem {
  label: string
  value: ReactNode
  /** 需要复制时提供原始文本；value 可同时是格式化展示。 */
  copy?: string
  mono?: boolean
}

/** 详情网格中的单个键值项；调用方只负责栅格容器，标签/字号/换行与 KeyValueList 一致。 */
export function KeyValuePair({
  label,
  children,
  empty = '—',
}: {
  label: string
  children: ReactNode
  /** 空值占位；仅对 null/undefined/空字符串生效，0 正常显示。 */
  empty?: ReactNode
}) {
  const isEmpty = children === null || children === undefined || children === ''
  return (
    <div className="min-w-0">
      <dt className="text-xs text-secondary">{label}</dt>
      <dd className="mt-0.5 break-words text-sm text-default">{isEmpty ? empty : children}</dd>
    </div>
  )
}

interface KeyValueListProps {
  items: KeyValueItem[]
  className?: string
}

/** 详情元信息：统一标签宽度、长值省略与复制入口，避免每个抽屉各写一套。 */
export function KeyValueList({ items, className = '' }: KeyValueListProps) {
  return (
    <dl className={`grid gap-x-4 gap-y-2 text-xs sm:grid-cols-[7.5rem_minmax(0,1fr)] ${className}`}>
      {items.map((item) => (
        <div key={item.label} className="contents">
          <dt className="text-secondary">{item.label}</dt>
          <dd className="flex min-w-0 items-center gap-1 text-default">
            <div className="min-w-0 flex-1">
              {typeof item.value === 'string' ? (
                <CellText {...(item.mono ? { mono: true } : {})}>{item.value}</CellText>
              ) : (
                item.value
              )}
            </div>
            {item.copy ? <CopyButton value={item.copy} label={item.label} className="shrink-0" /> : null}
          </dd>
        </div>
      ))}
    </dl>
  )
}
