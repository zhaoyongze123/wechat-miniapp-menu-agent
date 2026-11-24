import type { ReactNode } from 'react'
import { StatePanel } from './StatePanel'

export interface DataColumn<T> {
  id: string
  header: ReactNode
  cell: (row: T) => ReactNode
  className?: string
  headerClassName?: string
  /** 列宽（CSS 长度，如 `12rem`、`9.5rem`）。配置后表格使用固定布局，未配置的列平分剩余空间。 */
  width?: string
  /** 横向滚动时将本列固定在左侧或右侧；同一侧只应固定一列。 */
  sticky?: 'left' | 'right'
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
  /**
   * 表格布局。默认在任一列配置 width 时使用 fixed，避免自动布局被长文本撑开；
   * 无宽度配置的简单表格保持 auto，避免固定列宽裁切操作按钮。
   */
  layout?: 'auto' | 'fixed'
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
  minWidthClass = 'min-w-3xl',
  layout,
}: AppDataTableProps<T>) {
  const showEmpty = !loading && rows.length === 0
  const fixedLayout = layout ? layout === 'fixed' : columns.some((column) => column.width)
  return (
    <div className="relative overflow-x-auto rounded-xl border border-subtle">
      <table
        className={[
          'w-full border-collapse text-left text-sm',
          minWidthClass,
          fixedLayout ? 'table-fixed' : '',
        ].join(' ')}
      >
        <caption className="sr-only">{caption}</caption>
        {fixedLayout ? (
          <colgroup>
            {columns.map((column) => (
              <col key={column.id} style={column.width ? { width: column.width } : undefined} />
            ))}
          </colgroup>
        ) : null}
        <thead className="sticky top-0 z-20 bg-surface-secondary">
          <tr>
            {columns.map((column) => (
              <th
                key={column.id}
                scope="col"
                className={[
                  'border-b border-subtle px-4 py-3 text-xs whitespace-nowrap text-secondary',
                  stickyClass(column, true),
                  column.headerClassName ?? '',
                ].join(' ')}
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
                  'group border-b border-subtle align-top last:border-b-0',
                  onRowActivate
                    ? 'cursor-pointer hover:bg-secondary-ghost-hover focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[var(--color-ring)]'
                    : '',
                ].join(' ')}
                tabIndex={onRowActivate ? 0 : undefined}
                onClick={
                  onRowActivate
                    ? (event) => {
                        // 复选框、链接和行内按钮保持独立语义，不触发行详情。
                        if (isInteractiveTarget(event.target)) return
                        onRowActivate(row)
                      }
                    : undefined
                }
                onKeyDown={
                  onRowActivate
                    ? (event) => {
                        if (event.target !== event.currentTarget) return
                        if (event.key !== 'Enter' && event.key !== ' ') return
                        event.preventDefault()
                        onRowActivate(row)
                      }
                    : undefined
                }
              >
                {columns.map((column) => (
                  <td
                    key={column.id}
                    className={[
                      'px-4 py-3',
                      stickyClass(column, false),
                      column.className ?? '',
                    ].join(' ')}
                  >
                    {column.cell(row)}
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
      {loading ? (
        <div className="absolute inset-0 z-40 flex items-center justify-center bg-surface text-sm text-secondary">
          正在读取任务数据…
        </div>
      ) : null}
      {showEmpty ? <div className="p-2">{emptyState ?? <StatePanel variant="empty" title="没有数据" />}</div> : null}
    </div>
  )
}

/**
 * 固定列需要不透明背景，否则横向滚动时会透出下层内容；
 * 表头使用次级背景并提高层级，避免与吸顶表头互相覆盖。
 */
function stickyClass<T>(column: DataColumn<T>, header: boolean): string {
  if (!column.sticky) return ''
  const side = column.sticky === 'left' ? 'left-0 border-r' : 'right-0 border-l'
  if (header) return `sticky ${side} z-30 bg-surface-secondary border-subtle`
  return `sticky ${side} z-10 bg-surface group-hover:bg-secondary-ghost-hover border-subtle`
}

/** 行内可交互元素自行处理点击/键盘，避免行点击吞掉复选框或按钮语义。 */
function isInteractiveTarget(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    Boolean(target.closest('a,button,input,select,textarea,label,[role="checkbox"],[role="button"],[role="switch"]'))
  )
}
