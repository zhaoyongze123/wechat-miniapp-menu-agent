import { useCallback, useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { AppPagination } from '../../components/AppPagination'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { PageHeader } from '../../components/PageHeader'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { useRole } from '../../app/RoleProvider'
import { AssignmentDetailDrawer } from './AssignmentDetailDrawer'
import { fetchAssignmentRecords, reclaimAssignments } from './api'
import { assignmentStatusOf, deviceStateOf, formatStore } from './format'
import { useHubSharedStatus } from './hooks'
import type { AssignmentPhase, AssignmentRecord, ReclaimResponse } from './types'

const PAGE_SIZES = [20, 30, 50, 100]
const PHASE_VALUES: AssignmentPhase[] = ['', 'assigned', 'ready', 'completed', 'reclaimed', 'history']

const PHASE_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'assigned', label: '待下载' },
  { value: 'ready', label: '已下载' },
  { value: 'completed', label: '后台已确认' },
  { value: 'reclaimed', label: '已收回' },
  { value: 'history', label: '旧结果留档' },
]

function parsePhase(value: string | null): AssignmentPhase {
  return PHASE_VALUES.includes(value as AssignmentPhase) ? (value as AssignmentPhase) : ''
}

function parsePositiveInt(value: string | null, fallback: number): number {
  const parsed = Number.parseInt(value ?? '', 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback
}

function isReclaimable(record: AssignmentRecord): boolean {
  return (record.status === 'assigned' || record.status === 'ready') && record.disposition !== 'history'
}

export function HubAssignmentsPage() {
  const role = useRole()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [selected, setSelected] = useState<string[]>([])
  const [detailId, setDetailId] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<{ ids: string[]; label: string } | null>(null)
  const [confirmError, setConfirmError] = useState('')
  const [reclaimResult, setReclaimResult] = useState<ReclaimResponse | null>(null)

  const workerId = searchParams.get('worker') ?? ''
  const phase = parsePhase(searchParams.get('phase'))
  const q = searchParams.get('q') ?? ''
  const page = parsePositiveInt(searchParams.get('page'), 1)
  const sizeParam = parsePositiveInt(searchParams.get('size'), 30)
  const size = PAGE_SIZES.includes(sizeParam) ? sizeParam : 30

  const updateQuery = useCallback(
    (patch: Record<string, string>, options: { resetPage?: boolean } = {}) => {
      setSearchParams(
        (previous) => {
          const next = new URLSearchParams(previous)
          for (const [key, value] of Object.entries(patch)) {
            if (!value) next.delete(key)
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

  const [queryText, setQueryText] = useState(q)
  useEffect(() => setQueryText(q), [q])
  useEffect(() => {
    if (queryText.trim() === q.trim()) return
    const timer = window.setTimeout(() => updateQuery({ q: queryText.trim() }), 250)
    return () => window.clearTimeout(timer)
  }, [queryText, q, updateQuery])

  const sharedQuery = useHubSharedStatus()
  const workers = useMemo(() => sharedQuery.data?.workers ?? [], [sharedQuery.data])

  const recordsQuery = useQuery({
    queryKey: ['hub-assignments', workerId, phase, q, page, size],
    queryFn: ({ signal }) => fetchAssignmentRecords({ workerId, phase, q, page, size }, signal),
    placeholderData: keepPreviousData,
    enabled: role.role === 'hub',
    refetchInterval: 10_000,
    staleTime: 4_000,
  })

  const serverPage = recordsQuery.data?.page
  useEffect(() => {
    if (recordsQuery.isFetching || recordsQuery.isPlaceholderData) return
    if (serverPage && serverPage !== page) updateQuery({ page: String(serverPage) }, { resetPage: false })
  }, [serverPage, page, recordsQuery.isFetching, recordsQuery.isPlaceholderData, updateQuery])

  const records = useMemo(() => recordsQuery.data?.records ?? [], [recordsQuery.data])
  const reclaimableRecords = useMemo(() => records.filter(isReclaimable), [records])
  const selectedSet = useMemo(() => new Set(selected), [selected])
  const pageSelected = reclaimableRecords.filter((record) => selectedSet.has(record.id))
  const allPageSelected = reclaimableRecords.length > 0 && pageSelected.length === reclaimableRecords.length
  const somePageSelected = pageSelected.length > 0 && !allPageSelected
  const detailRecord = records.find((record) => record.id === detailId) ?? null
  const detailIndex = detailRecord ? records.findIndex((record) => record.id === detailRecord.id) : -1
  const hasFilters = Boolean(workerId || phase || q.trim())

  // 翻页或状态变化后，只保留当前页仍可回收的选择；部分失败项保留以便用户核对。
  useEffect(() => {
    setSelected((current) => {
      const allowed = new Set(reclaimableRecords.map((record) => record.id))
      for (const item of reclaimResult?.results ?? []) {
        if (item.status === 'failed') allowed.add(item.id)
      }
      const next = current.filter((id) => allowed.has(id))
      return next.length === current.length ? current : next
    })
  }, [reclaimableRecords, reclaimResult])

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['hub-assignments'] })
    void queryClient.invalidateQueries({ queryKey: ['hub-status'] })
    void queryClient.invalidateQueries({ queryKey: ['hub-shared-status'] })
  }

  const mutation = useMutation({
    mutationFn: (ids: string[]) => reclaimAssignments(ids),
    onSuccess: (result) => {
      setConfirm(null)
      setConfirmError('')
      setDetailId(null)
      const successfulIds = new Set(result.results.filter((item) => item.status === 'reclaimed').map((item) => item.id))
      setSelected((current) => current.filter((id) => !successfulIds.has(id)))
      setReclaimResult(result)
      const hasFailures = result.results.some((item) => item.status === 'failed')
      toast.push(
        hasFailures ? `已回收 ${result.count} 条，${result.results.length - result.count} 条未回收` : `已回收 ${result.count} 条分配`,
        hasFailures ? 'danger' : 'success',
      )
      invalidate()
    },
    onError: (cause) => setConfirmError(errorMessage(cause)),
  })

  const toggle = (record: AssignmentRecord) => {
    if (!isReclaimable(record) && !selectedSet.has(record.id)) return
    setSelected((current) =>
      current.includes(record.id) ? current.filter((id) => id !== record.id) : [...current, record.id],
    )
  }

  const togglePage = (next: boolean) => {
    if (!next) {
      const ids = new Set(reclaimableRecords.map((record) => record.id))
      setSelected((current) => current.filter((id) => !ids.has(id)))
      return
    }
    setSelected((current) => [...new Set([...current, ...reclaimableRecords.map((record) => record.id)])])
  }

  if (role.loading) return <StatePanel variant="loading" title="正在确认当前角色…" />
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
    return <StatePanel variant="info" title="仅共享后台可查看分配记录" description="分配记录在共享后台查看；Windows 采集端可在采集工作台查看本机任务与结果。" />
  }

  const columns: DataColumn<AssignmentRecord>[] = [
    {
      id: 'select',
      header: (
        <Checkbox
          checked={allPageSelected ? true : somePageSelected ? 'indeterminate' : false}
          disabled={!reclaimableRecords.length}
          label={<span className="sr-only">全选本页可回收分配</span>}
          onCheckedChange={togglePage}
        />
      ),
      className: 'w-10',
      width: '3.25rem',
      sticky: 'left',
      cell: (record) => (
        <span className="inline-flex" onClick={(event) => event.stopPropagation()}>
          <Checkbox
            checked={selectedSet.has(record.id)}
            disabled={!isReclaimable(record) && !selectedSet.has(record.id)}
            label={<span className="sr-only">选择分配 {formatStore(record.store).name}</span>}
            onCheckedChange={() => toggle(record)}
          />
        </span>
      ),
    },
    {
      id: 'store',
      header: '门店',
      className: 'min-w-48',
      width: '18rem',
      cell: (record) => {
        const store = formatStore(record.store)
        const meta = [store.code, record.batch_name, record.revision].filter(Boolean).join(' · ')
        return (
          <div className="min-w-0">
            <span className="block truncate text-default" title={record.store}>
              {store.name || record.store}
            </span>
            <span className="mt-0.5 block truncate text-2xs text-tertiary" title={meta}>
              {meta}
            </span>
          </div>
        )
      },
    },
    {
      id: 'worker',
      header: '采集电脑',
      className: 'whitespace-nowrap',
      width: '10rem',
      cell: (record) => {
        const name = record.worker_name || record.worker_id
        return <span className="block truncate text-default" title={name}>{name}</span>
      },
    },
    {
      id: 'status',
      header: '状态',
      className: 'whitespace-nowrap',
      width: '13rem',
      cell: (record) => {
        const meta = assignmentStatusOf(record.status, record.disposition)
        return (
          <span className="inline-flex min-w-0 flex-col gap-0.5">
            <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
            {record.result_message ? (
              <span className="max-w-44 truncate text-2xs text-tertiary" title={record.result_message}>
                {record.result_message}
              </span>
            ) : null}
          </span>
        )
      },
    },
    {
      id: 'actions',
      header: '',
      className: 'w-32 whitespace-nowrap text-right',
      width: '10rem',
      sticky: 'right',
      cell: (record) => (
        <span className="inline-flex gap-1" onClick={(event) => event.stopPropagation()}>
          <Button color="secondary" variant="ghost" size="sm" onClick={() => setDetailId(record.id)}>
            详情
          </Button>
          {isReclaimable(record) ? (
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              onClick={() => {
                setConfirmError('')
                setConfirm({ ids: [record.id], label: formatStore(record.store).name || record.store })
              }}
            >
              回收
            </Button>
          ) : null}
        </span>
      ),
    },
  ]

  const workerOptions = [
    { value: '', label: '全部采集电脑' },
    ...workers.map((worker) => {
      const state = deviceStateOf(worker)
      const suffix = state.key === 'online' || state.key === 'offline' ? '' : ` · ${state.label}`
      return { value: worker.id, label: `${worker.name}${suffix}` }
    }),
  ]

  return (
    <section className="space-y-6" aria-labelledby="hub-assignments-heading">
      <PageHeader
        title="分配记录"
        headingId="hub-assignments-heading"
        actions={
          <>
            <Button color="secondary" variant="outline" size="sm" onClick={() => void recordsQuery.refetch()}>
              刷新
            </Button>
            <Button
              color="danger"
              size="sm"
              disabled={!selected.length}
              onClick={() => {
                setConfirmError('')
                setConfirm({ ids: [...selected], label: `所选 ${selected.length} 条分配` })
              }}
            >
              回收选中 {selected.length ? `${selected.length} 条` : ''}
            </Button>
          </>
        }
      />

      {reclaimResult ? (
        <Alert
          color={reclaimResult.results.some((item) => item.status === 'failed') ? 'warning' : 'success'}
          variant="soft"
          title={
            reclaimResult.results.some((item) => item.status === 'failed')
              ? `已回收 ${reclaimResult.count} 条，${reclaimResult.results.length - reclaimResult.count} 条未回收`
              : `已回收 ${reclaimResult.count} 条分配`
          }
          description={
            <div>
              <p>
                {reclaimResult.tasks.length
                  ? `${reclaimResult.tasks.length} 家门店当前符合重新分配条件。`
                  : reclaimResult.count
                    ? '已回收记录中没有当前可重新分配的门店，请先在素材清洗中处理版本或选码问题。'
                    : '没有记录完成回收。'}
              </p>
              {reclaimResult.results.some((item) => item.status === 'failed') ? (
                <ul className="mt-2 max-h-36 space-y-1 overflow-auto text-sm" aria-label="未回收项目及原因">
                  {reclaimResult.results.filter((item) => item.status === 'failed').map((item) => (
                    <li key={item.id} className="break-all" title={item.id}>
                      {item.store ? `${formatStore(item.store).name || item.store}：` : `${item.id}：`}{item.message}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          }
          actions={
            <div className="flex items-center gap-2">
              {reclaimResult.tasks.length ? (
                <Link
                  className="text-sm text-default underline-offset-2 hover:underline"
                  to={`/workbench?selected=${encodeURIComponent(reclaimResult.tasks.map((task) => task.id).join(','))}`}
                >
                  去分配这 {reclaimResult.tasks.length} 家
                </Link>
              ) : null}
              <Button color="secondary" variant="ghost" size="sm" onClick={() => setReclaimResult(null)}>
                关闭
              </Button>
            </div>
          }
        />
      ) : null}

      <section className="rounded-xl border border-subtle bg-surface space-y-4 p-4 md:p-5" aria-labelledby="hub-records-list-heading">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 id="hub-records-list-heading" className="heading-xs">
              分配明细
            </h2>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="w-52">
              <Select
                aria-label="按采集电脑筛选"
                value={workerId}
                options={workerOptions}
                onChange={(option) => updateQuery({ worker: option.value })}
              />
            </div>
            <div className="w-36">
              <Select
                aria-label="按分配状态筛选"
                value={phase}
                options={PHASE_OPTIONS}
                onChange={(option) => updateQuery({ phase: option.value })}
              />
            </div>
            <div className="w-64">
              <Input
                aria-label="搜索分配记录"
                placeholder="搜索门店、批次或任务 ID"
                value={queryText}
                onChange={(event) => setQueryText(event.target.value)}
              />
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-surface-secondary px-3 py-2 text-xs">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-default">已选 {selected.length} 条</span>
            <span className="text-secondary">本页可回收 {reclaimableRecords.length} 条</span>
            <span className="text-tertiary">仅当前页可批量选择</span>
          </div>
          <Button color="secondary" variant="ghost" size="sm" disabled={!selected.length} onClick={() => setSelected([])}>
            清空选择
          </Button>
        </div>

        {recordsQuery.isError ? (
          <StatePanel
            variant="error"
            title="分配记录读取失败"
            description={errorMessage(recordsQuery.error)}
            action={
              <Button color="secondary" variant="outline" size="sm" onClick={() => void recordsQuery.refetch()}>
                重试
              </Button>
            }
          />
        ) : (
          <>
            <AppDataTable
              caption="门店分配记录"
              columns={columns}
              rows={records}
              minWidthClass="min-w-[900px]"
              rowKey={(record) => record.id}
              loading={recordsQuery.isFetching && !recordsQuery.data}
              onRowActivate={(record) => setDetailId(record.id)}
              emptyState={
                <StatePanel
                  variant="empty"
                  title={hasFilters ? '没有符合条件的分配记录' : '还没有分配记录'}
                  description={
                    hasFilters
                      ? '调整采集电脑、状态或搜索条件后再试。'
                      : '在采集工作台把门店分配给采集电脑后，记录会出现在这里。'
                  }
                  action={
                    hasFilters ? (
                      <Button
                        color="secondary"
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setQueryText('')
                          updateQuery({ worker: '', phase: '', q: '' })
                        }}
                      >
                        清除筛选
                      </Button>
                    ) : (
                      <Link className="text-sm text-default underline-offset-2 hover:underline" to="/workbench">
                        去分配门店
                      </Link>
                    )
                  }
                />
              }
            />

            <AppPagination
              page={recordsQuery.data?.page ?? page}
              pageCount={recordsQuery.data?.pages ?? 1}
              total={recordsQuery.data?.total ?? 0}
              pageSize={size}
              pageSizeOptions={PAGE_SIZES}
              onPageChange={(next) => updateQuery({ page: String(next) }, { resetPage: false })}
              onPageSizeChange={(next) => updateQuery({ size: String(next) })}
            />
          </>
        )}
      </section>

      <AssignmentDetailDrawer
        record={detailRecord}
        onOpenChange={(open) => {
          if (!open) setDetailId(null)
        }}
        onReclaim={(record) => {
          setConfirmError('')
          setConfirm({ ids: [record.id], label: formatStore(record.store).name || record.store })
        }}
        {...(detailIndex >= 0
          ? {
              onPrev: () => {
                const previous = records[detailIndex - 1]
                if (previous) setDetailId(previous.id)
              },
              onNext: () => {
                const next = records[detailIndex + 1]
                if (next) setDetailId(next.id)
              },
              hasPrev: detailIndex > 0,
              hasNext: detailIndex < records.length - 1,
              position: `${detailIndex + 1} / ${records.length}`,
            }
          : {})}
      />

      <ConfirmActionDialog
        open={Boolean(confirm)}
        title="回收分配"
        description={
          <div className="space-y-1">
            <p>将回收 {confirm?.label}。</p>
            <p>回收后门店立即解除占用，可重新分配；设备已下载但未上传的本机结果会转为历史留档，不会自动上传。</p>
          </div>
        }
        confirmLabel={confirm ? `回收 ${confirm.ids.length} 条` : '回收'}
        tone="danger"
        busy={mutation.isPending}
        error={confirmError}
        onOpenChange={(next) => {
          if (!next && !mutation.isPending) {
            setConfirm(null)
            setConfirmError('')
          }
        }}
        onConfirm={() => {
          if (confirm) mutation.mutate(confirm.ids)
        }}
      />
    </section>
  )
}
