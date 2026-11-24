import { useState } from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'

interface AppPaginationProps {
  page: number
  pageCount: number
  total: number
  pageSize: number
  pageSizeOptions?: number[]
  onPageChange: (page: number) => void
  onPageSizeChange?: (size: number) => void
  /** 大页数时显示首末页与快速跳转；默认页数超过 7 时开启。 */
  showQuickJump?: boolean
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
  showQuickJump,
}: AppPaginationProps) {
  const totalPages = Math.max(1, pageCount)
  const current = Math.min(Math.max(1, page), totalPages)
  const pages = pageWindow(current, totalPages)
  const quickJump = showQuickJump ?? totalPages > 7
  const [jumpTarget, setJumpTarget] = useState('')

  const jump = () => {
    const parsed = Number(jumpTarget)
    if (!Number.isFinite(parsed)) {
      setJumpTarget('')
      return
    }
    const next = Math.min(totalPages, Math.max(1, Math.trunc(parsed)))
    setJumpTarget('')
    if (next !== current) onPageChange(next)
  }

  return (
    <nav className="flex flex-wrap items-center justify-between gap-3 px-1 py-3" aria-label="分页">
      <span className="text-xs text-secondary">
        共 {total} 条 · 第 {current} / {totalPages} 页
      </span>
      <div className="flex flex-wrap items-center gap-2">
        {onPageSizeChange ? (
          <div className="w-30">
            <Select
              aria-label="每页条数"
              value={String(pageSize)}
              options={pageSizeOptions.map((size) => ({ value: String(size), label: `${size} 条/页` }))}
              onChange={(option) => onPageSizeChange(Number(option.value))}
            />
          </div>
        ) : null}
        {quickJump ? (
          <Button
            color="secondary"
            variant="outline"
            size="sm"
            disabled={current <= 1}
            onClick={() => onPageChange(1)}
          >
            首页
          </Button>
        ) : null}
        <Button
          color="secondary"
          variant="outline"
          size="sm"
          disabled={current <= 1}
          onClick={() => onPageChange(current - 1)}
        >
          上一页
        </Button>
        {pages.map((item) => (
          <Button
            key={item}
            color={item === current ? 'primary' : 'secondary'}
            variant={item === current ? 'solid' : 'ghost'}
            size="sm"
            aria-current={item === current ? 'page' : undefined}
            onClick={() => item !== current && onPageChange(item)}
          >
            {item}
          </Button>
        ))}
        <Button
          color="secondary"
          variant="outline"
          size="sm"
          disabled={current >= totalPages}
          onClick={() => onPageChange(current + 1)}
        >
          下一页
        </Button>
        {quickJump ? (
          <>
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              disabled={current >= totalPages}
              onClick={() => onPageChange(totalPages)}
            >
              末页
            </Button>
            <div className="flex items-center gap-1">
              <div className="w-16">
                <Input
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={totalPages}
                  aria-label="跳转到页码"
                  placeholder="页码"
                  value={jumpTarget}
                  onChange={(event) => setJumpTarget(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      jump()
                    }
                  }}
                />
              </div>
              <Button color="secondary" variant="outline" size="sm" disabled={!jumpTarget} onClick={jump}>
                跳转
              </Button>
            </div>
          </>
        ) : null}
      </div>
    </nav>
  )
}
