import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Select } from '@openai/apps-sdk-ui/components/Select'

interface AppPaginationProps {
  page: number
  pageCount: number
  total: number
  pageSize: number
  pageSizeOptions?: number[]
  onPageChange: (page: number) => void
  onPageSizeChange?: (size: number) => void
}

/** 页码窗口：最多 7 个按钮，当前位置始终包含在内。 */
export function pageWindow(page: number, pageCount: number, width = 7): number[] {
  const total = Math.max(1, pageCount)
  const current = Math.min(Math.max(1, page), total)
  const size = Math.min(Math.max(1, width), total)
  let start = Math.max(1, current - Math.floor(size / 2))
  start = Math.min(start, total - size + 1)
  return Array.from({ length: size }, (_, index) => start + index)
}

export function AppPagination({
  page,
  pageCount,
  total,
  pageSize,
  pageSizeOptions = [20, 50, 100],
  onPageChange,
  onPageSizeChange,
}: AppPaginationProps) {
  const pages = pageWindow(page, pageCount)
  return (
    <nav className="flex flex-wrap items-center justify-between gap-3 px-1 py-3" aria-label="分页">
      <span className="text-xs text-secondary">
        共 {total} 条 · 第 {page} / {Math.max(1, pageCount)} 页
      </span>
      <div className="flex items-center gap-2">
        {onPageSizeChange ? (
          <div className="w-[116px]">
            <Select
              aria-label="每页条数"
              value={String(pageSize)}
              options={pageSizeOptions.map((size) => ({ value: String(size), label: `${size} 条/页` }))}
              onChange={(option) => onPageSizeChange(Number(option.value))}
            />
          </div>
        ) : null}
        <Button
          color="secondary"
          variant="outline"
          size="sm"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
        >
          上一页
        </Button>
        {pages.map((item) => (
          <Button
            key={item}
            color={item === page ? 'primary' : 'secondary'}
            variant={item === page ? 'solid' : 'ghost'}
            size="sm"
            aria-current={item === page ? 'page' : undefined}
            onClick={() => onPageChange(item)}
          >
            {item}
          </Button>
        ))}
        <Button
          color="secondary"
          variant="outline"
          size="sm"
          disabled={page >= pageCount}
          onClick={() => onPageChange(page + 1)}
        >
          下一页
        </Button>
      </div>
    </nav>
  )
}
