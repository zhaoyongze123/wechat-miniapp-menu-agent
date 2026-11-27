import { useEffect, useMemo, useState } from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { errorMessage } from '../../api/client'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { AppDialog } from '../../components/AppDialog'
import { AppPagination } from '../../components/AppPagination'
import { CellText } from '../../components/CellText'
import { SearchInput } from '../../components/SearchInput'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { fetchRejectionsExportPreview, fetchStoreNamesExportPreview } from './api'
import type { ExportPreviewKind, ExportPreviewResponse, RejectionQuery } from './types'

interface ExportPreviewDialogProps {
  open: boolean
  kind: ExportPreviewKind | null
  rejectionQuery: RejectionQuery
  projectId?: string
  exporting: boolean
  onConfirmExport: () => Promise<void>
  onOpenChange: (open: boolean) => void
}

type ExtractionFilter = 'all' | 'extracted' | 'missing'

interface PreviewRow {
  __index: number
  data: Record<string, string>
}

const EXTRACTION_OPTIONS = [
  { value: 'all', label: '全部提取状态' },
  { value: 'extracted', label: '已提取实际名称' },
  { value: 'missing', label: '未提取实际名称' },
]

function columnCssWidth(excelWidth: number, key: string): string {
  if (key === 'reason' || key === 'detail') return '22rem'
  if (key === 'task_id') return '15rem'
  if (key === 'store_name' || key === 'observed_store_name') return '14rem'
  const rem = Math.max(7.5, Math.min(20, Math.round((excelWidth / 2) * 10) / 10))
  return `${rem}rem`
}

function stateTone(value: string): 'success' | 'warning' | 'danger' | 'info' | 'secondary' {
  if (value.includes('成功') || value.includes('完成') || value.includes('已确认')) return 'success'
  if (value.includes('不合格') || value.includes('失败') || value.includes('异常')) return 'danger'
  if (value.includes('待检查') || value.includes('重试') || value.includes('阻塞')) return 'warning'
  if (value.includes('进行') || value.includes('采集')) return 'info'
  return 'secondary'
}

