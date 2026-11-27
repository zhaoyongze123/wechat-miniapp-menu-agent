import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Badge } from '@openai/apps-sdk-ui/components/Badge'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDialog } from '../../components/AppDialog'
import { useUnsavedChanges } from '../../components/UnsavedChangesProvider'
import { AppDrawer } from '../../components/AppDrawer'
import { AppDataTable } from '../../components/AppDataTable'
import { CellText } from '../../components/CellText'
import { AppPagination } from '../../components/AppPagination'
import { ImagePreview } from '../../components/ImagePreview'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { useRole } from '../../app/RoleProvider'
import { errorMessage } from '../../api/client'
import { projectApiPath, projectQueryKey } from '../projects/scope'
import { formatStore } from '../hub/format'
import { ReviewDispositionDialog } from '../quality/ReviewDispositionDialog'
import { fetchTaskDetail, renameCaptureRun } from './api'
import { KIND_LABEL, formatTime, shortTime, statusOf } from './format'
import type { CaptureTaskEntry, HistoryRecord, TaskDetail } from './types'

const DETAIL_PAGE_SIZE = 30

const RESULT_FILTERS = [
  { value: 'all', label: '全部结果' },
  { value: 'captured', label: '已截图待复核' },
  { value: 'review', label: '待检查' },
  { value: 'rejected', label: '不合格' },
  { value: 'pending', label: '待处理或未记录' },
]

/** 这些状态仍可能推进，其他状态停止详情轮询。 */
export function taskDetailIsProgressing(status: string | undefined): boolean {
  return ['queued', 'running', 'pause_requested', 'resuming', 'terminating'].includes(status ?? '')
}

function taskResultLabel(task: CaptureTaskEntry): string {
  if (task.label) return task.label
  if (task.status === 'captured') return '已截图待复核'
  if (task.status === 'review') return '待检查'
  if (task.status === 'rejected') return '不合格'
  return ''
}

export function filterCaptureTasks(tasks: CaptureTaskEntry[], search: string, resultFilter: string): CaptureTaskEntry[] {
  const needle = search.trim().toLocaleLowerCase()
  return tasks.filter((task) => {
    const label = taskResultLabel(task)
    const matchesResult = resultFilter === 'all' ||
      (resultFilter === 'pending'
        ? !['已截图待复核', '待检查', '不合格'].includes(label)
        : task.status === resultFilter || label === RESULT_FILTERS.find((item) => item.value === resultFilter)?.label)
    const searchableText = `${task.store_name ?? ''} ${task.task_id} ${task.source_name ?? ''}`.toLocaleLowerCase()
    return matchesResult && (!needle || searchableText.includes(needle))
  })
}

interface Selection {
  kind: string
  id: string
}

interface TaskDetailDrawerProps {
  projectId: string
  selection: Selection | null
  summary: HistoryRecord | null
  onClose: () => void
  onRenamed: (record: HistoryRecord) => void
}

