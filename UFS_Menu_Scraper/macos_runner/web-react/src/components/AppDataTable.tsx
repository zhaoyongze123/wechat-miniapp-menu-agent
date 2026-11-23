import type { ReactNode } from 'react'
import { StatePanel } from './StatePanel'

export interface DataColumn<T> {
  id: string
  header: ReactNode
  cell: (row: T) => ReactNode
  className?: string
  headerClassName?: string
}

interface AppDataTableProps<T> {
  columns: DataColumn<T>[]
  rows: T[]
  rowKey: (row: T) => string
  caption: string
  loading?: boolean
  emptyState?: ReactNode
  onRowActivate?: (row: T) => void
  /** 表格最小宽度；抽屉等窄容器可传入更小的值避免横向滚动。 */
  minWidthClass?: string
}

/**
 * 语义表格：表头吸顶、内容区横向滚动，行内容由列定义决定。
 * 分页与筛选属于调用方状态，表格本身不发请求。
 */
export function AppDataTable<T>({
  columns,
  rows,
  rowKey,
  caption,
  loading = false,
  emptyState,
  onRowActivate,
  minWidthClass = 'min-w-[880px]',
}: AppDataTableProps<T>) {
  const showEmpty = !loading && rows.length === 0
  return (
    <div className="relative overflow-x-auto rounded-xl border border-line-subtle">
      <table className={`w-full ${minWidthClass} border-collapse text-left text-sm`}>
        <caption className="sr-only">{caption}</caption>
        <thead className="sticky top-0 z-10 bg-canvas">
          <tr>
            {columns.map((column) => (
              <th
                key={column.id}
                scope="col"
                className={`border-b border-line-subtle px-3 py-2.5 text-xs font-medium whitespace-nowrap text-ink-muted ${column.headerClassName ?? ''}`}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const key = rowKey(row)
            return (
              <tr
                key={key}
                className={[
                  'border-b border-line-subtle align-top last:border-b-0',
                  onRowActivate ? 'cursor-pointer hover:bg-hover' : '',
                ].join(' ')}
                onClick={onRowActivate ? () => onRowActivate(row) : undefined}
              >
                {columns.map((column) => (
                  <td key={column.id} className={`px-3 py-3 ${column.className ?? ''}`}>
                    {column.cell(row)}
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
      {loading ? (
        <div className="absolute inset-0 flex items-center justify-center bg-panel/80 text-sm text-ink-muted">
          正在读取任务数据…
        </div>
      ) : null}
      {showEmpty ? <div className="p-2">{emptyState ?? <StatePanel variant="empty" title="没有数据" />}</div> : null}
    </div>
  )
}