export function ExportPreviewDialog({
  open,
  kind,
  rejectionQuery,
  projectId,
  exporting,
  onConfirmExport,
  onOpenChange,
}: ExportPreviewDialogProps) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState<ExportPreviewResponse | null>(null)
  const [reloadSeed, setReloadSeed] = useState(0)

  const [search, setSearch] = useState('')
  const [stateFilter, setStateFilter] = useState('all')
  const [extractionFilter, setExtractionFilter] = useState<ExtractionFilter>('all')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)

  useEffect(() => {
    if (!open || !kind) return
    setSearch('')
    setStateFilter('all')
    setExtractionFilter('all')
    setPage(1)
  }, [open, kind])

  useEffect(() => {
    if (!open || !kind) return
    const controller = new AbortController()
    setLoading(true)
    setError('')
    setPreview(null)
    const request =
      kind === 'rejections'
        ? fetchRejectionsExportPreview(rejectionQuery, controller.signal, projectId)
        : fetchStoreNamesExportPreview(controller.signal, projectId)

    request
      .then((data) => {
        setPreview(data)
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        setError(errorMessage(err))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })

    return () => controller.abort()
  }, [open, kind, projectId, rejectionQuery, reloadSeed])

  const stateOptions = useMemo(() => {
    const entries = Object.entries(preview?.summary.states ?? {})
    return [
      { value: 'all', label: '全部采集状态' },
      ...entries.map(([label, count]) => ({ value: label, label: `${label}（${count}）` })),
    ]
  }, [preview])

  const filteredRows = useMemo<PreviewRow[]>(() => {
    const sourceRows = preview?.rows ?? []
    const needle = search.trim().toLowerCase()
    const result: PreviewRow[] = []
    for (let index = 0; index < sourceRows.length; index++) {
      const row = sourceRows[index]
      if (!row) continue
      if (kind === 'store-names') {
        if (stateFilter !== 'all' && (row.state || '未知') !== stateFilter) continue
        const hasObserved = Boolean((row.observed_store_name || '').trim())
        if (extractionFilter === 'extracted' && !hasObserved) continue
        if (extractionFilter === 'missing' && hasObserved) continue
      }
      if (needle) {
        const matched = Object.values(row).some((value) => String(value || '').toLowerCase().includes(needle))
        if (!matched) continue
      }
      result.push({ __index: index, data: row })
    }
    return result
  }, [preview, kind, search, stateFilter, extractionFilter])

  const pageCount = Math.max(1, Math.ceil(filteredRows.length / pageSize))
  const safePage = Math.min(page, pageCount)
  const pageRows = useMemo(() => {
    const start = (safePage - 1) * pageSize
    return filteredRows.slice(start, start + pageSize)
  }, [filteredRows, safePage, pageSize])

  useEffect(() => {
    setPage(1)
  }, [search, stateFilter, extractionFilter, pageSize])

  const columns = useMemo<DataColumn<PreviewRow>[]>(() => {
    const defs = preview?.columns ?? []
    return defs.map((col, idx) => {
      const base: DataColumn<PreviewRow> = {
        id: col.key,
        header: col.label,
        width: columnCssWidth(col.width, col.key),
        ...(idx === 0 ? { sticky: 'left' as const } : {}),
        cell: (row) => {
          const raw = String(row.data[col.key] ?? '').trim()
          if (col.key === 'state' || col.key === 'reviewed') {
            if (!raw) return <span className="text-tertiary">—</span>
            return <StatusBadge tone={stateTone(raw)}>{raw}</StatusBadge>
          }
          if (!raw) {
            return <span className="text-tertiary">—</span>
          }
          return (
            <CellText
              mono={col.key === 'task_id'}
              className={col.key === 'task_id' ? 'text-secondary' : 'text-sm text-default'}
            >
              {raw}
            </CellText>
          )
        },
      }
      return base
    })
  }, [preview])

  const hasLocalFilter = Boolean(search.trim()) || (kind === 'store-names' && (stateFilter !== 'all' || extractionFilter !== 'all'))
  const totalExportRows = preview?.total ?? 0
  const title = kind === 'store-names' ? '全量门店名称预览' : '不合格门店清单预览'
  const coverageText =
    kind === 'store-names' && preview && preview.total > 0
      ? `${(((preview.summary.extracted ?? 0) / preview.total) * 100).toFixed(1)}%`
      : '0.0%'

  const footer = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="text-xs text-secondary">
        {preview ? (
          hasLocalFilter ? (
            <span>
              当前预览筛选查看 <strong className="text-default">{filteredRows.length}</strong> 条，确认导出将包含全部{' '}
              <strong className="text-default">{totalExportRows}</strong> 条（{preview.filename}）
            </span>
          ) : (
            <span>
              确认导出将下载全部 <strong className="text-default">{totalExportRows}</strong> 条记录（{preview.filename}）
            </span>
          )
        ) : (
          <span>正在准备预览数据…</span>
        )}
      </div>
      <div className="flex items-center gap-2">
        <Button color="secondary" variant="outline" size="sm" disabled={exporting} onClick={() => onOpenChange(false)}>
          取消
        </Button>
        <Button
          color="primary"
          size="sm"
          disabled={loading || Boolean(error) || exporting || totalExportRows === 0}
          onClick={() => void onConfirmExport()}
        >
          {exporting ? '导出中…' : `确认导出 Excel（${totalExportRows} 条）`}
        </Button>
      </div>
    </div>
  )

  return (
    <AppDialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      widthClass="w-[min(1180px,95vw)]"
      busy={exporting}
      footer={footer}
    >
      <div className="space-y-4">
        {preview ? (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-subtle bg-surface-secondary px-4 py-2.5 text-xs">
            <div className="flex flex-wrap items-center gap-4 text-secondary">
              <span>
                导出文件：<strong className="text-default">{preview.filename}</strong>
              </span>
              <span>
                总计：<strong className="text-default">{preview.total}</strong> 条
              </span>
              {kind === 'store-names' ? (
                <>
                  <span>
                    已提取实际名称：
                    <strong className="text-default">
                      {preview.summary.extracted ?? 0} / {preview.total}（{coverageText}）
                    </strong>
                  </span>
                  <span>
                    未提取：<strong className="text-default">{preview.summary.missing ?? 0}</strong> 条
                  </span>
                </>
              ) : (
                <>
                  <span>
                    已人工确认：<strong className="text-default">{preview.summary.reviewed ?? 0}</strong> 条
                  </span>
                  <span>
                    未确认：<strong className="text-default">{preview.summary.unreviewed ?? 0}</strong> 条
                  </span>
                </>
              )}
            </div>
            <span className="text-tertiary">下方搜索与筛选仅用于预览核对，不影响导出范围</span>
          </div>
        ) : null}

        {preview && preview.total > 0 ? (
          <div className="flex flex-wrap items-center gap-2">
            <div className="min-w-60 flex-1">
              <SearchInput
                label="预览表格搜索"
                value={search}
                onChange={setSearch}
                placeholder={
                  kind === 'store-names'
                    ? '搜索任务编号、原始/实际门店名称或 reason…'
                    : '搜索门店、批次、原因、备注或实际观察名称…'
                }
              />
            </div>
            {kind === 'store-names' ? (
              <>
                <div className="w-44">
                  <Select
                    aria-label="按采集状态筛选预览"
                    value={stateFilter}
                    options={stateOptions}
                    onChange={(option) => setStateFilter(option.value)}
                  />
                </div>
                <div className="w-44">
                  <Select
                    aria-label="按名称提取情况筛选预览"
                    value={extractionFilter}
                    options={EXTRACTION_OPTIONS}
                    onChange={(option) => setExtractionFilter(option.value as ExtractionFilter)}
                  />
                </div>
              </>
            ) : null}
            {hasLocalFilter ? (
              <Button
                color="secondary"
                variant="ghost"
                size="sm"
                onClick={() => {
                  setSearch('')
                  setStateFilter('all')
                  setExtractionFilter('all')
                }}
              >
                清空筛选
              </Button>
            ) : null}
          </div>
        ) : null}

        {error ? (
          <StatePanel
            variant="error"
            title="预览加载失败"
            description={error}
            action={
              <Button color="secondary" variant="outline" size="sm" onClick={() => setReloadSeed((n) => n + 1)}>
                重试
              </Button>
            }
          />
        ) : (
          <>
            <AppDataTable
              columns={columns}
              rows={pageRows}
              rowKey={(row) => `${row.__index}`}
              caption={title}
              loading={loading}
              minWidthClass={kind === 'rejections' ? 'min-w-[1440px]' : 'min-w-[960px]'}
              emptyState={
                <StatePanel
                  variant="empty"
                  title={hasLocalFilter ? '当前筛选条件下没有匹配记录' : '暂无可导出数据'}
                  description={
                    hasLocalFilter
                      ? '可清空上方预览筛选查看全部待导出记录。'
                      : '当前范围没有可导出的门店记录。'
                  }
                />
              }
            />
            {preview && filteredRows.length > 0 ? (
              <AppPagination
                page={safePage}
                pageCount={pageCount}
                total={filteredRows.length}
                pageSize={pageSize}
                onPageChange={setPage}
                onPageSizeChange={setPageSize}
              />
            ) : null}
          </>
        )}
      </div>
    </AppDialog>
  )
}
