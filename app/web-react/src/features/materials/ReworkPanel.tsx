import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { SegmentedControl } from '@openai/apps-sdk-ui/components/SegmentedControl'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { AppPagination } from '../../components/AppPagination'
import { BulkActionBar } from '../../components/BulkActionBar'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { FilterToolbar } from '../../components/FilterToolbar'
import { SearchInput } from '../../components/SearchInput'
import { StatePanel } from '../../components/StatePanel'
import { StatCard } from '../../components/StatCard'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { adjacentVisibleKey, nextVisibleKey, useContinuityPreference } from '../continuity/useContinuityPreference'
import { formatStore } from '../hub/format'
import { decisionMeta, storeKey } from './format'
import { useRework, useReworkActions } from './hooks'
import type { QueueFilter } from './StoreReviewWorkspace'
import type {
  MaterialStoreSummary,
  ReworkCategory,
  ReworkPublishResponse,
  ReworkRecord,
} from './types'

const StoreReviewWorkspace = lazy(() =>
  import('./StoreReviewWorkspace').then((m) => ({ default: m.StoreReviewWorkspace })),
)

type CategoryFilter = 'all' | ReworkCategory
type ConfirmMode = 'return' | 'publish' | 'publish_all' | null

const CATEGORY_META: Record<ReworkCategory, { label: string; tone: 'warning' | 'danger' | 'info' }> = {
  pending: { label: '待处理', tone: 'warning' },
  rejected: { label: '不合格', tone: 'danger' },
  unpublished: { label: '待发布', tone: 'info' },
}

const ALL_BATCHES = '__all__'
const PAGE_SIZE = 50

function categoryToQueueFilter(category: CategoryFilter): QueueFilter {
  if (category === 'pending') return 'pending'
  if (category === 'unpublished') return 'confirmed'
  if (category === 'rejected') return 'skipped'
  return 'all'
}

function queueFilterToCategory(filter: QueueFilter): CategoryFilter {
  if (filter === 'pending') return 'pending'
  if (filter === 'confirmed') return 'unpublished'
  if (filter === 'skipped') return 'rejected'
  return 'all'
}

function recordToStoreSummary(record: ReworkRecord): MaterialStoreSummary {
  return {
    store_id: record.store_id,
    source_name: record.store_name,
    source_folder: record.source_folder,
    analysis_status: '',
    reason: record.reason_detail,
    recommendation: false,
    decision: record.decision,
    recommendation_level: '',
    wechat_validation_status: '',
    rejection: record.reason_code
      ? {
          code: record.reason_code,
          label: record.reason_label,
          detail: record.reason_detail,
          marked_at: record.updated_at,
          reviewed_at: '',
        }
      : null,
    selected: null,
    batch_id: record.batch_id,
    batch_name: record.batch_name,
    batch_status: record.batch_status ?? 'completed',
    queue_key: storeKey(record.batch_id, record.store_id),
  }
}

function groupByBatch(targets: ReworkRecord[]): { batchId: string; batchName: string; count: number }[] {
  const map = new Map<string, { batchId: string; batchName: string; count: number }>()
  for (const record of targets) {
    const existing = map.get(record.batch_id)
    if (existing) {
      existing.count += 1
    } else {
      map.set(record.batch_id, {
        batchId: record.batch_id,
        batchName: record.batch_name || record.batch_id,
        count: 1,
      })
    }
  }
  return Array.from(map.values())
}

