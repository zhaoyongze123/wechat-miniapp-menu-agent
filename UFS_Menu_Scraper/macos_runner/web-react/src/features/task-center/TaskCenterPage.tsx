import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { Badge } from '@openai/apps-sdk-ui/components/Badge'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { SegmentedControl } from '@openai/apps-sdk-ui/components/SegmentedControl'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { TextLink } from '@openai/apps-sdk-ui/components/TextLink'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { AppPagination } from '../../components/AppPagination'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { errorMessage } from '../../api/client'
import { useRole } from '../../app/RoleProvider'
import { ActiveTasksPanel } from './ActiveTasksPanel'
import { TaskDetailDrawer } from './TaskDetailDrawer'
import { fetchTaskHistory } from './api'
import { KIND_LABEL, formatTime, statusOf } from './format'
import type { ActiveTask, HistoryRecord, HistoryResponse, TaskFilters } from './types'

const STATUS_OPTIONS = [
  { value: 'all', label: '全部状态' },
  { value: 'running', label: '未结束' },
  { value: 'terminated', label: '已终止' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败/取消' },
  { value: 'paused', label: '已暂停' },
]

const PAGE_SIZES = [20, 50, 100]

function parseKind(value: string | null): TaskFilters['kind'] {
  return value === 'capture' || value === 'materials' ? value : 'all'
}

function parsePositiveInt(value: string | null, fallback: number): number {
  const parsed = Number.parseInt(value ?? '', 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback
}

function parseDetail(value: string | null): { kind: string; id: string } | null {
  if (!value) return null
  const index = value.indexOf(':')
  if (index <= 0 || index === value.length - 1) return null
  return { kind: value.slice(0, index), id: value.slice(index + 1) }
}

export function TaskCenterPage() {
  const role = useRole()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  const kind = parseKind(searchParams.get('kind'))
  const status = searchParams.get('status') ?? 'all'
  const q = searchParams.get('q') ?? ''
  const page = parsePositiveInt(searchParams.get('page'), 1)
  const pageSize = PAGE_SIZES.includes(parsePositiveInt(searchParams.get('size'), 20))
    ? parsePositiveInt(searchParams.get('size'), 20)
    : 20
  const filters = useMemo<TaskFilters>(
    () => ({ kind, status, q, page, pageSize }),
    [kind, status, q, page, pageSize],
  )

  const updateQuery = useCallback(
    (patch: Record<string, string>, options: { resetPage?: boolean } = {}) => {
      setSearchParams(
        (previous) => {
          const next = new URLSearchParams(previous)
          for (const [key, value] of Object.entries(patch)) {
            if (!value || value === 'all') next.delete(key)
            else next.set(key, value)
          }
          if (options.resetPage !== false) next.delete('page')
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  // 搜索框输入只做本地防抖，真正发请求仍由 URL 上的 q 决定，刷新后条件不丢。
  const [queryText, setQueryText] = useState(q)
  useEffect(() => setQueryText(q), [q])
  useEffect(() => {
    if (queryText.trim() === q.trim()) return
    const timer = window.setTimeout(() => updateQuery({ q: queryText.trim() }), 250)
    return () => window.clearTimeout(timer)
  }, [queryText, q, updateQuery])

  const historyQuery = useQuery({
    queryKey: ['task-history', filters.kind, filters.status, filters.q, filters.page, filters.pageSize],
    queryFn: ({ signal }) => fetchTaskHistory(filters, signal),
    placeholderData: keepPreviousData,
    enabled: role.role !== 'standalone',
    staleTime: 5_000,
  })

  // 服务端会按真实总数收敛页码；把收敛结果写回 URL，避免刷新后停在空页。
  const serverPage = historyQuery.data?.page
  useEffect(() => {
    if (serverPage && serverPage !== page) updateQuery({ page: String(serverPage) }, { resetPage: false })
  }, [serverPage, page, updateQuery])

  const selection = useMemo(() => parseDetail(searchParams.get('detail')), [searchParams])
  const savedScroll = useRef(0)
  const triggerRef = useRef<HTMLElement | null>(null)
  const previousSelection = useRef(selection)

  // 关闭详情（按钮或浏览器后退）后恢复打开前的滚动位置与触发按钮焦点。
  useEffect(() => {
    if (previousSelection.current && !selection) {
      const main = document.getElementById('app-main')
      if (main) main.scrollTop = savedScroll.current
      triggerRef.current?.focus({ preventScroll: true })
    }
    previousSelection.current = selection
  }, [selection])

  const openDetail = useCallback(
    (detailKind: string, id: string) => {
      const main = document.getElementById('app-main')
      savedScroll.current = main?.scrollTop ?? 0
      triggerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
      setSearchParams((previous) => {
        const next = new URLSearchParams(previous)
        next.set('detail', `${detailKind}:${id}`)
        return next
      })
    },
    [setSearchParams],
  )

  const closeDetail = useCallback(() => {
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous)
        next.delete('detail')
        return next
      },
      { replace: true },
    )
  }, [setSearchParams])

  const openActive = useCallback(
    (task: ActiveTask) => {
      openDetail(task.source === 'capture' ? 'capture' : 'materials', task.id)
    },
    [openDetail],
  )

  const handleRenamed = useCallback(
    (updated: HistoryRecord) => {
      queryClient.setQueryData<HistoryResponse>(
        ['task-history', filters.kind, filters.status, filters.q, filters.page, filters.pageSize],
        (current) =>
          current
            ? {
                ...current,
                records: current.records.map((record) => {
                  if (record.kind !== 'capture' || record.id !== updated.id) return record
                  const next: HistoryRecord = { ...record }
                  if (updated.title !== undefined) next.title = updated.title
                  if (updated.name !== undefined) next.name = updated.name
                  return next
                }),
              }
            : current,
      )
      void queryClient.invalidateQueries({ queryKey: ['task-history'] })
    },
    [queryClient, filters],
  )

  if (role.loading) {
    return <StatePanel variant="loading" title="正在确认当前角色…" />
  }

  if (role.error) {
    return (
      <StatePanel
        variant="error"
        title="无法确认当前服务角色"
        description={role.error}
        action={
          <Button color="secondary" variant="outline" size="sm" onClick={role.reload}>
            重试
          </Button>
        }
      />
    )
  }

  if (role.role === 'standalone') {
    return (
      <StatePanel
        variant="info"
        title="当前为本机独立模式"
        description="React 控制台一期只支持共享后台（Hub）与 Windows 原生采集端。本机独立模式请继续使用旧版控制台。"
        action={
          <TextLink href="/#task-center" className="text-sm">
            打开旧版任务中心
          </TextLink>
        }
      />
    )
  }

  const data = historyQuery.data
  const records = data?.records ?? []
  const summary = data?.summary ?? {}
  const total = Number(summary.total) || 0
  const hasFilters = kind !== 'all' || status !== 'all' || q.trim() !== ''
  const scopeText =
    role.role === 'hub'
      ? '统计后台素材分析与后台保留的采集运行记录；按开始时间倒序。'
      : '按开始时间倒序查看本机任务；进度与结果均来自本机服务。'

  const columns: DataColumn<HistoryRecord>[] = [
    {
      id: 'title',
      header: '任务名称',
      className: 'min-w-50 max-w-80',
      cell: (record) => (
        <div className="min-w-0">
          <span className="block truncate" title={`${record.title ?? record.id} · ${record.id}`}>
            {record.title || record.id}
          </span>
          <span className="font-mono mt-0.5 block truncate text-2xs text-tertiary">{record.id}</span>
          {kind === 'all' ? (
            <span className="mt-1 inline-block">
              <Badge color="secondary" variant="soft" size="sm">
                {KIND_LABEL[record.kind] ?? record.kind}
              </Badge>
            </span>
          ) : null}
        </div>
      ),
    },
    {
      id: 'batch',
      header: '批次范围',
      className: 'min-w-32',
      cell: (record) => <BatchCell record={record} />,
    },
    {
      id: 'status',
      header: '任务状态',
      className: 'whitespace-nowrap',
      cell: (record) => {
        const statusMeta = statusOf(record.status)
        return <StatusBadge tone={statusMeta.tone}>{statusMeta.label}</StatusBadge>
      },
    },
    {
      id: 'result',
      header: '结果摘要',
      className: 'min-w-42',
      cell: (record) => <ResultCell record={record} />,
    },
    {
      id: 'created',
      header: '开始时间',
      className: 'whitespace-nowrap text-secondary',
      cell: (record) => formatTime(record.created_at),
    },
    {
      id: 'actions',
      header: '操作',
      className: 'whitespace-nowrap',
      cell: (record) => (
        <Button
          color="secondary"
          variant="outline"
          size="sm"
          onClick={(event) => {
            event.stopPropagation()
            openDetail(record.kind, record.id)
          }}
        >
          详情
        </Button>
      ),
    },
  ]

  const selectedSummary =
    records.find((record) => record.kind === selection?.kind && record.id === selection?.id) ?? null

  return (
    <section className="space-y-6" aria-labelledby="task-center-heading">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 id="task-center-heading" className="heading-md">
            任务中心
          </h1>
          <p className="mt-1 text-sm text-secondary">{scopeText}</p>
        </div>
        <div className="flex items-center gap-3">
          <Button
            color="secondary"
            variant="outline"
            size="sm"
            onClick={() => {
              void historyQuery.refetch()
              void queryClient.invalidateQueries({ queryKey: ['tasks-active'] })
            }}
          >
            刷新
          </Button>
        </div>
      </header>

      <section className="space-y-3" aria-labelledby="active-heading">
        <h2 id="active-heading" className="text-sm text-secondary">
          运行中与最近结束
        </h2>
        <ActiveTasksPanel onOpen={openActive} />
      </section>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-5" aria-label="任务统计">
        <StatCard label="全部任务" value={total} />
        <StatCard label="未结束" value={Number(summary.running) || 0} />
        <StatCard label="已完成" value={Number(summary.completed) || 0} />
        <StatCard label="已终止" value={Number(summary.terminated) || 0} />
        <StatCard label="失败/取消" value={Number(summary.failed) || 0} />
      </section>

      <section className="rounded-xl border border-subtle bg-surface space-y-4 p-4 md:p-5" aria-labelledby="history-heading">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 id="history-heading" className="heading-xs">
              任务历史
            </h2>
            <p className="mt-0.5 text-xs text-secondary">筛选与页码保存在地址栏，刷新或返回后保持原条件。</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <SegmentedControl
              aria-label="任务类型"
              value={kind}
              onChange={(next) => updateQuery({ kind: next })}
              size="sm"
              pill
            >
              <SegmentedControl.Option value="all">全部类型</SegmentedControl.Option>
              <SegmentedControl.Option value="capture">采集运行</SegmentedControl.Option>
              <SegmentedControl.Option value="materials">素材分析</SegmentedControl.Option>
            </SegmentedControl>
            <div className="w-35">
              <Select
                aria-label="任务状态"
                value={status}
                options={STATUS_OPTIONS}
                onChange={(option) => updateQuery({ status: option.value })}
              />
            </div>
            <div className="w-60">
              <Input
                aria-label="搜索历史任务"
                placeholder="搜索任务名、批次或任务 ID"
                value={queryText}
                onChange={(event) => setQueryText(event.target.value)}
              />
            </div>
          </div>
        </div>

        {historyQuery.isError ? (
          <StatePanel
            variant="error"
            title="任务历史读取失败"
            description={errorMessage(historyQuery.error)}
            action={
              <Button color="secondary" variant="outline" size="sm" onClick={() => void historyQuery.refetch()}>
                重试
              </Button>
            }
          />
        ) : (
          <>
            <AppDataTable
              caption="任务历史列表"
              columns={columns}
              rows={records}
              rowKey={(record) => `${record.kind}:${record.id}`}
              loading={historyQuery.isFetching && !historyQuery.data}
              onRowActivate={(record) => openDetail(record.kind, record.id)}
              emptyState={
                <StatePanel
                  variant="empty"
                  title={hasFilters ? '没有符合条件的任务' : '还没有任务记录'}
                  description={
                    hasFilters
                      ? '调整类型、状态或搜索关键词后再试。'
                      : '开始一次采集或素材分析后，这里会按时间倒序列出。'
                  }
                  action={
                    hasFilters ? (
                      <Button
                        color="secondary"
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setQueryText('')
                          updateQuery({ kind: '', status: '', q: '' })
                        }}
                      >
                        清除筛选
                      </Button>
                    ) : null
                  }
                />
              }
            />
            <AppPagination
              page={data?.page ?? page}
              pageCount={data?.page_count ?? 1}
              total={total}
              pageSize={pageSize}
              pageSizeOptions={PAGE_SIZES}
              onPageChange={(next) => updateQuery({ page: String(next) }, { resetPage: false })}
              onPageSizeChange={(size) => updateQuery({ size: String(size) })}
            />
          </>
        )}
      </section>

      <TaskDetailDrawer
        selection={selection}
        summary={selectedSummary}
        onClose={closeDetail}
        onRenamed={handleRenamed}
      />
    </section>
  )
}

function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-xl border border-subtle bg-surface px-4 py-3">
      <p className="text-xs text-secondary">{label}</p>
      <p className="mt-1 heading-lg tabular-nums">{value}</p>
    </div>
  )
}