export function TaskDetailDrawer({ projectId, selection, summary, onClose, onRenamed }: TaskDetailDrawerProps) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const role = useRole()
  const [dispositionTaskId, setDispositionTaskId] = useState<string | null>(null)
  const [renaming, setRenaming] = useState(false)
  const [name, setName] = useState('')
  const [renameError, setRenameError] = useState('')
  const [search, setSearch] = useState('')
  const [resultFilter, setResultFilter] = useState('all')
  const [page, setPage] = useState(1)
  const [metadataExpanded, setMetadataExpanded] = useState(false)
  const [storeDetail, setStoreDetail] = useState<CaptureTaskEntry | null>(null)
  const [preview, setPreview] = useState<{ src: string; alt: string } | null>(null)

  const detailQuery = useQuery({
    queryKey: projectQueryKey(projectId, 'task-detail', selection?.kind ?? '', selection?.id ?? ''),
    queryFn: ({ signal }) => fetchTaskDetail(selection?.kind ?? '', selection?.id ?? '', signal, projectId),
    enabled: selection !== null && Boolean(projectId),
    staleTime: 5_000,
    refetchInterval: (query) => taskDetailIsProgressing(query.state.data?.status) ? 5_000 : false,
    refetchIntervalInBackground: false,
  })

  const detail = detailQuery.data
  useUnsavedChanges(renaming && name.trim() !== (detail?.name || detail?.title || ''), '任务名称尚未保存，切换项目后当前修改将丢失。')

  useEffect(() => {
    setRenaming(false)
    setName('')
    setRenameError('')
    setSearch('')
    setResultFilter('all')
    setPage(1)
    setMetadataExpanded(false)
    setStoreDetail(null)
  }, [selection?.kind, selection?.id])

  const renameMutation = useMutation({
    mutationFn: (value: string) => renameCaptureRun(selection?.id ?? '', value, projectId),
    onSuccess: (updated) => {
      if (selection) {
        queryClient.setQueryData<TaskDetail>(projectQueryKey(projectId, 'task-detail', selection.kind, selection.id), (current) => {
          if (!current) return current
          const next: TaskDetail = { ...current }
          if (updated.title !== undefined) next.title = updated.title
          if (updated.name !== undefined) next.name = updated.name
          return next
        })
      }
      onRenamed(updated)
      setRenaming(false)
      setRenameError('')
      toast.push('任务名称已更新', 'success')
    },
    onError: (error: unknown) => {
      // 保存失败保留原输入，方便直接修改后重试。
      setRenameError(errorMessage(error))
    },
  })

  const title = detail?.title || summary?.title || detail?.id || selection?.id || '任务详情'
  const status = statusOf(detail?.status ?? summary?.status)
  const canRename = (detail?.kind ?? selection?.kind) === 'capture' && detail?.can_rename !== false

  const visibleTasks = useMemo(() => {
    if (detail?.kind !== 'capture') return []
    return filterCaptureTasks(detail.tasks, search, resultFilter)
  }, [detail, resultFilter, search])

  const pageCount = Math.max(1, Math.ceil(visibleTasks.length / DETAIL_PAGE_SIZE))
  const currentPage = Math.min(page, pageCount)
  const pageTasks = visibleTasks.slice((currentPage - 1) * DETAIL_PAGE_SIZE, currentPage * DETAIL_PAGE_SIZE)
  useEffect(() => {
    if (page > pageCount) setPage(pageCount)
  }, [page, pageCount])

  const captureSummary = detail?.kind === 'capture'
    ? detail
    : summary?.kind === 'capture'
      ? summary
      : null
  const headerCounts = captureSummary?.counts ?? null
  const headerStatus = statusOf(detail?.status ?? summary?.status)
  const workerName = detail?.worker_name ?? summary?.worker_name
  const lastReported = detail?.worker_last_seen ?? summary?.worker_last_seen ?? detail?.synced_at ?? summary?.synced_at
  const description = [
    KIND_LABEL[detail?.kind ?? selection?.kind ?? ''] ?? '',
    detail?.id ?? selection?.id ?? '',
    (detail?.created_at ?? summary?.created_at) ? `开始于 ${formatTime(detail?.created_at ?? summary?.created_at)}` : '',
    workerName
      ? `${workerName} · 最近上报 ${formatTime(lastReported)}`
      : '',
  ].filter(Boolean).join(' · ')

  function startRename() {
    setName(detail?.name || detail?.title || '')
    setRenameError('')
    setRenaming(true)
  }

  function submitRename() {
    const value = name.trim()
    if (!value) {
      setRenameError('任务名称不能为空')
      return
    }
    if (value.length > 64) {
      setRenameError('任务名称不能超过 64 个字符')
      return
    }
    renameMutation.mutate(value)
  }

  return (
    <>
    <AppDrawer
      open={selection !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title={title}
      description={description}
      headerActions={
        <div className="flex max-w-[min(450px,52vw)] flex-wrap items-center justify-end gap-1.5">
          {detail || summary ? <StatusBadge tone={headerStatus.tone}>{headerStatus.label}</StatusBadge> : null}
          {headerCounts ? <>
            <Badge color="secondary" variant="soft" size="sm">已处理 {headerCounts.processed ?? headerCounts.recorded ?? 0} / {headerCounts.total ?? 0}</Badge>
            <Badge color="secondary" variant="soft" size="sm">截图 {headerCounts.done ?? 0}</Badge>
            <Badge color="secondary" variant="soft" size="sm">待检查 {headerCounts.attention ?? 0}</Badge>
            <Badge color="secondary" variant="soft" size="sm">不合格 {headerCounts.rejected ?? 0}</Badge>
          </> : null}
          {canRename ? (
            <Button color="secondary" variant="outline" size="sm" onClick={startRename} disabled={!detail || renaming}>
              重命名
            </Button>
          ) : null}
        </div>
      }
      footer={detail?.kind === 'capture' ? (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <AppPagination page={currentPage} pageCount={pageCount} total={visibleTasks.length} pageSize={DETAIL_PAGE_SIZE} pageSizeOptions={[DETAIL_PAGE_SIZE]} onPageChange={setPage} />
          <Button color="secondary" variant="outline" size="sm" onClick={onClose}>关闭</Button>
        </div>
      ) : <div className="flex justify-end"><Button color="secondary" variant="outline" size="sm" onClick={onClose}>关闭</Button></div>}
    >
      {detailQuery.isPending ? (
        <StatePanel variant="loading" title="正在读取任务详情…" />
      ) : detailQuery.isError && !detailQuery.data ? (
        <StatePanel
          variant="error"
          title="详情读取失败"
          description={errorMessage(detailQuery.error)}
          action={
            <Button color="secondary" variant="outline" size="sm" onClick={() => void detailQuery.refetch()}>
              重试
            </Button>
          }
        />
      ) : detail ? (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            {detail.kind !== 'capture' ? <StatusBadge tone={status.tone}>{status.label}</StatusBadge> : null}
            {detail.updated_at && detail.updated_at !== detail.created_at ? (
              <span className="text-secondary">更新于 {formatTime(detail.updated_at)}</span>
            ) : null}
          </div>

          {detail.kind === 'capture' ? (
            <details open={metadataExpanded} onToggle={(event) => setMetadataExpanded(event.currentTarget.open)} className="rounded-lg border border-subtle px-3 py-2 text-sm">
              <summary className="cursor-pointer text-secondary">任务范围与同步信息</summary>
              <dl className="mt-3 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs">
                <dt className="text-tertiary">任务 ID</dt>
                <dd><CellText copyable copyLabel="任务 ID" mono>{detail.id}</CellText></dd>
                {detail.local_run_id ? (
                  <><dt className="text-tertiary">采集端轮次 ID</dt><dd><CellText copyable copyLabel="采集端轮次 ID" mono>{detail.local_run_id}</CellText></dd></>
                ) : null}
                {detail.batches?.length ? (
                  <><dt className="text-tertiary">批次</dt><dd>{detail.batches.map((batch) => `${batch.name}${batch.versions.length ? ` · ${batch.versions.join('/')}` : ''}`).join('、')}</dd></>
                ) : null}
                {detail.history_status ? (
                  <><dt className="text-tertiary">同步状态</dt><dd>{detail.history_status === 'synced' ? '轮次记录已同步' : '部分轮次记录已上报'}</dd></>
                ) : null}
              </dl>
            </details>
          ) : null}

          {renaming ? (
            <div className="rounded-lg bg-surface-secondary p-3">
              <label className="mb-1 block text-xs text-secondary" htmlFor="task-rename-input">
                任务名称（1–64 个字符）
              </label>
              <div className="flex flex-wrap items-center gap-2">
                <div className="min-w-[220px] flex-1">
                  <Input
                    id="task-rename-input"
                    value={name}
                    maxLength={64}
                    invalid={Boolean(renameError)}
                    placeholder="任务名称"
                    onChange={(event) => {
                      setName(event.target.value)
                      setRenameError('')
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') submitRename()
                      if (event.key === 'Escape') setRenaming(false)
                    }}
                  />
                </div>
                <Button color="primary" size="sm" onClick={submitRename} disabled={renameMutation.isPending}>
                  {renameMutation.isPending ? '保存中…' : '保存'}
                </Button>
                <Button color="secondary" variant="ghost" size="sm" onClick={() => setRenaming(false)}>
                  取消
                </Button>
              </div>
              {renameError ? <Alert color="danger" variant="soft" title={renameError} className="mt-2" /> : null}
            </div>
          ) : null}

          {detail.kind === 'capture' ? (
            <>
              {detail.message ? <p className="text-sm text-secondary">{detail.message}</p> : null}
              {detailQuery.isRefetchError ? (
                <Alert color="warning" variant="soft" title={`刷新失败，仍显示最近成功数据（${formatTime(new Date(detailQuery.dataUpdatedAt).toISOString())}）`} description={errorMessage(detailQuery.error)} />
              ) : detailQuery.isFetching ? (
                <p className="text-xs text-secondary" role="status">正在更新任务进度…</p>
              ) : (
                <p className="text-xs text-tertiary">最近成功更新 {formatTime(new Date(detailQuery.dataUpdatedAt).toISOString())}</p>
              )}
              {detail.worker_name && detail.history_status === 'partial' ? (
                <Alert color="warning" variant="soft" title="共享端逐店记录可能尚未全部上报" description="逐店结果仅显示当前已收到的记录。" />
              ) : null}
              <div className="flex flex-wrap items-center gap-2">
                <div className="min-w-[240px] flex-1">
                  <Input aria-label="搜索门店名称或编号" placeholder="搜索门店名称或编号" value={search} onChange={(event) => { setSearch(event.target.value); setPage(1) }} />
                </div>
                <div className="min-w-48">
                  <Select aria-label="门店结果筛选" value={resultFilter} options={RESULT_FILTERS} onChange={(option) => { setResultFilter(option.value); setPage(1) }} />
                </div>
                <span className="text-xs text-secondary">匹配 {visibleTasks.length} / {detail.tasks.length} 家</span>
              </div>
              <AppDataTable
                caption="本轮逐店采集结果"
                rows={pageTasks}
                rowKey={(row) => row.task_id}
                minWidthClass="min-w-[900px]"
                emptyState={
                  <StatePanel
                    variant="empty"
                    title={search || resultFilter !== 'all' ? '没有符合筛选条件的门店' : '没有门店明细'}
                    description={search || resultFilter !== 'all' ? '调整搜索词或结果筛选后再试。' : '旧记录可能未保存当轮逐店结果。'}
                  />
                }
                columns={[
                  {
                    id: 'store',
                    header: '门店',
                    width: '14rem',
                    cell: (row) => (
                      <div className="min-w-0">
                        <button
                          type="button"
                          className="block max-w-full truncate text-left font-medium hover:underline"
                          title={row.store_name || row.source_name || row.task_id}
                          onClick={() => setStoreDetail(row)}
                        >
                          {formatStore(row.store_name || row.source_name || '').name || row.task_id}
                        </button>
                        <p className="mt-1 truncate font-mono text-xs text-tertiary" title={row.task_id}>{row.task_id}</p>
                        {row.source_name && row.store_name && row.source_name !== row.store_name ? (
                          <details className="mt-1 text-xs text-secondary">
                            <summary className="cursor-pointer">查看原始来源</summary>
                            <p className="mt-1"><CellText copyable copyLabel="原始来源">{row.source_name}</CellText></p>
                          </details>
                        ) : null}
                      </div>
                    ),
                  },
                  {
                    id: 'batch',
                    header: '批次 / 版本',
                    width: '9rem',
                    cell: (row) => (
                      <span className="block truncate text-secondary" title={[row.batch_name, row.version].filter(Boolean).join(' · ')}>
                        {[row.batch_name, row.version].filter(Boolean).join(' · ') || '—'}
                      </span>
                    ),
                  },
                  {
                    id: 'result',
                    header: '结果',
                    width: '9rem',
                    cell: (row) => (
                      <span className="block truncate" title={row.label || row.status || '未处理'}>
                        {row.label || row.status || '未处理'}
                      </span>
                    ),
                  },
                  {
                    id: 'message',
                    header: '说明',
                    width: '14rem',
                    cell: (row) => (
                      <span className="line-clamp-2 text-secondary" title={row.reject_reason || row.message || ''}>{row.reject_reason || row.message || '—'}</span>
                    ),
                  },
                  {
                    id: 'actions',
                    header: '操作',
                    width: '7.5rem',
                    cell: (row) => (
                      <Button color="secondary" variant="outline" size="sm" onClick={() => setStoreDetail(row)}>
                        查看门店
                      </Button>
                    ),
                  },
                ]}
              />
            </>
          ) : (
            <>
              <CountChips detail={detail} />
              {detail.message ? <p className="text-sm text-secondary">{detail.message}</p> : null}
              {detail.events.length ? (
                <ol className="space-y-2">
                  {detail.events.map((event, index) => (
                    <li
                      key={`${event.time ?? ''}-${index}`}
                      className="flex gap-3 rounded-lg bg-surface-secondary px-3 py-2 text-sm"
                    >
                      <time className="font-mono shrink-0 text-xs text-tertiary">
                        {shortTime(event.time) || '—'}
                      </time>
                      <span className={event.level === 'error' ? 'text-danger' : ''}>
                        {event.message || '—'}
                      </span>
                    </li>
                  ))}
                </ol>
              ) : (
                <StatePanel variant="empty" title="没有更多明细" />
              )}
            </>
          )}
        </div>
      ) : null}
    </AppDrawer>

    <AppDialog
      open={storeDetail !== null}
      onOpenChange={(open) => { if (!open) setStoreDetail(null) }}
      title={storeDetail
        ? formatStore(storeDetail.store_name || storeDetail.source_name || '').name || storeDetail.task_id
        : '门店详情'}
      description={storeDetail
        ? [storeDetail.batch_name, storeDetail.version, storeDetail.task_id].filter(Boolean).join(' · ')
        : ''}
      widthClass="w-[min(720px,94vw)]"
      footer={
        <div className="flex flex-wrap justify-end gap-2">
          {storeDetail?.status === 'review' && role.role !== 'worker' ? (
            <Button
              color="primary"
              size="sm"
              onClick={() => {
                setDispositionTaskId(storeDetail.task_id)
                setStoreDetail(null)
              }}
            >
              就地处置
            </Button>
          ) : null}
          <Button color="secondary" variant="outline" size="sm" onClick={() => setStoreDetail(null)}>
            关闭
          </Button>
        </div>
      }
    >
      {storeDetail ? (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={storeDetail.status === 'rejected' ? 'danger' : storeDetail.status === 'review' ? 'warning' : storeDetail.status === 'captured' ? 'success' : 'secondary'}>
              {storeDetail.label || storeDetail.status || '未记录'}
            </StatusBadge>
            <span className="text-xs text-secondary">{storeDetail.batch_name || '—'}{storeDetail.version ? ` · ${storeDetail.version}` : ''}</span>
          </div>
          {storeDetail.reject_reason || storeDetail.message ? (
            <p className="whitespace-pre-wrap break-words rounded-lg bg-surface-secondary p-3 text-sm text-default">
              {storeDetail.reject_reason || storeDetail.message}
            </p>
          ) : (
            <p className="text-sm text-tertiary">这次轮次没有保存该门店的说明。</p>
          )}
          {storeDetail.status === 'review' ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {[
                { src: projectApiPath(`/api/tasks/${encodeURIComponent(storeDetail.task_id)}/source`, projectId), alt: '原始二维码' },
                { src: projectApiPath(`/api/tasks/${encodeURIComponent(storeDetail.task_id)}/last-frame`, projectId), alt: '最近采集画面' },
              ].map((item) => (
                <button
                  key={item.alt}
                  type="button"
                  className="overflow-hidden rounded-lg border border-subtle bg-surface-secondary text-left"
                  onClick={() => setPreview(item)}
                >
                  <img src={item.src} alt={item.alt} className="h-40 w-full object-contain" />
                  <span className="block px-3 py-2 text-2xs text-secondary">{item.alt}</span>
                </button>
              ))}
            </div>
          ) : (
            <p className="text-xs text-tertiary">这里是历史轮次留存的逐店结果；当前有效任务的处置请在质量审核或采集工作台完成。</p>
          )}
        </div>
      ) : null}
    </AppDialog>

    <ImagePreview
      open={preview !== null}
      src={preview?.src ?? ''}
      alt={preview?.alt ?? ''}
      caption={preview?.alt ?? ''}
      onOpenChange={(open) => { if (!open) setPreview(null) }}
    />

    {/* 就地处置：不离开任务详情，关闭后回到原轮次与门店列表位置。 */}
    {dispositionTaskId !== null ? (
      <ReviewDispositionDialog
        open
        taskId={dispositionTaskId}
        onOpenChange={(open) => {
          if (!open) setDispositionTaskId(null)
        }}
      />
    ) : null}
    </>
  )
}

function CountChips({ detail }: { detail: TaskDetail }) {
  const counts = detail.counts ?? {}
  const chips =
    detail.kind === 'capture'
      ? [
          `已处理 ${counts.processed ?? counts.recorded ?? 0} / ${counts.total ?? 0}`,
          `已截图待复核 ${counts.done ?? 0}`,
          `待检查 ${counts.attention ?? 0}`,
          `不合格 ${counts.rejected ?? 0}`,
        ]
      : [`已分析 ${counts.done ?? 0} / ${counts.total ?? 0}`, `失败 ${counts.failed ?? 0}`]
  return (
    <div className="flex flex-wrap gap-2">
      {chips.map((chip) => (
        <Badge key={chip} color="secondary" variant="soft" size="sm">
          {chip}
        </Badge>
      ))}
      {detail.kind === 'capture' && counts.unknown ? (
        <Badge color="warning" variant="soft" size="sm">
          {counts.unknown} 家旧记录缺少本轮结果
        </Badge>
      ) : null}
    </div>
  )
}
