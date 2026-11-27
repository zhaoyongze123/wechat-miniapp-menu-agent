import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { SegmentedControl } from '@openai/apps-sdk-ui/components/SegmentedControl'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { AppPagination } from '../../components/AppPagination'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { FilterToolbar } from '../../components/FilterToolbar'
import { ListPageLayout } from '../../components/ListPageLayout'
import { SearchInput } from '../../components/SearchInput'
import { StatePanel } from '../../components/StatePanel'
import { StatCard } from '../../components/StatCard'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { useRole } from '../../app/RoleProvider'
import { adjacentVisibleKey, useContinuityPreference, nextVisibleKey } from '../continuity/useContinuityPreference'
import { formatStore, formatTime } from '../hub/format'
import { AnalysisDialog } from './AnalysisDialog'
import { BatchManagementDialog } from './BatchManagementDialog'
import { ImportBatchDialog } from './ImportBatchDialog'
import { PublishDialog } from './PublishDialog'
import { ReworkPanel } from './ReworkPanel'
import { StoreReviewWorkspace } from './StoreReviewWorkspace'
import {
  RECOMMENDATION_LEVEL_LABELS,
  WECHAT_VALIDATION_LABELS,
  analysisStatusLabel,
  batchStatusMeta,
  decisionMeta,
  isJobActive,
  isJobOpen,
  normalizeDecision,
} from './format'
import { useBatch, useBatchActions, useBatchOptions, useBatches } from './hooks'
import type { MaterialStoreSummary, StoreDecision } from './types'

type StoreFilter = 'all' | StoreDecision
type ConfirmKind = 'reanalyze' | 'retry' | 'accept'
type MaterialsView = 'batch' | 'rework'

const PAGE_SIZE = 50