function BatchCell({ record }: { record: HistoryRecord }) {
  const batches = record.batches ?? []
  if (batches.length > 1) {
    const description = batches.map((batch) => [batch.name, ...(batch.versions ?? [])].filter(Boolean).join(' · ')).join('，')
    return (
      <span title={description} className="inline-flex flex-col">
        <span>涉及 {batches.length} 个批次</span>
        <span className="text-2xs text-secondary">{description}</span>
      </span>
    )
  }
  const single = batches[0]
  if (single) {
    return (
      <span className="inline-flex flex-col">
        <span className="truncate" title={single.name}>
          {single.name}
        </span>
        {single.versions.length ? (
          <span className="mt-0.5 flex flex-wrap gap-1">
            {single.versions.map((version) => (
              <span key={version} className="rounded-md bg-surface-tertiary px-1.5 py-0.5 text-2xs text-secondary">
                {version}
              </span>
            ))}
          </span>
        ) : null}
      </span>
    )
  }
  if (record.batch_name) {
    return (
      <span className="truncate" title={record.batch_name}>
        {record.batch_name}
      </span>
    )
  }
  return <span className="text-tertiary">—</span>
}

function ResultCell({ record }: { record: HistoryRecord }) {
  const counts = record.counts ?? {}
  if (record.kind === 'capture') {
    return (
      <div className="flex flex-col gap-0.5 text-xs">
        <span>
          已处理 {counts.recorded ?? 0} / {counts.total ?? 0}
        </span>
        <span className="text-secondary">
          已截图待复核 {counts.done ?? 0} · 待检查 {counts.attention ?? 0} · 不合格 {counts.rejected ?? 0}
        </span>
        {counts.unknown ? (
          <span className="text-warning">旧记录未保存当轮结果（{counts.unknown} 家）</span>
        ) : null}
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-0.5 text-xs">
      <span>
        已分析 {counts.done ?? 0} / {counts.total ?? 0}
      </span>
      {counts.failed ? <span className="text-danger">分析失败 {counts.failed}</span> : null}
    </div>
  )
}
