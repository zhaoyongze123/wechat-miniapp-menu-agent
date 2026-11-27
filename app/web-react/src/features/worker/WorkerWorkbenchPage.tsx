import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { AppPagination } from '../../components/AppPagination'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { FilterToolbar } from '../../components/FilterToolbar'
import { PageHeader } from '../../components/PageHeader'
import { SearchInput } from '../../components/SearchInput'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { useRole } from '../../app/RoleProvider'
import { useProject } from '../projects/ProjectProvider'
import { projectQueryKey } from '../projects/scope'
import { addSelection, removeSelection } from '../hub/selection'
import {
  fetchManualState,
  manualCapture,
  manualCommandContext,
  manualRecognizeStoreName,
  manualReject,
  manualSaveStoreName,
  manualSkip,
  pauseCapture,
  resumeCapture,
  retryTask,
  terminateCapture,
} from './api'
import {
  canResume,
  canRetry,
  canSwitchToManual,
  captureModeLabel,
  filterTasks,
  formatStore,
  formatTime,
  pageTasks,
  progressOf,
  runStatusOf,
  sortTasks,
  stateMetaOf,
} from './format'
import { useRefreshWorkerStatus, useWorkerStatus } from './hooks'
import { ManualPanel } from './ManualPanel'
import { StatCard } from '../../components/StatCard'
import { StartRunDialog } from './StartRunDialog'
import { WorkerSyncDialog } from './WorkerSyncDialog'
import { WorkerTaskDetailDrawer } from './WorkerTaskDetailDrawer'
import type { CaptureMode, WorkerFilter, WorkerSort, WorkerTask } from './types'
import { START_RUN_LIMIT } from './types'

const PAGE_SIZES = [20, 50, 100]
const DEFAULT_PAGE_SIZE = 20
const FILTER_OPTIONS = [
  { value: 'all', label: '全部状态' },
  { value: 'pending', label: '待采集' },
  { value: 'running', label: '采集中' },
  { value: 'captured', label: '已截图待复核' },
  { value: 'review', label: '待检查' },
  { value: 'returned', label: '待重新选码' },
  { value: 'rejected', label: '不合格' },
  { value: 'returned', label: '待重新选码' },
]
const SORT_OPTIONS = [
  { value: 'source', label: '按门店名称' },
  { value: 'recent', label: '按最近更新' },
  { value: 'status', label: '按状态' },
]
const FILTERS: WorkerFilter[] = ['all', 'pending', 'running', 'captured', 'review', 'rejected', 'returned']
const SORTS: WorkerSort[] = ['source', 'recent', 'status']