export function MaterialsPage() {
  const role = useRole()
  const toast = useToast()
  const [searchParams, setSearchParams] = useSearchParams()
  const [batchId, setBatchId] = useState(() => searchParams.get('batch') ?? '')
  const [view, setView] = useState<MaterialsView>(searchParams.get('view') === 'rework' ? 'rework' : 'batch')
  const [filter, setFilter] = useState<StoreFilter>('all')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(PAGE_SIZE)
  const [detailStoreId, setDetailStoreId] = useState<string | null>(() => searchParams.get('store') || null)
  // 质量审核跳转携带的门店只消费一次，之后仍按批次切换清空详情。
  const deepLinkStore = useRef(searchParams.get('store') ?? '')
  const [importOpen, setImportOpen] = useState(false)
  const [publishOpen, setPublishOpen] = useState(false)
  const [analysisOpen, setAnalysisOpen] = useState(false)
  const [managementOpen, setManagementOpen] = useState(false)
  const [confirmKind, setConfirmKind] = useState<ConfirmKind | null>(null)
  const [confirmError, setConfirmError] = useState('')
  const [continuousReview, setContinuousReview] = useContinuityPreference('materials.continuous-review')

  const batchesQuery = useBatches()
  const optionsQuery = useBatchOptions()
  const batchQuery = useBatch(batchId)
  const actions = useBatchActions(batchId)
  const batch = batchQuery.data

  // 首次进入或无有效批次时，优先选当前在用批次，否则取最新批次。
  useEffect(() => {
    if (batchId) return
    const options = optionsQuery.data ?? []
    if (!options.length) return
    const preferred = options.find((item) => item.current_active) ?? options[0]
    if (preferred) setBatchId(preferred.batch_id)
  }, [batchId, optionsQuery.data])

  useEffect(() => {
    if (!batchId) return
    const linked = deepLinkStore.current
    deepLinkStore.current = ''
    setDetailStoreId(linked || null)
    setFilter('all')
    setSearch('')
    setPage(1)
  }, [batchId])

  const stores = batch?.stores ?? []
  const counts = useMemo(() => {
    let pending = 0
    let confirmed = 0
    let skipped = 0
    let recommended = 0
    for (const store of stores) {
      const decision = normalizeDecision(store.decision)
      if (decision === 'pending') {
        pending += 1
        if (store.recommendation) recommended += 1
      } else if (decision === 'confirmed') confirmed += 1
      else skipped += 1
    }
    return { pending, confirmed, skipped, recommended }
  }, [stores])

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return stores.filter((store) => {
      if (filter !== 'all' && normalizeDecision(store.decision) !== filter) return false
      if (!needle) return true
      return `${store.source_name} ${store.source_folder}`.toLowerCase().includes(needle)
    })
  }, [filter, search, stores])
  const visibleStoreIds = filtered.map((store) => store.store_id)
  const visibleStoreIdsRef = useRef(visibleStoreIds)
  visibleStoreIdsRef.current = visibleStoreIds
  const detailOrderRef = useRef(visibleStoreIds)
  useEffect(() => {
    if (detailStoreId) detailOrderRef.current = visibleStoreIdsRef.current
  }, [detailStoreId])

  useEffect(() => {
    setPage(1)
  }, [filter, search])

  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize))
  const currentPage = Math.min(page, pageCount)
  const pageRows = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize)
  const detailIndex = detailStoreId ? filtered.findIndex((store) => store.store_id === detailStoreId) : -1
  const adjacentStoreId = (direction: -1 | 1) => {
    if (!detailStoreId) return null
    if (detailIndex >= 0) return filtered[detailIndex + direction]?.store_id ?? null
    return adjacentVisibleKey(detailOrderRef.current, detailStoreId, filtered, (store) => store.store_id, direction)
  }
  const prevStoreId = adjacentStoreId(-1)
  const nextStoreId = adjacentStoreId(1)
  const hasPreviousStore = Boolean(prevStoreId)
  const hasNextStore = Boolean(nextStoreId)

  const job = batch?.job ?? null
  const selectedOption = (optionsQuery.data ?? []).find((option) => option.batch_id === batchId)
  const jobOpen = isJobOpen(job?.status)
  const jobActive = isJobActive(job?.status)
  const status = batch?.status ?? ''
  const canAnalyze = status === 'ready_to_analyze' && !jobOpen
  const canReanalyze = status === 'completed' && !jobOpen && counts.pending > 0
  const canRetry = status === 'completed' && !jobOpen && (job?.failed_stores ?? 0) > 0
  const canAccept = status === 'completed' && !jobOpen && counts.recommended > 0
  const canPublish = status === 'completed' && (batch?.summary.selected_stores ?? 0) > 0

  const selectBatch = (nextBatchId: string) => {
    if (!nextBatchId) return
    setBatchId(nextBatchId)
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        next.set('batch', nextBatchId)
        if (view === 'rework') next.set('view', 'rework')
        else next.delete('view')
        return next
      },
      { replace: true },
    )
  }

  const switchView = (next: MaterialsView) => {
    setView(next)
    setSearchParams(
      (current) => {
        const params = new URLSearchParams(current)
        if (next === 'rework') params.set('view', 'rework')
        else params.delete('view')
        if (batchId) params.set('batch', batchId)
        return params
      },
      { replace: true },
    )
  }

  const pendingIds = useMemo(
    () =>
      stores
        .filter((store) => normalizeDecision(store.decision) === 'pending')
        .map((store) => store.store_id)
        .slice(0, 2000),
    [stores],
  )

  const runConfirmedAction = () => {
    if (!confirmKind) return
    setConfirmError('')
    const success = (message: string) => {
      toast.push(message, 'success')
      setConfirmKind(null)
    }
    const failure = (cause: unknown) => setConfirmError(errorMessage(cause))

    if (confirmKind === 'reanalyze') {
      actions.reanalyze.mutate(pendingIds, {
        onSuccess: () => success(`已提交 ${pendingIds.length} 家待审核门店重新分析`),
        onError: failure,
      })
      return
    }
    if (confirmKind === 'retry') {
      actions.retry.mutate(undefined, {
        onSuccess: () => success('已提交失败门店重试'),
        onError: failure,
      })
      return
    }
    actions.accept.mutate(undefined, {
      onSuccess: (result) => success(`已自动确认 ${result.confirmed} 家推荐门店`),
      onError: failure,
    })
  }

  const confirmBusy = actions.reanalyze.isPending || actions.retry.isPending || actions.accept.isPending

  const columns: DataColumn<MaterialStoreSummary>[] = [
    {
      id: 'store',
      header: '门店',
      className: 'min-w-56',
      width: '20rem',
      cell: (store) => {
        const parsed = formatStore(store.source_name)
        const meta = [parsed.code, batch?.name].filter(Boolean).join(' · ')
        return (
          <div className="min-w-0">
            <span className="block truncate text-default" title={store.source_name}>{parsed.name}</span>
            <span className="mt-0.5 block truncate text-2xs text-tertiary" title={meta || store.source_name}>
              {meta || '批次信息未记录'}
            </span>
          </div>
        )
      },
    },
    {
      id: 'decision',
      header: '决策',
      className: 'whitespace-nowrap',
      width: '6rem',
      cell: (store) => {
        const meta = decisionMeta(store.decision)
        return <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
      },
    },
    {
      id: 'recommendation',
      header: '推荐 / 校验',
      className: 'min-w-40',
      width: '11rem',
      cell: (store) => (
        <div className="space-y-1">
          {store.recommendation ? (
            <StatusBadge tone="info">{RECOMMENDATION_LEVEL_LABELS[store.recommendation_level] ?? '推荐'}</StatusBadge>
          ) : (
            <span className="text-2xs text-tertiary">无自动推荐</span>
          )}
          {store.wechat_validation_status ? (
            <span className="block text-2xs text-tertiary">
              {WECHAT_VALIDATION_LABELS[store.wechat_validation_status] ?? store.wechat_validation_status}
            </span>
          ) : null}
        </div>
      ),
    },
    {
      id: 'selection',
      header: '选码',
      className: 'whitespace-nowrap',
      width: '9rem',
      cell: (store) => {
        if (!store.selected) return <span className="text-tertiary">—</span>
        return (
          <span className="text-secondary">
            第 {store.selected.image_index + 1} 张
            {store.selected.candidate_index !== null ? ` · 候选 ${store.selected.candidate_index + 1}` : ' · 人工框选'}
          </span>
        )
      },
    },
    {
      id: 'analysis',
      header: '分析状态',
      className: 'min-w-40',
      width: '14rem',
      cell: (store) => (
        <div className="min-w-0">
          <span className="text-xs text-secondary">{analysisStatusLabel(store.analysis_status)}</span>
          {store.rejection ? (
            <span className="mt-0.5 block truncate text-2xs text-danger" title={store.rejection.label || store.rejection.code}>
              {store.rejection.label || store.rejection.code}
            </span>
          ) : store.reason ? (
            <span className="mt-0.5 block truncate text-2xs text-tertiary" title={store.reason}>{store.reason}</span>
          ) : null}
        </div>
      ),
    },
    {
      id: 'actions',
      header: '',
      className: 'w-20 whitespace-nowrap text-right',
      width: '5.5rem',
      sticky: 'right',
      cell: (store) => (
        <Button
          color="secondary"
          variant="outline"
          size="sm"
          onClick={(event) => {
            event.stopPropagation()
            setDetailStoreId(store.store_id)
          }}
        >
          详情
        </Button>
      ),
    },
  ]

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
    return <StatePanel variant="info" title="仅共享后台可清洗素材" description="Windows 采集端只负责采集与上传，素材审核在共享后台完成。" />
  }

  if (batchesQuery.isLoading && !batchesQuery.data) {
    return <StatePanel variant="loading" title="正在读取素材批次…" />
  }

  if (batchesQuery.isError) {
    return (
      <StatePanel
        variant="error"
        title="素材批次读取失败"
        description={errorMessage(batchesQuery.error)}
        action={
          <Button color="secondary" variant="outline" size="sm" onClick={() => void batchesQuery.refetch()}>
            重试
          </Button>
        }
      />
    )
  }

  return (
    <>
      <ListPageLayout
        title="素材清洗"
        headingId="materials-heading"
        actions={
          <>
            <Button color="secondary" variant="outline" size="sm" onClick={() => setManagementOpen(true)}>
              批次管理
            </Button>
            <Button color="primary" size="sm" onClick={() => setImportOpen(true)}>
              新建批次
            </Button>
          </>
        }
        summary={
          <SegmentedControl<MaterialsView> value={view} onChange={switchView} aria-label="素材视图">
            <SegmentedControl.Option value="batch">批次清洗</SegmentedControl.Option>
            <SegmentedControl.Option value="rework">跨批次返工</SegmentedControl.Option>
          </SegmentedControl>
        }
      >
      {view === 'rework' ? (
        <ReworkPanel />
      ) : (optionsQuery.data ?? []).length === 0 ? (
        <StatePanel
          variant="empty"
          title="还没有素材批次"
          description="上传图片或导入服务器目录创建第一个批次，分析完成后即可开始逐店选码。"
          action={
            <Button color="primary" size="sm" onClick={() => setImportOpen(true)}>
              新建批次
            </Button>
          }
        />
      ) : (
        <>
          <section className="space-y-4 rounded-xl border border-subtle bg-surface p-4 md:p-5" aria-label="批次概览">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <div className="w-full sm:w-80">
                  <Select
                    aria-label="选择素材批次"
                    value={batchId}
                    placeholder="选择批次"
                    options={(optionsQuery.data ?? []).map((option) => ({
                      value: option.batch_id,
                      label: `${option.name}${option.current_active ? '（默认版本）' : ''}`,
                      description: `${formatTime(option.created_at)} · ${option.workflow.label}`,
                    }))}
                    onChange={(option) => selectBatch(option.value)}
                  />
                </div>
                {batch ? (
                  <>
                    <StatusBadge tone={batchStatusMeta(batch.status).tone}>
                      {batchStatusMeta(batch.status).label}
                    </StatusBadge>
                    <StatusBadge tone="secondary">{batch.workflow.label}</StatusBadge>
                    {batch.release ? <StatusBadge tone="success">默认 {batch.release.version}</StatusBadge> : null}
                  </>
                ) : null}
              </div>
              <Button
                color="secondary"
                variant="outline"
                size="sm"
                onClick={() => {
                  void batchQuery.refetch()
                  void optionsQuery.refetch()
                }}
              >
                刷新
              </Button>
            </div>

            {batch ? (
              <>
                <p className="text-xs text-secondary">
                  {selectedOption?.created_at ? `创建于 ${formatTime(selectedOption.created_at)} · ` : ''}
                  {batch.source_kind === 'upload' ? '浏览器上传' : '服务器目录'}
                  {batch.draft_changes
                    ? ` · 较当前版本 新增 ${batch.draft_changes.added} / 移除 ${batch.draft_changes.removed} / 变更 ${batch.draft_changes.changed}`
                    : ''}
                </p>

                <div className="flex flex-wrap items-center gap-2">
                  {canAnalyze ? (
                    <Button
                      color="primary"
                      size="sm"
                      disabled={actions.analyze.isPending}
                      onClick={() =>
                        actions.analyze.mutate(undefined, {
                          onSuccess: () => {
                            toast.push('已开始分析批次', 'success')
                            setAnalysisOpen(true)
                          },
                          onError: (cause) => toast.push(errorMessage(cause), 'danger'),
                        })
                      }
                    >
                      {actions.analyze.isPending ? '正在提交…' : '开始分析'}
                    </Button>
                  ) : null}
                  {canReanalyze ? (
                    <Button color="secondary" variant="outline" size="sm" onClick={() => setConfirmKind('reanalyze')}>
                      重新分析待审核（{counts.pending}）
                    </Button>
                  ) : null}
                  {canRetry ? (
                    <Button color="secondary" variant="outline" size="sm" onClick={() => setConfirmKind('retry')}>
                      重试失败门店（{job?.failed_stores ?? 0}）
                    </Button>
                  ) : null}
                  {canAccept ? (
                    <Button color="secondary" variant="outline" size="sm" onClick={() => setConfirmKind('accept')}>
                      自动确认推荐（{counts.recommended}）
                    </Button>
                  ) : null}
                  {status === 'completed' ? (
                    <Button
                      color="primary"
                      variant="solid"
                      size="sm"
                      disabled={!canPublish}
                      onClick={() => setPublishOpen(true)}
                    >
                      发布版本
                    </Button>
                  ) : null}
                  <Button color="secondary" variant="ghost" size="sm" onClick={() => setAnalysisOpen(true)}>
                    分析进度{jobActive ? '（运行中）' : ''}
                  </Button>
                </div>

                {batch.error ? <Alert color="danger" variant="soft" title="批次分析出错" description={batch.error} /> : null}
                {job && jobOpen ? (
                  <Alert
                    color={job.status === 'paused' ? 'warning' : 'info'}
                    variant="soft"
                    title={`分析作业${jobStatusText(job.status)}：${job.done_stores}/${job.total_stores} 家`}
                    description={job.error || job.message || '完成后门店会进入可审核状态，可随时打开分析进度查看明细。'}
                  />
                ) : null}
              </>
            ) : batchQuery.isLoading ? (
              <StatePanel variant="loading" title="正在读取批次明细…" />
            ) : batchQuery.isError ? (
              <StatePanel variant="error" title="批次明细读取失败" description={errorMessage(batchQuery.error)} />
            ) : null}
          </section>

          {batch ? (
            <>
              <section className="grid grid-cols-2 gap-3 sm:grid-cols-5" aria-label="批次统计">
                <StatCard label="门店" value={batch.summary.total_stores} />
                <StatCard label="原图" value={batch.summary.total_images} />
                <StatCard label="待审核" value={batch.summary.pending_stores} tone="warning" />
                <StatCard label="已确认" value={batch.summary.confirmed_stores} tone="success" />
                <StatCard label="不合格" value={batch.summary.skipped_stores} tone="danger" />
              </section>

              <section className="space-y-3 rounded-xl border border-subtle p-4" aria-labelledby="material-store-list">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <h2 id="material-store-list" className="heading-xs">
                    门店列表
                  </h2>
                </div>

                <FilterToolbar
                  label="素材门店筛选"
                  activeCount={[search, filter !== 'all' ? filter : ''].filter(Boolean).length}
                  onClear={() => {
                    setSearch('')
                    setFilter('all')
                  }}
                  summary={`共 ${stores.length} 家门店`}
                >
                  <SearchInput
                    className="w-64"
                    label="门店"
                    placeholder="搜索门店名称或目录"
                    value={search}
                    onChange={setSearch}
                  />
                  <SegmentedControl<StoreFilter> value={filter} onChange={setFilter} aria-label="门店决策筛选">
                    <SegmentedControl.Option value="all">全部（{stores.length}）</SegmentedControl.Option>
                    <SegmentedControl.Option value="pending">待审核（{counts.pending}）</SegmentedControl.Option>
                    <SegmentedControl.Option value="confirmed">已确认（{counts.confirmed}）</SegmentedControl.Option>
                    <SegmentedControl.Option value="skipped">不合格（{counts.skipped}）</SegmentedControl.Option>
                  </SegmentedControl>
                </FilterToolbar>

                <label className="flex items-center gap-2 text-xs text-secondary">
                  <input
                    type="checkbox"
                    checked={continuousReview}
                    onChange={(event) => setContinuousReview(event.target.checked)}
                  />
                  提交本店决策后自动进入下一家
                </label>

                <AppDataTable
                  caption="素材门店列表"
                  columns={columns}
                  rows={pageRows}
                  minWidthClass="min-w-[1060px]"
                  rowKey={(store) => store.store_id}
                  loading={batchQuery.isFetching && !batchQuery.data}
                  onRowActivate={(store) => setDetailStoreId(store.store_id)}
                  emptyState={
                    <StatePanel
                      variant="empty"
                      title={search ? '没有匹配的门店' : '当前筛选下没有门店'}
                      description={search ? '请尝试其他关键词或切换筛选条件。' : '切换筛选条件查看其他决策状态的门店。'}
                    />
                  }
                />

                <AppPagination
                  page={currentPage}
                  pageCount={pageCount}
                  total={filtered.length}
                  pageSize={pageSize}
                  onPageChange={setPage}
                  onPageSizeChange={(size) => {
                    setPageSize(size)
                    setPage(1)
                  }}
                />
              </section>
            </>
          ) : null}
        </>
      )}

      <ImportBatchDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        onCreated={(created) => {
          setView('batch')
          setBatchId(created.batch_id)
        }}
      />

      <PublishDialog
        open={publishOpen}
        batch={batch ?? null}
        onOpenChange={setPublishOpen}
        onPublished={() => {
          void batchesQuery.refetch()
          void optionsQuery.refetch()
          void batchQuery.refetch()
        }}
      />

      <AnalysisDialog open={analysisOpen} batchId={batchId} onOpenChange={setAnalysisOpen} />

      <BatchManagementDialog
        open={managementOpen}
        selectedBatchId={batchId}
        onOpenChange={setManagementOpen}
        onSelectBatch={selectBatch}
      />

      {detailStoreId && batch ? (
        <StoreReviewWorkspace
          batchId={batchId}
          batchName={batch.name}
          batchStatus={status}
          stores={stores}
          filteredStores={filtered}
          activeStoreId={detailStoreId}
          filter={filter}
          search={search}
          continuousReview={continuousReview}
          hasPrev={hasPreviousStore}
          hasNext={hasNextStore}
          prevStoreId={prevStoreId}
          nextStoreId={nextStoreId}
          onFilterChange={setFilter}
          onSearchChange={setSearch}
          onContinuousReviewChange={setContinuousReview}
          onSelectStore={(storeId) => {
            const idx = filtered.findIndex((item) => item.store_id === storeId)
            if (idx >= 0) setPage(Math.floor(idx / pageSize) + 1)
            setDetailStoreId(storeId)
          }}
          onDecisionSaved={async (storeId) => {
            if (!continuousReview) return
            const refreshed = await batchQuery.refetch()
            if (refreshed.isError || !refreshed.data) {
              toast.push('已保存当前门店，但刷新列表失败；请手动继续处理', 'info')
              return
            }
            const needle = search.trim().toLowerCase()
            const currentScope = refreshed.data.stores.filter((item) => {
              if (filter !== 'all' && normalizeDecision(item.decision) !== filter) return false
              return !needle || `${item.source_name} ${item.source_folder}`.toLowerCase().includes(needle)
            })
            // 优先进入下一家仍为待审核的门店；若当前筛选无后续待审则按列表顺序推进。
            const pendingScope = currentScope.filter((item) => normalizeDecision(item.decision) === 'pending')
            const nextId =
              nextVisibleKey(detailOrderRef.current, storeId, pendingScope, (item) => item.store_id) ??
              nextVisibleKey(detailOrderRef.current, storeId, currentScope, (item) => item.store_id)
            if (!nextId) {
              setDetailStoreId(null)
              toast.push('已处理完当前筛选范围内的后续待审核门店', 'success')
              return
            }
            const nextIndex = currentScope.findIndex((store) => store.store_id === nextId)
            if (nextIndex >= 0) {
              setPage(Math.floor(nextIndex / pageSize) + 1)
            }
            setDetailStoreId(nextId)
          }}
          onClose={() => setDetailStoreId(null)}
          onNavigate={(delta) => {
            const nextId = adjacentStoreId(delta)
            if (!nextId) return
            const nextIndex = filtered.findIndex((store) => store.store_id === nextId)
            if (nextIndex >= 0) setPage(Math.floor(nextIndex / pageSize) + 1)
            setDetailStoreId(nextId)
          }}
        />
      ) : null}

      <ConfirmActionDialog
        open={confirmKind !== null}
        title={
          confirmKind === 'reanalyze'
            ? `重新分析 ${pendingIds.length} 家待审核门店`
            : confirmKind === 'retry'
              ? '重试失败门店'
              : `自动确认 ${counts.recommended} 家推荐门店`
        }
        description={
          confirmKind === 'reanalyze' ? (
            <div className="space-y-1">
              <p>仅重跑当前仍为待审核的门店；已确认与不合格门店不受影响。</p>
              <p>重新分析会覆盖这些门店的候选与推荐结果，已有人工框选需要重新确认。</p>
            </div>
          ) : confirmKind === 'retry' ? (
            <div className="space-y-1">
              <p>只重跑上次任务中失败且未确认的门店，会先结束旧任务再创建新任务。</p>
              <p>已经人工确认的门店不会被重试。</p>
            </div>
          ) : (
            <div className="space-y-1">
              <p>对所有仍为待审核且带自动推荐的门店执行确认，共 {counts.recommended} 家。</p>
              <p>自动确认采用当前推荐候选，可在门店详情中逐条撤销或调整。</p>
            </div>
          )
        }
        confirmLabel={confirmKind === 'accept' ? '确认自动确认' : '确认执行'}
        busy={confirmBusy}
        error={confirmError}
        onOpenChange={(next) => {
          if (!next && !confirmBusy) {
            setConfirmKind(null)
            setConfirmError('')
          }
        }}
        onConfirm={runConfirmedAction}
      />
      </ListPageLayout>
    </>
  )
}

function jobStatusText(status: string | undefined): string {
  switch (status) {
    case 'queued':
      return '排队中'
    case 'running':
      return '进行中'
    case 'pause_requested':
      return '正在暂停'
    case 'paused':
      return '已暂停'
    case 'interrupted':
      return '已中断'
    default:
      return status ?? ''
  }
}
