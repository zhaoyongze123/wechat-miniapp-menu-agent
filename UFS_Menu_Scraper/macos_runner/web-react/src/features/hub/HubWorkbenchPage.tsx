import { useCallback, useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { SegmentedControl } from '@openai/apps-sdk-ui/components/SegmentedControl'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { AppDrawer } from '../../components/AppDrawer'
import { AppPagination } from '../../components/AppPagination'
import { BulkActionBar } from '../../components/BulkActionBar'
import { PageHeader } from '../../components/PageHeader'
import { SearchInput } from '../../components/SearchInput'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { StatCard } from '../../components/StatCard'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { useRole } from '../../app/RoleProvider'
import { AssignDialog } from './AssignDialog'
import { HubStoreDetailDrawer } from './HubStoreDetailDrawer'
import { fetchHubStatus } from './api'
import { assignmentStatusOf, deviceStateOf, formatStore, storeStateOf } from './format'
import { activeAssignmentMap, isWorkerActive, useHubSharedStatus } from './hooks'
import { HUB_SELECTION_LIMIT, addSelection, removeSelection } from './selection'
import type {
  HubQueueFilter,
  HubQueueScope,
  HubQueueSort,
  HubSelectedTask,
  HubTask,
} from './types'

const PAGE_SIZES = [20, 50, 100]

const STATUS_OPTIONS = [
  { value: 'all', label: '全部状态' },
  { value: 'pending', label: '待采集' },
  { value: 'running', label: '采集中' },
  { value: 'captured', label: '已截图待复核' },
  { value: 'rejected', label: '不合格' },
  { value: 'review', label: '待检查' },
  { value: 'returned', label: '待重新选码' },
]

const SORT_OPTIONS = [
  { value: 'source', label: '按门店名称' },
  { value: 'recent', label: '按最近更新' },
  { value: 'status', label: '按状态' },
]

const FILTER_VALUES: HubQueueFilter[] = ['all', 'pending', 'running', 'captured', 'rejected', 'review', 'returned']
const SORT_VALUES: HubQueueSort[] = ['source', 'recent', 'status']

function parseScope(value: string | null): HubQueueScope {
  return value === 'current' ? 'current' : 'all'
}

function parseFilter(value: string | null): HubQueueFilter {
  return FILTER_VALUES.includes(value as HubQueueFilter) ? (value as HubQueueFilter) : 'all'
}

function parseSort(value: string | null): HubQueueSort {
  return SORT_VALUES.includes(value as HubQueueSort) ? (value as HubQueueSort) : 'source'
}

function parsePositiveInt(value: string | null, fallback: number): number {
  const parsed = Number.parseInt(value ?? '', 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback
}

export function HubWorkbenchPage() {
  const role = useRole()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  // 从“分配记录”回收后跳转时，可用 ?selected=id1,id2 带入待重新分配的门店。
  const [selectedIds, setSelectedIds] = useState<string[]>(() => {
    const raw = searchParams.get('selected')
    return raw ? raw.split(',').filter(Boolean).slice(0, HUB_SELECTION_LIMIT) : []
  })
  const [assignOpen, setAssignOpen] = useState(false)
  const [selectedOpen, setSelectedOpen] = useState(false)
  const [detailTask, setDetailTask] = useState<HubTask | null>(null)

  // 一次性消费地址栏中的选择参数，后续翻页与筛选不再受其影响。
  useEffect(() => {
    if (!searchParams.has('selected')) return
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous)
        next.delete('selected')
        return next
      },
      { replace: true },
    )
  }, [searchParams, setSearchParams])

  const scope = parseScope(searchParams.get('scope'))
  const batch = scope === 'current' ? '' : (searchParams.get('batch') ?? '')
  const filter = parseFilter(searchParams.get('filter'))
  const sort = parseSort(searchParams.get('sort'))
  const q = searchParams.get('q') ?? ''
  const page = parsePositiveInt(searchParams.get('page'), 1)
  const sizeParam = parsePositiveInt(searchParams.get('size'), 50)
  const size = PAGE_SIZES.includes(sizeParam) ? sizeParam : 50

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

  const [debugView, setDebugView] = useState(false)
  const [queryText, setQueryText] = useState(q)
  useEffect(() => setQueryText(q), [q])
  useEffect(() => {
    if (queryText.trim() === q.trim()) return
    const timer = window.setTimeout(() => updateQuery({ q: queryText.trim() }), 250)
    return () => window.clearTimeout(timer)
  }, [queryText, q, updateQuery])

  const statusQuery = useQuery({
    queryKey: ['hub-status', scope, batch, filter, sort, q, page, size, selectedIds.join(','), debugView],
    queryFn: ({ signal }) =>
      fetchHubStatus({ page, size, scope, batch, q, filter, sort, selected: selectedIds, includeEvents: debugView }, signal),
    placeholderData: keepPreviousData,
    enabled: role.role === 'hub',
    refetchInterval: 10_000,
    staleTime: 4_000,
  })

  const sharedQuery = useHubSharedStatus()
  const assignmentMap = useMemo(() => activeAssignmentMap(sharedQuery.data), [sharedQuery.data])
  const workers = useMemo(() => sharedQuery.data?.workers ?? [], [sharedQuery.data])

  // 已被分配的 store 不能再选：共享状态是唯一事实源，轮询后自动从选择中剔除。
  useEffect(() => {
    if (!sharedQuery.data) return
    setSelectedIds((current) => {
      const next = current.filter((id) => !assignmentMap.has(id))
      return next.length === current.length ? current : next
    })
  }, [assignmentMap, sharedQuery.data])

  // 服务端会按真实总数收敛页码；同步回地址栏，避免刷新后停在空页。
  // 占位数据仍是上一页结果，必须等本次请求完成后再判断，否则会把刚点的页码改回去。
  const serverPage = statusQuery.data?.queue?.page
  useEffect(() => {
    if (statusQuery.isFetching || statusQuery.isPlaceholderData) return
    if (serverPage && serverPage !== page) updateQuery({ page: String(serverPage) }, { resetPage: false })
  }, [serverPage, page, statusQuery.isFetching, statusQuery.isPlaceholderData, updateQuery])

  const queue = statusQuery.data?.queue
  const records = statusQuery.data?.tasks ?? []
  const counts = statusQuery.data?.counts ?? {}
  const events = statusQuery.data?.events ?? []
  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds])
  const selectedMap = useMemo(
    () => new Map((queue?.selected ?? []).map((task) => [task.id, task])),
    [queue],
  )
  const selectedTasks = useMemo<HubSelectedTask[]>(
    () => selectedIds.map((id) => selectedMap.get(id) ?? { id }),
    [selectedIds, selectedMap],
  )

  const canSelect = useCallback(
    (task: HubTask): boolean => !assignmentMap.has(task.id) && task.runnable === true,
    [assignmentMap],
  )

  const toggleSelection = useCallback(
    (task: HubTask) => {
      if (!canSelect(task)) return
      if (selectedSet.has(task.id)) {
        setSelectedIds((current) => removeSelection(current, [task.id]))
        return
      }
      setSelectedIds((current) => {
        const result = addSelection(current, [task.id])
        if (result.overflow) toast.push(`一次最多分配 ${HUB_SELECTION_LIMIT} 家门店`, 'info')
        return result.ids
      })
    },
    [canSelect, selectedSet, toast],
  )

  const handleAssigned = useCallback(
    (count: number, workerName: string) => {
      setAssignOpen(false)
      setSelectedIds([])
      toast.push(`已向 ${workerName} 分配 ${count} 家门店`, 'success')
      void queryClient.invalidateQueries({ queryKey: ['hub-status'] })
      void queryClient.invalidateQueries({ queryKey: ['hub-shared-status'] })
      void queryClient.invalidateQueries({ queryKey: ['hub-assignments'] })
    },
    [queryClient, toast],
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

  if (role.role !== 'hub') {
    return (
      <StatePanel
        variant="info"
        title="共享调度页面仅共享后台可用"
        description="本页负责把已发布门店分配给 Windows 采集电脑，请使用共享后台或 Windows 采集端访问。"
      />
    )
  }

  const release = statusQuery.data?.material_release ?? null
  // counts 还包含 needs_review / attention 两个兼容别名，门店总数只累加六个互斥状态。
  const totalStores = ['pending', 'running', 'captured', 'review', 'rejected', 'returned']
    .reduce((sum, key) => sum + (Number(counts[key]) || 0), 0)
  const activeWorkers = workers.filter(isWorkerActive)
  const onlineWorkers = activeWorkers.filter((worker) => deviceStateOf(worker).key === 'online')
  const assignedCount = assignmentMap.size
  const hasFilters = filter !== 'all' || q.trim() !== '' || batch !== '' || scope === 'current'
  const scopeText =
    scope === 'current'
      ? release
        ? `当前素材：${[release.batch_name, release.version].filter(Boolean).join(' · ')}${release.store_count ? ` · ${release.store_count} 家` : ''}。仅显示当前发布快照，批次下拉不生效。`
        : '当前素材集尚未发布快照，列表为空。请先在素材清洗中发布批次。'
      : batch
        ? `全部发布批次 · 仅看所选批次。每家门店仍只取所属批次的最新发布版本。`
        : '全部发布批次：每家门店取所属批次的最新发布版本；先用上方批次下拉可缩小到单一批次。'

  const selectableRows = records.filter(canSelect)
  const pageSelected = selectableRows.filter((task) => selectedSet.has(task.id))
  const allPageSelected = selectableRows.length > 0 && pageSelected.length === selectableRows.length
  const somePageSelected = pageSelected.length > 0 && !allPageSelected

  const togglePage = (next: boolean) => {
    const ids = selectableRows.map((task) => task.id)
    if (!next) {
      setSelectedIds((current) => removeSelection(current, ids))
      return
    }
    setSelectedIds((current) => {
      const result = addSelection(current, ids)
      if (result.overflow) toast.push(`一次最多分配 ${HUB_SELECTION_LIMIT} 家门店`, 'info')
      return result.ids
    })
  }

  const columns: DataColumn<HubTask>[] = [
    {
      id: 'select',
      header: (
        <Checkbox
          checked={allPageSelected ? true : somePageSelected ? 'indeterminate' : false}
          disabled={!selectableRows.length}
          label={<span className="sr-only">全选本页可选门店</span>}
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
            disabled={!canSelect(task)}
            label={<span className="sr-only">选择门店 {formatStore(task.source_name).name}</span>}
            onCheckedChange={() => toggleSelection(task)}
          />
        </span>
      ),
    },
    {
      id: 'store',
      header: '门店 / 批次版本',
      className: 'min-w-56',
      width: '18rem',
      cell: (task) => {
        const store = formatStore(task.source_name)
        return (
          <div className="min-w-0">
            <button type="button" className="block max-w-full truncate text-left text-default hover:underline focus-visible:underline" title={task.source_name} onClick={(event) => { event.stopPropagation(); setDetailTask(task) }}>
              {store.name || '未命名门店'}
            </button>
            <span className="mt-0.5 block truncate text-2xs text-tertiary" title={[store.code, task.batch_name, task.version].filter(Boolean).join(' · ')}>
              {[store.code, task.batch_name, task.version].filter(Boolean).join(' · ') || '批次信息未记录'}
            </span>
            {task.runnable !== true ? (
              <span className="mt-1 block truncate text-2xs text-warning" title={task.reject_label || task.label || task.message || '当前不可分配'}>
                {task.reject_label || task.label || task.message || '当前不可分配'}
              </span>
            ) : null}
          </div>
        )
      },
    },
    {
      id: 'device',
      header: '采集电脑',
      className: 'min-w-32 whitespace-nowrap',
      width: '11rem',
      cell: (task) => {
        const assignment = assignmentMap.get(task.id)
        if (!assignment) return <span className="text-tertiary">待分配</span>
        const name = assignment.worker_name || workers.find((worker) => worker.id === assignment.worker_id)?.name || assignment.worker_id
        return (
          <span className="inline-flex min-w-0 flex-col">
            <span className="truncate text-default" title={name}>{name}</span>
            <span className="text-2xs text-secondary">{assignmentStatusOf(assignment.status).label}</span>
          </span>
        )
      },
    },
    {
      id: 'status',
      header: '采集状态',
      className: 'whitespace-nowrap',
      width: '7rem',
      cell: (task) => {
        const meta = storeStateOf(task)
        return <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
      },
    },
    {
      id: 'detail',
      header: '操作',
      className: 'whitespace-nowrap',
      width: '5.5rem',
      sticky: 'right',
      cell: (task) => <Button color="secondary" variant="outline" size="sm" onClick={(event) => { event.stopPropagation(); setDetailTask(task) }}>详情</Button>,
    },
  ]

  return (
    <section className="space-y-6" aria-labelledby="hub-workbench-heading">
      <PageHeader
        title="采集工作台"
        headingId="hub-workbench-heading"
        actions={
          <>
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              aria-pressed={debugView}
              onClick={() => setDebugView((current) => !current)}
            >
              {debugView ? '关闭调试视图' : '调试视图'}
            </Button>
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              onClick={() => {
                void statusQuery.refetch()
                void sharedQuery.refetch()
              }}
            >
              刷新
            </Button>
            <Button color="primary" size="sm" disabled={!selectedIds.length} onClick={() => setAssignOpen(true)}>
              分配选中 {selectedIds.length ? `${selectedIds.length} 家` : '门店'}
            </Button>
          </>
        }
      />

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-7" aria-label="门店状态统计">
        <StatCard label="门店总数" value={totalStores} active={filter === 'all'} onClick={() => updateQuery({ filter: 'all' })} />
        <StatCard label="待采集" value={Number(counts.pending) || 0} active={filter === 'pending'} onClick={() => updateQuery({ filter: 'pending' })} />
        <StatCard label="采集中" value={Number(counts.running) || 0} active={filter === 'running'} onClick={() => updateQuery({ filter: 'running' })} />
        <StatCard label="已截图待复核" value={Number(counts.captured) || 0} active={filter === 'captured'} onClick={() => updateQuery({ filter: 'captured' })} />
        <StatCard label="待检查" value={Number(counts.review) || 0} active={filter === 'review'} onClick={() => updateQuery({ filter: 'review' })} />
        <StatCard label="不合格" value={Number(counts.rejected) || 0} tone="danger" active={filter === 'rejected'} onClick={() => updateQuery({ filter: 'rejected' })} />
        <StatCard label="待重新选码" value={Number(counts.returned) || 0} tone="warning" active={filter === 'returned'} onClick={() => updateQuery({ filter: 'returned' })} />
      </section>

      <section className="rounded-xl border border-subtle bg-surface flex flex-wrap items-center justify-between gap-3 px-4 py-3" aria-label="采集电脑概况">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-sm">
          <span className="text-default">采集电脑 {activeWorkers.length} 台</span>
          <span className="text-secondary">在线 {onlineWorkers.length} 台</span>
          <span className="text-secondary">已分配门店 {assignedCount} 家</span>
        </div>
        <Link className="text-sm text-default underline-offset-2 hover:underline" to="/devices">
          管理采集电脑
        </Link>
      </section>

      <section className="rounded-xl border border-subtle bg-surface space-y-4 p-4 md:p-5" aria-labelledby="hub-stores-heading">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 id="hub-stores-heading" className="heading-xs">
              门店列表
            </h2>
            <p className="mt-0.5 text-xs text-secondary">{scopeText}</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <SegmentedControl
              aria-label="批次范围"
              value={scope}
              onChange={(next) => updateQuery({ scope: next, batch: '' })}
              size="sm"
              pill
            >
              <SegmentedControl.Option value="current">当前素材</SegmentedControl.Option>
              <SegmentedControl.Option value="all">全部发布批次</SegmentedControl.Option>
            </SegmentedControl>
            {scope === 'all' ? (
              <div className="w-44">
                <Select
                  aria-label="按发布批次筛选"
                  placeholder="全部发布批次"
                  value={batch}
                  options={[
                    { value: '', label: '全部发布批次' },
                    ...(queue?.batches ?? []).map((item) => ({ value: item.id, label: item.name })),
                  ]}
                  onChange={(option) => updateQuery({ batch: option.value })}
                />
              </div>
            ) : null}
            <div className="w-36">
              <Select
                aria-label="按状态筛选"
                value={filter}
                options={STATUS_OPTIONS}
                onChange={(option) => updateQuery({ filter: option.value })}
              />
            </div>
            <div className="w-36">
              <Select
                aria-label="列表排序"
                value={sort}
                options={SORT_OPTIONS}
                onChange={(option) => updateQuery({ sort: option.value })}
              />
            </div>
            <div className="w-60">
              <SearchInput
                label="搜索门店"
                placeholder="搜索门店名称或图片"
                value={queryText}
                onChange={setQueryText}
              />
            </div>
            {hasFilters ? (
              <Button
                color="secondary"
                variant="ghost"
                size="sm"
                onClick={() => {
                  setQueryText('')
                  updateQuery({ scope: 'all', batch: '', filter: '', q: '' })
                }}
              >
                清除筛选
              </Button>
            ) : null}
          </div>
        </div>

        {statusQuery.isError ? (
          <StatePanel
            variant="error"
            title="门店列表读取失败"
            description={errorMessage(statusQuery.error)}
            action={
              <Button color="secondary" variant="outline" size="sm" onClick={() => void statusQuery.refetch()}>
                重试
              </Button>
            }
          />
        ) : (
          <>
            <BulkActionBar
              count={selectedIds.length}
              hint={`本页可选 ${selectableRows.length} 家 · 已选 ${pageSelected.length} 家 · 上限 ${HUB_SELECTION_LIMIT} 家，翻页后选择保留`}
              onClear={() => setSelectedIds([])}
            >
              <Button color="secondary" variant="ghost" size="sm" onClick={() => setSelectedOpen(true)}>
                查看已选
              </Button>
            </BulkActionBar>

            <AppDataTable
              caption="Hub 门店列表"
              columns={columns}
              rows={records}
              minWidthClass="min-w-[820px]"
              rowKey={(task) => task.id}
              loading={statusQuery.isFetching && !statusQuery.data}
              onRowActivate={setDetailTask}
              emptyState={
                <StatePanel
                  variant="empty"
                  title={hasFilters ? '没有符合条件的门店' : '还没有可分配的门店'}
                  description={
                    hasFilters
                      ? '调整批次范围、状态或搜索关键词后再试。'
                      : '发布素材批次后，门店会出现在这里等待分配。'
                  }
                  action={
                    hasFilters ? (
                      <Button
                        color="secondary"
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setQueryText('')
                          updateQuery({ scope: 'all', batch: '', filter: '', q: '' })
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
              page={queue?.page ?? page}
              pageCount={queue?.pages ?? 1}
              total={queue?.total ?? 0}
              pageSize={size}
              pageSizeOptions={PAGE_SIZES}
              onPageChange={(next) => updateQuery({ page: String(next) }, { resetPage: false })}
              onPageSizeChange={(next) => updateQuery({ size: String(next) })}
            />
          </>
        )}
      </section>

      {scope === 'current' && !release ? (
        <Alert
          color="warning"
          variant="soft"
          title="当前没有已发布的素材快照"
          description="“当前素材”只展示当前发布版本；请在素材清洗中发布批次，或切换到“全部发布批次”。"
        />
      ) : null}

      {debugView ? (
        <section className="rounded-xl border border-subtle bg-surface" aria-labelledby="hub-debug-heading">
          <div className="border-b border-subtle px-4 py-3">
            <h2 id="hub-debug-heading" className="heading-sm">
              最近活动
            </h2>
          </div>
          {events.length ? (
            <ul className="max-h-80 divide-y divide-subtle overflow-y-auto">
              {events.slice(0, 80).map((event, index) => (
                <li
                  key={`${event.time ?? ''}-${index}`}
                  className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-2 text-xs"
                >
                  <span className="tabular-nums text-tertiary">{event.time}</span>
                  <span className="min-w-0 flex-1 text-default">{event.message}</span>
                  {event.level && event.level !== 'info' ? (
                    <StatusBadge tone={event.level === 'error' ? 'danger' : 'warning'}>{event.level}</StatusBadge>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="px-4 py-4 text-sm text-tertiary">暂无运行日志。</p>
          )}
        </section>
      ) : null}

      <AssignDialog
        open={assignOpen}
        onOpenChange={setAssignOpen}
        tasks={selectedTasks}
        workers={workers}
        onExclude={(ids) => setSelectedIds((current) => removeSelection(current, ids))}
        onAssigned={handleAssigned}
      />

      <HubStoreDetailDrawer
        task={detailTask ? records.find((task) => task.id === detailTask.id) ?? detailTask : null}
        onClose={() => setDetailTask(null)}
      />

      <SelectedStoresDrawer
        open={selectedOpen}
        onOpenChange={setSelectedOpen}
        tasks={selectedTasks}
        onRemove={(id) => setSelectedIds((current) => removeSelection(current, [id]))}
        onClear={() => setSelectedIds([])}
        onAssign={() => {
          setSelectedOpen(false)
          setAssignOpen(true)
        }}
      />
    </section>
  )
}

function SelectedStoresDrawer({
  open,
  onOpenChange,
  tasks,
  onRemove,
  onClear,
  onAssign,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  tasks: HubSelectedTask[]
  onRemove: (id: string) => void
  onClear: () => void
  onAssign: () => void
}) {
  return (
    <AppDrawer
      open={open}
      onOpenChange={onOpenChange}
      title={`已选门店 ${tasks.length} 家`}
      description="选择会跨页保留；分配成功后自动清空。"
      footer={
        <div className="flex items-center justify-between gap-3">
          <Button color="secondary" variant="ghost" size="sm" disabled={!tasks.length} onClick={onClear}>
            清空全部
          </Button>
          <Button color="primary" size="sm" disabled={!tasks.length} onClick={onAssign}>
            去分配
          </Button>
        </div>
      }
    >
      <ul className="space-y-1">
        {tasks.map((task) => {
          const store = formatStore(task.source_name ?? '')
          return (
            <li key={task.id} className="flex items-center justify-between gap-3 rounded-lg px-2 py-2 hover:bg-secondary-ghost-hover">
              <span className="min-w-0">
                <span className="block truncate text-sm text-default">{store.name || '未命名门店'}</span>
                <span className="block truncate text-2xs text-tertiary">
                  {[store.code, task.batch_name, task.version].filter(Boolean).join(' · ') || '批次信息未记录'}
                </span>
              </span>
              <Button color="secondary" variant="ghost" size="sm" onClick={() => onRemove(task.id)}>
                移除
              </Button>
            </li>
          )
        })}
      </ul>
    </AppDrawer>
  )
}