/** 跨批次处理：项目内全批次状态统计、原地跨批次连续选码处理、跨页全选与统一增量发布。 */
export function ReworkPanel() {
  const toast = useToast()
  const [category, setCategory] = useState<CategoryFilter>('all')
  const [batchFilter, setBatchFilter] = useState<string>(ALL_BATCHES)
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<string[]>([])
  const [confirm, setConfirm] = useState<ConfirmMode>(null)
  const [error, setError] = useState('')
  const [lastResult, setLastResult] = useState<ReworkPublishResponse | null>(null)
  const [detailKey, setDetailKey] = useState<string | null>(null)
  const [continuousReview, setContinuousReview] = useContinuityPreference('materials.continuous-review')

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 300)
    return () => window.clearTimeout(timer)
  }, [search])

  const reworkQuery = useRework(query)
  const records = useMemo(() => reworkQuery.data?.records ?? [], [reworkQuery.data])
  const summary = reworkQuery.data?.summary
  const actions = useReworkActions()

  // 列表刷新后剔除已不存在的选择，避免对已处理门店重复操作。
  useEffect(() => {
    if (!reworkQuery.data) return
    const keys = new Set(reworkQuery.data.records.map((record) => storeKey(record.batch_id, record.store_id)))
    setSelected((current) => {
      const next = current.filter((key) => keys.has(key))
      return next.length === current.length ? current : next
    })
  }, [reworkQuery.data])

  const batchOptions = useMemo(() => {
    const counts = new Map<string, { name: string; count: number }>()
    for (const record of records) {
      const item = counts.get(record.batch_id)
      if (item) item.count += 1
      else counts.set(record.batch_id, { name: record.batch_name || record.batch_id, count: 1 })
    }
    return [
      { value: ALL_BATCHES, label: `全部批次（${counts.size}）` },
      ...Array.from(counts.entries()).map(([id, info]) => ({
        value: id,
        label: `${info.name}（${info.count}）`,
      })),
    ]
  }, [records])

  // 当所选批次在刷新后不再有待处理/返工项时，自动回到“全部批次”。
  useEffect(() => {
    if (batchFilter === ALL_BATCHES) return
    if (!records.some((record) => record.batch_id === batchFilter)) {
      setBatchFilter(ALL_BATCHES)
    }
  }, [batchFilter, records])

  useEffect(() => {
    setPage(1)
  }, [category, batchFilter, query])

  const batchScoped = useMemo(
    () => (batchFilter === ALL_BATCHES ? records : records.filter((record) => record.batch_id === batchFilter)),
    [records, batchFilter],
  )
  const scopedCounts = useMemo(() => {
    let pending = 0
    let rejected = 0
    let unpublished = 0
    for (const record of batchScoped) {
      if (record.category === 'pending') pending += 1
      else if (record.category === 'rejected') rejected += 1
      else unpublished += 1
    }
    return { all: batchScoped.length, pending, rejected, unpublished }
  }, [batchScoped])

  const filtered = useMemo(
    () => (category === 'all' ? batchScoped : batchScoped.filter((record) => record.category === category)),
    [batchScoped, category],
  )
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const currentPage = Math.min(page, pageCount)
  const pageRows = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE)

  const selectedSet = useMemo(() => new Set(selected), [selected])
  const selectedRecords = useMemo(
    () => records.filter((record) => selectedSet.has(storeKey(record.batch_id, record.store_id))),
    [records, selectedSet],
  )
  const publishableSelected = useMemo(
    () => selectedRecords.filter((record) => record.publishable),
    [selectedRecords],
  )
  const returnableSelected = useMemo(
    () => selectedRecords.filter((record) => record.category !== 'pending'),
    [selectedRecords],
  )
  const allPublishable = useMemo(
    () => batchScoped.filter((record) => record.publishable),
    [batchScoped],
  )

  const filteredKeys = useMemo(
    () => filtered.map((record) => storeKey(record.batch_id, record.store_id)),
    [filtered],
  )
  const selectedInFilteredCount = useMemo(
    () => filteredKeys.filter((key) => selectedSet.has(key)).length,
    [filteredKeys, selectedSet],
  )
  const pageKeys = pageRows.map((record) => storeKey(record.batch_id, record.store_id))
  const pageSelectedCount = pageKeys.filter((key) => selectedSet.has(key)).length
  const allPageSelected = pageKeys.length > 0 && pageSelectedCount === pageKeys.length

  // 跨批次审核工作台队列与相邻跳转
  const workspaceAllStores = useMemo(() => batchScoped.map(recordToStoreSummary), [batchScoped])
  const workspaceFilteredStores = useMemo(() => filtered.map(recordToStoreSummary), [filtered])
  const visibleKeysRef = useRef(filteredKeys)
  visibleKeysRef.current = filteredKeys
  const detailOrderRef = useRef(filteredKeys)
  useEffect(() => {
    if (detailKey) detailOrderRef.current = visibleKeysRef.current
  }, [detailKey])

  const detailIndex = detailKey ? filteredKeys.indexOf(detailKey) : -1
  const adjacentKey = (direction: -1 | 1): string | null => {
    if (!detailKey) return null
    if (detailIndex >= 0) return filteredKeys[detailIndex + direction] ?? null
    return adjacentVisibleKey(
      detailOrderRef.current,
      detailKey,
      filtered,
      (record) => storeKey(record.batch_id, record.store_id),
      direction,
    )
  }
  const prevDetailKey = adjacentKey(-1)
  const nextDetailKey = adjacentKey(1)
  const activeDetailRecord = useMemo(
    () => (detailKey ? records.find((record) => storeKey(record.batch_id, record.store_id) === detailKey) ?? null : null),
    [detailKey, records],
  )

  const openDetail = (record: ReworkRecord) => {
    setDetailKey(storeKey(record.batch_id, record.store_id))
  }

  const toggle = (key: string) => {
    setSelected((current) => (current.includes(key) ? current.filter((item) => item !== key) : [...current, key]))
  }

  const togglePage = (next: boolean) => {
    setSelected((current) => {
      if (next) return Array.from(new Set([...current, ...pageKeys]))
      return current.filter((key) => !pageKeys.includes(key))
    })
  }

  const selectAllFiltered = () => {
    setSelected((current) => Array.from(new Set([...current, ...filteredKeys])))
  }

  const runReturn = async () => {
    setError('')
    const targets = returnableSelected.length > 0 ? returnableSelected : selectedRecords
    const keys = targets.map((record) => storeKey(record.batch_id, record.store_id))
    try {
      const result = await actions.returnStores.mutateAsync(keys)
      if (result.returned) {
        toast.push(`已打回 ${result.returned} 家门店到「待处理」，可直接点「处理」重新选码`, 'success')
      }
      if (result.errors.length) {
        const nameByKey = new Map(targets.map((record) => [storeKey(record.batch_id, record.store_id), record.store_name]))
        const sample = result.errors
          .slice(0, 3)
          .map((item) => `${nameByKey.get(storeKey(item.batch_id, item.store_id)) ?? item.store_id}：${item.error}`)
          .join('；')
        setError(`部分门店打回失败：${sample}${result.errors.length > 3 ? '…' : ''}`)
        return
      }
      setSelected([])
      setConfirm(null)
    } catch (cause) {
      setError(errorMessage(cause))
    }
  }

  const publishTargets = confirm === 'publish_all' ? allPublishable : publishableSelected
  const publishBatchBreakdown = useMemo(() => groupByBatch(publishTargets), [publishTargets])

  const runPublish = () => {
    setError('')
    const targets = publishTargets
    const keys = targets.map((record) => storeKey(record.batch_id, record.store_id))
    actions.publish.mutate(keys, {
      onSuccess: (result) => {
        setLastResult(result)
        setConfirm(null)
        const successfulBatches = new Set(result.results.map((item) => item.batch_id))
        const failedBatches = new Set(result.errors.map((item) => item.batch_id))
        // 仅清除已成功发布的门店；失败批次及当前不可发布的选中项保留，便于核对和重试。
        const completedKeys = new Set(
          targets
            .filter((record) => successfulBatches.has(record.batch_id) && !failedBatches.has(record.batch_id))
            .map((record) => storeKey(record.batch_id, record.store_id)),
        )
        setSelected((current) => current.filter((key) => !completedKeys.has(key)))
        const published = result.results.reduce((sum, item) => sum + (item.task_scan.added || 0), 0)
        toast.push(
          result.errors.length
            ? `已增量发布 ${result.results.length} 个批次，${result.errors.length} 个批次失败`
            : `已跨批次增量发布 ${result.results.length} 个批次，新增采集任务 ${published} 家`,
          result.errors.length ? 'danger' : 'success',
        )
      },
      onError: (cause) => setError(errorMessage(cause)),
    })
  }

  const columns: DataColumn<ReworkRecord>[] = [
    {
      id: 'select',
      header: (
        <Checkbox
          label="本页全选"
          checked={allPageSelected ? true : pageSelectedCount > 0 ? 'indeterminate' : false}
          onCheckedChange={(next) => togglePage(next)}
          aria-label="全选当前页"
        />
      ),
      className: 'w-10',
      width: '3.25rem',
      sticky: 'left',
      cell: (record) => {
        const key = storeKey(record.batch_id, record.store_id)
        return (
          <div onClick={(event) => event.stopPropagation()}>
            <Checkbox
              checked={selectedSet.has(key)}
              onCheckedChange={() => toggle(key)}
              aria-label={`选择 ${record.store_name}`}
            />
          </div>
        )
      },
    },
    {
      id: 'store',
      header: '门店 / 批次',
      className: 'min-w-56',
      width: '18rem',
      cell: (record) => {
        const store = formatStore(record.store_name)
        const meta = [store.code, record.batch_name].filter(Boolean).join(' · ')
        return (
          <div className="min-w-0">
            <span className="block truncate text-default" title={record.store_name}>{store.name || record.store_name}</span>
            <span className="mt-0.5 block truncate text-2xs text-tertiary" title={[meta, record.source_folder].filter(Boolean).join(' · ')}>
              {meta || record.source_folder || '批次信息未记录'}
            </span>
          </div>
        )
      },
    },
    {
      id: 'category',
      header: '状态分类',
      className: 'whitespace-nowrap',
      width: '6.5rem',
      cell: (record) => (
        <StatusBadge tone={CATEGORY_META[record.category]?.tone ?? 'secondary'}>
          {CATEGORY_META[record.category]?.label ?? record.category}
        </StatusBadge>
      ),
    },
    {
      id: 'decision',
      header: '当前决策',
      className: 'whitespace-nowrap',
      width: '7.5rem',
      cell: (record) => {
        const meta = decisionMeta(record.decision)
        return <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
      },
    },
    {
      id: 'reason',
      header: '原因 / 备注',
      className: 'min-w-40',
      width: '14rem',
      cell: (record) =>
        record.reason_label ? (
          <span className="text-secondary">
            <span className="block truncate" title={record.reason_label}>{record.reason_label}</span>
            {record.reason_detail ? (
              <span className="line-clamp-2 text-2xs text-tertiary" title={record.reason_detail}>{record.reason_detail}</span>
            ) : null}
          </span>
        ) : (
          <span className="text-tertiary">—</span>
        ),
    },
    {
      id: 'version',
      header: '批次版本',
      className: 'whitespace-nowrap',
      width: '6rem',
      cell: (record) => <span className="text-secondary">{record.version || '未发布'}</span>,
    },
    {
      id: 'publishable',
      header: '可发布',
      className: 'whitespace-nowrap',
      width: '5.5rem',
      cell: (record) =>
        record.publishable ? <StatusBadge tone="success">可发布</StatusBadge> : <span className="text-tertiary">否</span>,
    },
    {
      id: 'actions',
      header: '',
      className: 'w-36 whitespace-nowrap text-right',
      width: '9rem',
      sticky: 'right',
      cell: (record) => (
        <div className="flex items-center justify-end gap-1.5" onClick={(event) => event.stopPropagation()}>
          <Button
            color={record.category === 'pending' ? 'primary' : 'secondary'}
            variant={record.category === 'pending' ? 'soft' : 'outline'}
            size="sm"
            onClick={() => openDetail(record)}
          >
            {record.category === 'pending' ? '处理' : '详情'}
          </Button>
          {record.category !== 'pending' ? (
            <Button
              color="secondary"
              variant="ghost"
              size="sm"
              onClick={() => {
                setSelected([storeKey(record.batch_id, record.store_id)])
                setConfirm('return')
              }}
            >
              打回
            </Button>
          ) : null}
        </div>
      ),
    },
  ]

  const activeFilterCount = [
    search,
    category !== 'all' ? category : '',
    batchFilter !== ALL_BATCHES ? batchFilter : '',
  ].filter(Boolean).length

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4" aria-label="跨批次状态统计">
        <StatCard
          label="待处理"
          value={summary?.pending ?? 0}
          tone="warning"
          active={category === 'pending'}
          onClick={() => setCategory((current) => (current === 'pending' ? 'all' : 'pending'))}
        />
        <StatCard
          label="不合格"
          value={summary?.rejected ?? 0}
          tone="danger"
          active={category === 'rejected'}
          onClick={() => setCategory((current) => (current === 'rejected' ? 'all' : 'rejected'))}
        />
        <StatCard
          label="已选码待发布"
          value={summary?.unpublished ?? 0}
          tone="success"
          active={category === 'unpublished'}
          onClick={() => setCategory((current) => (current === 'unpublished' ? 'all' : 'unpublished'))}
        />
        <StatCard
          label="涉及批次"
          value={summary?.batches ?? 0}
          active={category === 'all' && batchFilter === ALL_BATCHES}
          onClick={() => {
            setCategory('all')
            setBatchFilter(ALL_BATCHES)
          }}
        />
      </section>

      <section className="space-y-3 rounded-xl border border-subtle p-4" aria-labelledby="rework-list-heading">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="rework-list-heading" className="heading-xs">
            跨批次门店
          </h2>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              color="primary"
              size="sm"
              disabled={!allPublishable.length || actions.publish.isPending}
              onClick={() => {
                setError('')
                setConfirm('publish_all')
              }}
            >
              发布全部待发布（{allPublishable.length}）
            </Button>
            <Button color="secondary" variant="outline" size="sm" onClick={() => void reworkQuery.refetch()}>
              刷新
            </Button>
          </div>
        </div>

        <FilterToolbar
          label="跨批次门店筛选"
          activeCount={activeFilterCount}
          onClear={() => {
            setSearch('')
            setCategory('all')
            setBatchFilter(ALL_BATCHES)
          }}
          summary={`当前筛选 ${filtered.length} 家`}
        >
          <SearchInput
            className="w-60"
            label="跨批次门店"
            placeholder="搜索门店或批次名称"
            value={search}
            onChange={setSearch}
          />
          <SegmentedControl<CategoryFilter>
            value={category}
            onChange={(next) => setCategory(next)}
            aria-label="状态分类筛选"
          >
            <SegmentedControl.Option value="all">全部（{scopedCounts.all}）</SegmentedControl.Option>
            <SegmentedControl.Option value="pending">待处理（{scopedCounts.pending}）</SegmentedControl.Option>
            <SegmentedControl.Option value="rejected">不合格（{scopedCounts.rejected}）</SegmentedControl.Option>
            <SegmentedControl.Option value="unpublished">待发布（{scopedCounts.unpublished}）</SegmentedControl.Option>
          </SegmentedControl>
          {batchOptions.length > 2 ? (
            <div className="w-48">
              <Select
                aria-label="按批次筛选"
                value={batchFilter}
                placeholder="全部批次"
                options={batchOptions}
                onChange={(option) => setBatchFilter(option.value)}
              />
            </div>
          ) : null}
        </FilterToolbar>

        <label className="flex items-center gap-2 text-xs text-secondary">
          <input
            type="checkbox"
            checked={continuousReview}
            onChange={(event) => setContinuousReview(event.target.checked)}
          />
          提交本店决策后自动进入下一家（支持跨批次连续处理）
        </label>

        <BulkActionBar
          count={selected.length}
          hint={`其中可发布 ${publishableSelected.length} 家`}
          onClear={() => setSelected([])}
        >
          {filtered.length > selectedInFilteredCount ? (
            <Button color="secondary" variant="ghost" size="sm" onClick={selectAllFiltered}>
              全选当前筛选全部 {filtered.length} 家
            </Button>
          ) : null}
          <Button
            color="primary"
            size="sm"
            disabled={!publishableSelected.length || actions.publish.isPending}
            onClick={() => {
              setError('')
              setConfirm('publish')
            }}
          >
            发布已选门店（{publishableSelected.length}）
          </Button>
          <Button
            color="secondary"
            variant="outline"
            size="sm"
            disabled={!returnableSelected.length || actions.returnStores.isPending}
            onClick={() => {
              setError('')
              setConfirm('return')
            }}
          >
            打回已选门店（{returnableSelected.length}）
          </Button>
        </BulkActionBar>

        {reworkQuery.isError ? (
          <StatePanel
            variant="error"
            title="跨批次清单读取失败"
            description={errorMessage(reworkQuery.error)}
            action={
              <Button color="secondary" variant="outline" size="sm" onClick={() => void reworkQuery.refetch()}>
                重试
              </Button>
            }
          />
        ) : (
          <AppDataTable
            caption="跨批次门店清单"
            columns={columns}
            rows={pageRows}
            minWidthClass="min-w-[1120px]"
            rowKey={(record) => storeKey(record.batch_id, record.store_id)}
            loading={reworkQuery.isFetching && !reworkQuery.data}
            onRowActivate={openDetail}
            emptyState={
              <StatePanel
                variant="empty"
                title={activeFilterCount > 0 ? '没有匹配的门店' : '当前没有待处理或待发布门店'}
                description={
                  activeFilterCount > 0
                    ? '请尝试其他关键词或切换状态/批次筛选。'
                    : '项目内所有批次的门店都已完成确认并发布。'
                }
              />
            }
          />
        )}

        <AppPagination
          page={currentPage}
          pageCount={pageCount}
          total={filtered.length}
          pageSize={PAGE_SIZE}
          pageSizeOptions={[PAGE_SIZE]}
          onPageChange={setPage}
        />

        {lastResult?.errors.length ? (
          <Alert
            color="danger"
            variant="soft"
            title="部分批次发布失败"
            description={lastResult.errors
              .map((item) => `${item.batch_name || item.batch_id}：${item.error}`)
              .join('；')}
          />
        ) : null}
      </section>

      {detailKey && activeDetailRecord ? (
        <Suspense
          fallback={
            <div className="fixed inset-0 z-30 flex items-center justify-center bg-surface">
              <StatePanel variant="loading" title="正在打开跨批次审核工作台…" />
            </div>
          }
        >
          <StoreReviewWorkspace
            batchId={activeDetailRecord.batch_id}
            batchName={activeDetailRecord.batch_name}
            batchStatus={activeDetailRecord.batch_status ?? 'completed'}
            stores={workspaceAllStores}
            filteredStores={workspaceFilteredStores}
            activeStoreId={detailKey}
            filter={categoryToQueueFilter(category)}
            search={search}
            continuousReview={continuousReview}
            hasPrev={Boolean(prevDetailKey)}
            hasNext={Boolean(nextDetailKey)}
            prevStoreId={prevDetailKey}
            nextStoreId={nextDetailKey}
            onFilterChange={(next) => setCategory(queueFilterToCategory(next))}
            onSearchChange={setSearch}
            onContinuousReviewChange={setContinuousReview}
            onSelectStore={(key) => {
              const idx = filteredKeys.indexOf(key)
              if (idx >= 0) setPage(Math.floor(idx / PAGE_SIZE) + 1)
              setDetailKey(key)
            }}
            onDecisionSaved={async (savedKey) => {
              const refreshed = await reworkQuery.refetch()
              if (!continuousReview) return
              if (refreshed.isError || !refreshed.data) {
                toast.push('已保存当前门店，但刷新列表失败；请手动继续处理', 'info')
                return
              }
              const nextRecords =
                batchFilter === ALL_BATCHES
                  ? refreshed.data.records
                  : refreshed.data.records.filter((item) => item.batch_id === batchFilter)
              const currentScope =
                category === 'all' ? nextRecords : nextRecords.filter((item) => item.category === category)
              const pendingScope = currentScope.filter((item) => item.category === 'pending')
              const nextKey =
                nextVisibleKey(
                  detailOrderRef.current,
                  savedKey,
                  pendingScope,
                  (item) => storeKey(item.batch_id, item.store_id),
                ) ??
                nextVisibleKey(
                  detailOrderRef.current,
                  savedKey,
                  currentScope,
                  (item) => storeKey(item.batch_id, item.store_id),
                )
              if (!nextKey) {
                setDetailKey(null)
                toast.push('已处理完当前筛选范围内的后续门店，可统一增量发布', 'success')
                return
              }
              const nextIdx = currentScope.findIndex((item) => storeKey(item.batch_id, item.store_id) === nextKey)
              if (nextIdx >= 0) setPage(Math.floor(nextIdx / PAGE_SIZE) + 1)
              setDetailKey(nextKey)
            }}
            onClose={() => setDetailKey(null)}
            onNavigate={(delta) => {
              const nextKey = adjacentKey(delta)
              if (!nextKey) return
              const nextIdx = filteredKeys.indexOf(nextKey)
              if (nextIdx >= 0) setPage(Math.floor(nextIdx / PAGE_SIZE) + 1)
              setDetailKey(nextKey)
            }}
          />
        </Suspense>
      ) : null}

      <ConfirmActionDialog
        open={confirm === 'return'}
        title={returnableSelected.length > 1 ? `打回 ${returnableSelected.length} 家门店` : '打回门店'}
        description={
          <div className="space-y-1">
            <p>打回后门店回到「待处理」，可在当前页面直接点「处理」重新选码；不会新建批次，也不会修改已发布版本。</p>
            {returnableSelected.length > 1 ? <p>本次影响：{returnableSelected.length} 家门店。</p> : null}
          </div>
        }
        confirmLabel="确认打回"
        busy={actions.returnStores.isPending}
        error={error}
        onOpenChange={(next) => {
          if (!next && !actions.returnStores.isPending) {
            setConfirm(null)
            setError('')
          }
        }}
        onConfirm={() => void runReturn()}
      />

      <ConfirmActionDialog
        open={confirm === 'publish' || confirm === 'publish_all'}
        title={`跨批次增量发布 ${publishTargets.length} 家门店`}
        description={
          <div className="space-y-2">
            <p>按原批次分组，在各自最新快照上增量更新；不会影响其他门店，也不会切换默认版本。</p>
            {confirm === 'publish' && selectedRecords.length !== publishableSelected.length ? (
              <p>{selectedRecords.length - publishableSelected.length} 家非待发布门店不会参与本次发布。</p>
            ) : null}
            {publishBatchBreakdown.length > 0 ? (
              <ul className="max-h-36 space-y-1 overflow-y-auto rounded-lg border border-subtle bg-surface-secondary px-3 py-2 text-xs text-secondary">
                {publishBatchBreakdown.map((item) => (
                  <li key={item.batchId} className="flex items-center justify-between gap-2">
                    <span className="truncate">{item.batchName}</span>
                    <span className="shrink-0 tabular-nums">{item.count} 家</span>
                  </li>
                ))}
              </ul>
            ) : null}
            <p>发布后这些门店会重新进入采集任务分配，请确认选码已复核。</p>
          </div>
        }
        confirmLabel={`确认发布（${publishTargets.length} 家）`}
        busy={actions.publish.isPending}
        error={error}
        onOpenChange={(next) => {
          if (!next && !actions.publish.isPending) {
            setConfirm(null)
            setError('')
          }
        }}
        onConfirm={runPublish}
      />
    </div>
  )
}