/** Windows 原生采集工作台：本机队列、全自动/半自动、运行控制与断线补传提示。 */
export function WorkerWorkbenchPage() {
  const toast = useToast()
  const { projectId } = useProject()
  const previousProjectId = useRef(projectId)
  const refresh = useRefreshWorkerStatus()
  const { status: sharedStatus } = useRole()
  const statusQuery = useWorkerStatus(true)
  const status = statusQuery.data
  const tasks = useMemo(() => status?.tasks ?? [], [status])

  const [searchParams, setSearchParams] = useSearchParams()
  const filter = (FILTERS.includes(searchParams.get('filter') as WorkerFilter) ? searchParams.get('filter') : 'all') as WorkerFilter
  const sort = (SORTS.includes(searchParams.get('sort') as WorkerSort) ? searchParams.get('sort') : 'source') as WorkerSort
  const batch = searchParams.get('batch') ?? ''
  const q = searchParams.get('q') ?? ''
  const size = PAGE_SIZES.includes(Number(searchParams.get('size'))) ? Number(searchParams.get('size')) : DEFAULT_PAGE_SIZE
  const page = Math.max(1, Number(searchParams.get('page')) || 1)

  const [queryText, setQueryText] = useState(q)
  useEffect(() => setQueryText(q), [q])
  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (queryText === q) return
      updateQuery({ q: queryText, page: 1 })
    }, 250)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryText, q])

  const updateQuery = useCallback(
    (patch: Record<string, string | number>) => {
      const next = new URLSearchParams(searchParams)
      for (const [key, value] of Object.entries(patch)) {
        if (value === '' || value === 'all' && key === 'filter' || value === 'source' && key === 'sort') next.delete(key)
        else next.set(key, String(value))
      }
      setSearchParams(next, { replace: true })
    },
    [searchParams, setSearchParams],
  )

  const batches = useMemo(() => {
    const map = new Map<string, string>()
    for (const task of tasks) {
      if (task.batch_id) map.set(task.batch_id, task.batch_name || '未命名批次')
    }
    return [...map].map(([value, label]) => ({ value, label }))
  }, [tasks])

  const filtered = useMemo(() => filterTasks(tasks, { filter, q, batch }), [tasks, filter, q, batch])
  const sorted = useMemo(() => sortTasks(filtered, sort), [filtered, sort])
  const paged = useMemo(() => pageTasks(sorted, page, size), [sorted, page, size])

  const [selected, setSelected] = useState<string[]>([])
  const selectedSet = useMemo(() => new Set(selected), [selected])
  const selectedTasks = useMemo(() => tasks.filter((task) => selectedSet.has(task.id)), [tasks, selectedSet])
  const blockedSelected = selectedTasks.filter((task) => !isRunnable(task))

  const run = status?.run ?? null
  const running = Boolean(status?.running)
  const resumeAvailable = canResume(run)
  const counts = status?.counts ?? {}
  const totalStores = tasks.length

  const [startOpen, setStartOpen] = useState(false)
  const [startMode, setStartMode] = useState<CaptureMode>('auto')
  const [terminateOpen, setTerminateOpen] = useState(false)
  const [syncOpen, setSyncOpen] = useState(false)
  const [detailTask, setDetailTask] = useState<WorkerTask | null>(null)
  const [debugView, setDebugView] = useState(false)
  const [commandError, setCommandError] = useState('')
  const [manualError, setManualError] = useState('')

  useEffect(() => {
    if (previousProjectId.current === projectId) return
    previousProjectId.current = projectId
    setSelected([])
    setDetailTask(null)
    setStartOpen(false)
    setTerminateOpen(false)
  }, [projectId])

  const terminate = useMutation({
    mutationFn: () => terminateCapture(projectId),
    onSuccess: () => {
      toast.push('已终止当前批次，未处理门店仍在队列中', 'info')
      setTerminateOpen(false)
      setCommandError('')
      refresh()
    },
    onError: (error) => setCommandError(errorMessage(error)),
  })

  const pause = useMutation({
    mutationFn: () => pauseCapture(projectId),
    onSuccess: () => {
      toast.push('已请求暂停，当前门店完成后生效', 'info')
      refresh()
    },
    onError: (error) => setCommandError(errorMessage(error)),
  })

  const resume = useMutation({
    mutationFn: (mode?: CaptureMode) => resumeCapture(mode, projectId),
    onSuccess: (_data, mode) => {
      toast.push(mode === 'manual' ? '已切换半自动并继续采集' : '已继续采集', 'success')
      setCommandError('')
      refresh()
    },
    onError: (error) => setCommandError(errorMessage(error)),
  })

  const retry = useMutation({
    mutationFn: (taskId: string) => retryTask(taskId, projectId),
    onSuccess: () => {
      toast.push('已重新排队该门店', 'success')
      refresh()
    },
    onError: (error) => setCommandError(errorMessage(error)),
  })

  const manualQuery = useQuery({
    queryKey: projectQueryKey(projectId, 'manual-control'),
    queryFn: ({ signal }) => fetchManualState(signal, projectId),
    enabled: Boolean(projectId && running && run?.capture_mode === 'manual'),
    refetchInterval: 1_500,
    staleTime: 1_250,
  })
  const manual = running && run?.capture_mode === 'manual' ? manualQuery.data?.manual : null
  const manualContext = manualCommandContext(manualQuery.data)

  const manualAction = useMutation({
    mutationFn: async (action: 'capture' | 'skip' | { reason: string; detail: string }) => {
      if (!manualContext || manualQuery.isError || Date.now() - manualQuery.dataUpdatedAt > 5_000) {
        throw new Error('当前状态已过期，请刷新后再操作')
      }
      if (action === 'capture') return manualCapture(manualContext, projectId)
      if (action === 'skip') return manualSkip(manualContext, projectId)
      return manualReject(manualContext, action.reason, action.detail, projectId)
    },
    onSuccess: (result, action) => {
      setManualError('')
      if (action === 'capture') toast.push('已触发截图', 'success')
      else if (action === 'skip') toast.push('已跳过本店', 'info')
      else toast.push('已记录不合格', 'info')
      void result
      refresh()
    },
    onError: (error) => setManualError(errorMessage(error)),
    onSettled: () => { void manualQuery.refetch() },
  })

  const connection = sharedStatus && sharedStatus.role === 'worker' ? sharedStatus : null
  const online = connection?.online ?? false
  const pendingUploads = connection?.pending_uploads ?? 0

  const toggleSelection = (task: WorkerTask) => {
    if (selectedSet.has(task.id)) {
      setSelected(removeSelection(selected, [task.id]))
      return
    }
    if (!isRunnable(task)) {
      toast.push('该门店当前不可开跑，请先处理待检查或不合格状态', 'info')
      return
    }
    const result = addSelection(selected, [task.id], START_RUN_LIMIT)
    if (result.overflow) toast.push(`一次最多选择 ${START_RUN_LIMIT} 家门店`, 'info')
    setSelected(result.ids)
  }

  const selectableRows = paged.rows.filter(isRunnable)
  const allPageSelected = selectableRows.length > 0 && selectableRows.every((task) => selectedSet.has(task.id))
  const somePageSelected = selectableRows.some((task) => selectedSet.has(task.id))
  const togglePage = () => {
    if (allPageSelected) {
      setSelected(removeSelection(selected, selectableRows.map((task) => task.id)))
      return
    }
    const result = addSelection(selected, selectableRows.map((task) => task.id), START_RUN_LIMIT)
    if (result.overflow) toast.push(`一次最多选择 ${START_RUN_LIMIT} 家门店`, 'info')
    setSelected(result.ids)
  }

  const canStart = !running && selectedTasks.length > 0 && blockedSelected.length === 0
  const progress = progressOf(run)

  const columns: DataColumn<WorkerTask>[] = [
    {
      id: 'select',
      header: (
        <Checkbox
          checked={allPageSelected ? true : somePageSelected ? 'indeterminate' : false}
          disabled={!selectableRows.length}
          label={<span className="sr-only">全选本页可开跑门店</span>}
          onCheckedChange={togglePage}
        />
      ),
      className: 'w-10',
      width: '3.25rem',
      sticky: 'left',
      cell: (task) => (
        <span className="inline-flex" onClick={(event) => event.stopPropagation()}>
          <Checkbox
            checked={selectedSet.has(task.id)}
            disabled={!isRunnable(task)}
            label={<span className="sr-only">选择门店 {formatStore(task.source_name).name}</span>}
            onCheckedChange={() => toggleSelection(task)}
          />
        </span>
      ),
    },
    {
      id: 'store',
      header: '门店',
      width: '15rem',
      cell: (task) => {
        const store = formatStore(task.source_name)
        const meta = [
          store.code || '编号未记录',
          task.batch_name ?? '',
          task.version ? `v${task.version}` : '',
        ].filter(Boolean).join(' · ')
        return (
          <div className="min-w-0">
            <p className="truncate font-medium text-default" title={task.source_name}>{store.name || task.source_name}</p>
            <p className="mt-0.5 truncate text-xs text-secondary" title={meta}>{meta}</p>
          </div>
        )
      },
    },
    {
      id: 'state',
      header: '状态',
      className: 'whitespace-nowrap',
      width: '7rem',
      cell: (task) => {
        const meta = stateMetaOf(task)
        return <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
      },
    },
    {
      id: 'message',
      header: '最近结果',
      width: '16rem',
      cell: (task) => (
        <span className="line-clamp-2 max-w-md text-xs text-secondary" title={task.reject_label || task.message || ''}>
          {task.reject_label || task.message || '—'}
        </span>
      ),
    },
    {
      id: 'updated',
      header: '最近更新',
      className: 'whitespace-nowrap text-xs text-secondary',
      width: '9rem',
      cell: (task) => formatTime(task.updated_at),
    },
    {
      id: 'actions',
      header: '操作',
      className: 'whitespace-nowrap',
      width: '11.5rem',
      sticky: 'right',
      cell: (task) => (
        <div className="flex flex-wrap gap-1.5" onClick={(event) => event.stopPropagation()}>
          <Button color="secondary" variant="outline" size="sm" onClick={() => setDetailTask(task)}>
            详情
          </Button>
          <Button
            color="secondary"
            variant="outline"
            size="sm"
            disabled={running || !canRetry(task) || retry.isPending}
            onClick={() => retry.mutate(task.id)}
          >
            重新排队
          </Button>
        </div>
      ),
    },
  ]

  return (
    <section className="space-y-6" aria-labelledby="worker-workbench-heading">
      <PageHeader
        title="采集工作台"
        headingId="worker-workbench-heading"
        actions={
          <>
            <Button color="secondary" variant="outline" size="sm" aria-pressed={debugView} onClick={() => setDebugView((value) => !value)}>
              {debugView ? '关闭调试视图' : '调试视图'}
            </Button>
            <Button color="secondary" variant="outline" size="sm" onClick={() => setSyncOpen(true)}>
              同步状态{pendingUploads ? ` · 待上传 ${pendingUploads}` : ''}
            </Button>
            <Button color="secondary" variant="outline" size="sm" onClick={() => void statusQuery.refetch()}>
              刷新
            </Button>
            <Button
              color="primary"
              size="sm"
              disabled={!canStart}
              onClick={() => {
                setStartMode(run?.capture_mode === 'manual' ? 'manual' : 'auto')
                setStartOpen(true)
              }}
            >
              开跑采集 {selectedTasks.length ? `${selectedTasks.length} 家` : ''}
            </Button>
          </>
        }
      />

      {connection && !connection.paired ? (
        <Alert
          color="info"
          variant="soft"
          title="尚未配对共享后台"
          description="本机仍可执行已有队列；配对成功后结果会自动补传，不会丢弃。"
        />
      ) : null}
      {connection?.paired && !online ? (
        <Alert
          color="warning"
          variant="soft"
          title="共享后台连接已断开"
          description={`本机队列可继续执行${pendingUploads ? `，${pendingUploads} 个结果待补传` : ''}；网络恢复后自动续传。`}
        />
      ) : null}
      {connection?.credential_invalid ? (
        <Alert color="danger" variant="soft" title="共享凭据已失效" description="请在设置或诊断中重新配对，本机采集结果会保留。" />
      ) : null}

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-7" aria-label="本机状态统计">
        <StatCard label="本机门店" value={totalStores} active={filter === 'all'} onClick={() => updateQuery({ filter: 'all', page: 1 })} />
        <StatCard label="待采集" value={Number(counts.pending) || 0} active={filter === 'pending'} onClick={() => updateQuery({ filter: 'pending', page: 1 })} />
        <StatCard label="采集中" value={Number(counts.running) || 0} active={filter === 'running'} onClick={() => updateQuery({ filter: 'running', page: 1 })} />
        <StatCard label="已截图待复核" value={Number(counts.captured) || 0} active={filter === 'captured'} onClick={() => updateQuery({ filter: 'captured', page: 1 })} />
        <StatCard label="待检查" value={Number(counts.review) || 0} tone="warning" active={filter === 'review'} onClick={() => updateQuery({ filter: 'review', page: 1 })} />
        <StatCard label="不合格" value={Number(counts.rejected) || 0} tone="danger" active={filter === 'rejected'} onClick={() => updateQuery({ filter: 'rejected', page: 1 })} />
        <StatCard label="待重新选码" value={Number(counts.returned) || 0} active={filter === 'returned'} onClick={() => updateQuery({ filter: 'returned', page: 1 })} />
      </section>

      <section className="rounded-xl border border-subtle bg-surface space-y-4 p-4 md:p-5" aria-labelledby="worker-run-heading">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 id="worker-run-heading" className="heading-xs">
              当前批次
            </h2>
            <p className="mt-0.5 text-xs text-secondary">
              {status?.execution_engine ? `执行引擎 ${status.execution_engine}` : '执行引擎待检测'}
              {status?.launch_mode ? ` · ${status.launch_mode}` : ''}
              {run?.capture_mode ? ` · ${captureModeLabel(run.capture_mode)}` : ''}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {run ? <StatusBadge tone={runStatusOf(run.status).tone}>{runStatusOf(run.status).label}</StatusBadge> : null}
            {running ? (
              <Button color="secondary" variant="outline" size="sm" disabled={pause.isPending} onClick={() => pause.mutate()}>
                {status?.stop_requested ? '暂停中…' : '暂停'}
              </Button>
            ) : null}
            {!running && resumeAvailable ? (
              <Button color="primary" size="sm" disabled={resume.isPending} onClick={() => resume.mutate(undefined)}>
                继续采集
              </Button>
            ) : null}
            {!running && canSwitchToManual(run) ? (
              <Button color="secondary" variant="outline" size="sm" disabled={resume.isPending} onClick={() => resume.mutate('manual')}>
                切换半自动并继续
              </Button>
            ) : null}
            {run && ['running', 'pause_requested', 'paused', 'blocked', 'interrupted'].includes(run.status) ? (
              <Button color="danger" variant="outline" size="sm" onClick={() => setTerminateOpen(true)}>
                终止
              </Button>
            ) : null}
          </div>
        </div>

        {run ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <p className="truncate text-sm font-medium text-default">{run.name || '未命名批次'}</p>
              <p className="text-xs text-secondary tabular-nums">
                已处理 {progress.processed} / {progress.total} · 剩余 {progress.remaining}
                {run.captured !== undefined ? ` · 已截图 ${run.captured}` : ''}
                {run.review !== undefined ? ` · 待检查 ${run.review}` : ''}
                {run.unqualified !== undefined ? ` · 不合格 ${run.unqualified}` : ''}
              </p>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-secondary-soft" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress.percent}>
              <div className="h-full rounded-full bg-primary-solid transition-all" style={{ width: `${progress.percent}%` }} />
            </div>
            <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-secondary">
              {run.current_task_id ? (
                <span>
                  当前门店 {formatStore(tasks.find((task) => task.id === run.current_task_id)?.source_name ?? '').name || run.current_task_id}
                </span>
              ) : null}
              {run.cross_batch ? <span>跨批次 {run.batch_count ?? ''} 个</span> : null}
              {run.next_name ? <span>下一批次 {run.next_name}</span> : null}
              {run.updated_at ? <span>更新于 {formatTime(run.updated_at)}</span> : null}
            </div>
            {run.message ? <p className="rounded-lg bg-surface-secondary px-3 py-2 text-xs text-secondary">{run.message}</p> : null}
          </div>
        ) : (
          <StatePanel
            variant="info"
            title="当前没有运行中的批次"
            description="在下方勾选门店后点击“开跑采集”，可选择全自动或半自动。"
          />
        )}

        {status?.execution_engine && status.engine_ready === false ? (
          <Alert color="danger" variant="soft" title="执行引擎不可用" description={status.engine_message || '请检查运行环境或改用半自动。'} />
        ) : null}
        {status?.robot_readiness && status.robot_readiness.ready === false && (status.robot_readiness.issues?.length ?? 0) > 0 ? (
          <Alert
            color="warning"
            variant="soft"
            title="全自动前置检查未通过"
            description={(status.robot_readiness.issues ?? []).join('；')}
          />
        ) : null}
        {commandError ? <Alert color="danger" variant="soft" title={commandError} /> : null}
      </section>

      {run?.capture_mode === 'manual' && manual ? (
        <ManualPanel
          key={manual.task_id}
          manual={manual}
          busy={manualAction.isPending || manualQuery.isError || !manualContext}
          error={manualQuery.isError ? '无法更新当前门店，操作已暂停' : manualError}
          onCapture={() => manualAction.mutate('capture')}
          onSkip={() => manualAction.mutate('skip')}
          onReject={(reason, detail) => manualAction.mutate({ reason, detail })}
          onRecognizeStoreName={async () => {
            if (!manualContext || manualQuery.isError || Date.now() - manualQuery.dataUpdatedAt > 5_000) {
              throw new Error('当前状态已过期，请刷新后再操作')
            }
            const result = await manualRecognizeStoreName(manualContext, projectId)
            void manualQuery.refetch()
            return result
          }}
          onSaveStoreName={async (storeName) => {
            if (!manualContext || manualQuery.isError || Date.now() - manualQuery.dataUpdatedAt > 5_000) {
              throw new Error('当前状态已过期，请刷新后再操作')
            }
            await manualSaveStoreName(manualContext, storeName, projectId)
            void manualQuery.refetch()
          }}
        />
      ) : null}

      <section className="rounded-xl border border-subtle bg-surface space-y-4 p-4 md:p-5" aria-labelledby="worker-queue-heading">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 id="worker-queue-heading" className="heading-xs">
              本机任务队列
            </h2>
            <p className="mt-0.5 text-xs text-secondary">
              共 {filtered.length} 家匹配 · 已选 {selectedTasks.length} 家
              {blockedSelected.length ? ` · ${blockedSelected.length} 家当前不可开跑` : ''}
            </p>
          </div>
          <FilterToolbar
            label="本机任务筛选"
            activeCount={[batch, filter !== 'all' ? filter : '', sort !== 'source' ? sort : '', q].filter(Boolean).length}
            onClear={() => {
              setQueryText('')
              updateQuery({ batch: '', filter: 'all', sort: 'source', q: '', page: 1 })
            }}
            summary={`共 ${filtered.length} 家匹配`}
          >
            <div className="w-44">
              <Select
                aria-label="按批次筛选"
                value={batch}
                options={[{ value: '', label: '全部批次' }, ...batches]}
                onChange={(option) => updateQuery({ batch: option.value, page: 1 })}
              />
            </div>
            <div className="w-36">
              <Select
                aria-label="按状态筛选"
                value={filter}
                options={FILTER_OPTIONS}
                onChange={(option) => updateQuery({ filter: option.value, page: 1 })}
              />
            </div>
            <div className="w-36">
              <Select
                aria-label="列表排序"
                value={sort}
                options={SORT_OPTIONS}
                onChange={(option) => updateQuery({ sort: option.value, page: 1 })}
              />
            </div>
            <SearchInput
              className="w-60"
              label="门店"
              placeholder="搜索门店名称、批次或原因"
              value={queryText}
              onChange={setQueryText}
            />
            {selected.length ? (
              <Button color="secondary" variant="ghost" size="sm" onClick={() => setSelected([])}>
                清空选择
              </Button>
            ) : null}
          </FilterToolbar>
        </div>

        {statusQuery.isError ? (
          <StatePanel
            variant="error"
            title="无法读取本机任务"
            description={errorMessage(statusQuery.error)}
            action={
              <Button color="secondary" variant="outline" size="sm" onClick={() => void statusQuery.refetch()}>
                重试
              </Button>
            }
          />
        ) : statusQuery.isPending ? (
          <StatePanel variant="loading" title="正在读取本机任务…" />
        ) : (
          <>
            <AppDataTable
              columns={columns}
              rows={paged.rows}
              rowKey={(task) => task.id}
              caption="本机采集任务队列"
              minWidthClass="min-w-[1000px]"
              onRowActivate={(task) => setDetailTask(task)}
              emptyState={
                <StatePanel
                  variant="empty"
                  title={q || batch ? '没有匹配的门店' : '本机队列为空'}
                  description={q || batch ? '调整筛选或搜索后重试。' : '共享后台分配或同步门店后，这里会出现待采集任务。'}
                />
              }
            />
            <AppPagination
              page={paged.page}
              pageCount={paged.pages}
              total={filtered.length}
              pageSize={size}
              pageSizeOptions={PAGE_SIZES}
              onPageChange={(next) => updateQuery({ page: next })}
              onPageSizeChange={(nextSize) => updateQuery({ size: nextSize, page: 1 })}
            />
          </>
        )}

        {selected.length > START_RUN_LIMIT ? (
          <Alert color="warning" variant="soft" title={`一次最多开跑 ${START_RUN_LIMIT} 家`} description="请先减少选择。" />
        ) : null}
      </section>

      <section className="rounded-xl border border-subtle bg-surface space-y-3 p-4 md:p-5" aria-labelledby="worker-events-heading">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="worker-events-heading" className="heading-xs">
            最近事件
          </h2>
          <div className="flex items-center gap-3 text-xs text-secondary">
            {pendingUploads ? <StatusBadge tone="warning">{pendingUploads} 个结果待补传</StatusBadge> : null}
            <Link className="text-default underline-offset-2 hover:underline" to="/task-center">
              查看任务中心
            </Link>
          </div>
        </div>
        {(status?.events ?? []).length ? (
          <ul className="space-y-1.5 text-xs">
            {[...(status?.events ?? [])].slice(-10).reverse().map((event, index) => (
              <li key={`${event.time}-${index}`} className="flex gap-3">
                <span className="w-32 shrink-0 text-secondary tabular-nums">{formatTime(event.time)}</span>
                <span className={event.level === 'error' ? 'text-danger' : event.level === 'warning' ? 'text-warning' : 'text-default'}>
                  {event.message}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-secondary">暂无事件记录。</p>
        )}
        {status?.run_directory || status?.batch_output_dir ? (
          <p className="text-xs text-secondary">
            输出目录：{status?.batch_output_dir || status?.run_directory}
          </p>
        ) : null}
      </section>

      <StartRunDialog
        open={startOpen}
        onOpenChange={setStartOpen}
        tasks={selectedTasks}
        defaultMode={startMode}
        blocked={running}
        onStarted={(mode) => {
          toast.push(mode === 'manual' ? '已开始半自动采集' : '已开始全自动采集', 'success')
          setSelected([])
          refresh()
        }}
      />

      <WorkerSyncDialog open={syncOpen} onOpenChange={setSyncOpen} />

      <WorkerTaskDetailDrawer task={detailTask} debugView={debugView} onOpenChange={(open) => { if (!open) setDetailTask(null) }} />

      <ConfirmActionDialog
        open={terminateOpen}
        onOpenChange={setTerminateOpen}
        title="终止当前批次？"
        description={
          run
            ? `将结束「${run.name || '未命名批次'}」的采集进程；已处理 ${progress.processed} 家结果保留，剩余 ${progress.remaining} 家仍留在本机队列中，可稍后重新开跑。`
            : '将结束当前批次。'
        }
        confirmLabel="终止批次"
        tone="danger"
        busy={terminate.isPending}
        error={commandError}
        onConfirm={() => terminate.mutate()}
      />
    </section>
  )
}

function isRunnable(task: WorkerTask): boolean {
  // 服务端 shared_worker.tasks() 已排除共享端判定的不合格/已采集/待重新选码门店。
  return task.runnable === true
}
