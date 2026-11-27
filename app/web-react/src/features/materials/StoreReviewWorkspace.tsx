import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { SegmentedControl } from '@openai/apps-sdk-ui/components/SegmentedControl'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDialog } from '../../components/AppDialog'
import { ImagePreview } from '../../components/ImagePreview'
import { SearchInput } from '../../components/SearchInput'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { useUnsavedChanges } from '../../components/UnsavedChangesProvider'
import { errorMessage } from '../../api/client'
import { useProject } from '../projects/ProjectProvider'
import { formatStore } from '../hub/format'
import { fetchStore, storeCandidateUrl, storeImageUrl, storeSelectedUrl } from './api'
import {
  MAX_STORE_UPLOAD_FILES,
  PURPOSE_LABELS,
  PURPOSE_SOURCE_LABELS,
  RECOMMENDATION_LEVEL_LABELS,
  REJECTION_REASONS,
  WECHAT_VALIDATION_LABELS,
  analysisStatusLabel,
  decisionMeta,
  formatBytes,
  normalizeDecision,
} from './format'
import { materialsKeys, useStore, useStoreActions } from './hooks'
import { ReviewImageCanvas } from './ReviewImageCanvas'
import {
  drawCroppedPreview,
  findInitialImageAndRecommendation,
  getDetectorErrorMessage,
  isUsableBox,
} from './reviewGeometry'
import type {
  Box,
  MaterialStoreDetail,
  MaterialStoreSummary,
  QrCandidate,
  StoreDecision,
  UploadMode,
} from './types'

export type QueueFilter = 'all' | StoreDecision

function summaryQueueKey(item: MaterialStoreSummary): string {
  return item.queue_key ?? item.store_id
}

interface StoreReviewWorkspaceProps {
  batchId: string
  batchName: string
  batchStatus: string
  stores: MaterialStoreSummary[]
  filteredStores: MaterialStoreSummary[]
  activeStoreId: string
  filter: QueueFilter
  search: string
  continuousReview: boolean
  hasPrev: boolean
  hasNext: boolean
  prevStoreId: string | null
  nextStoreId: string | null
  onFilterChange: (next: QueueFilter) => void
  onSearchChange: (next: string) => void
  onContinuousReviewChange: (next: boolean) => void
  onSelectStore: (storeId: string) => void
  onNavigate: (delta: -1 | 1) => void
  onDecisionSaved: (storeId: string) => void | Promise<void>
  onClose: () => void
}

export function StoreReviewWorkspace({
  batchId,
  batchName,
  batchStatus,
  stores,
  filteredStores,
  activeStoreId,
  filter,
  search,
  continuousReview,
  hasPrev,
  hasNext,
  prevStoreId,
  nextStoreId,
  onFilterChange,
  onSearchChange,
  onContinuousReviewChange,
  onSelectStore,
  onNavigate,
  onDecisionSaved,
  onClose,
}: StoreReviewWorkspaceProps) {
  const { projectId } = useProject()
  const queryClient = useQueryClient()

  const [queueCollapsed, setQueueCollapsed] = useState(false)
  const [shortcutsOpen, setShortcutsOpen] = useState(false)
  const [hasDraft, setHasDraft] = useState(false)
  const [guardOpen, setGuardOpen] = useState(false)

  const pendingActionRef = useRef<(() => void) | null>(null)
  const saveBeforeLeaveRef = useRef(false)
  const saveActionRef = useRef<(() => void) | null>(null)
  const queueScrollRef = useRef<HTMLDivElement>(null)
  const activeStoreIdRef = useRef(activeStoreId)
  activeStoreIdRef.current = activeStoreId

  useUnsavedChanges(hasDraft, '门店选码或补图尚未保存，离开将丢弃未保存修改。')

  const storeSummary = useMemo(
    () =>
      stores.find((item) => summaryQueueKey(item) === activeStoreId) ??
      filteredStores.find((item) => summaryQueueKey(item) === activeStoreId) ??
      null,
    [activeStoreId, filteredStores, stores],
  )
  const effectiveBatchId = storeSummary?.batch_id || batchId
  const effectiveBatchName = storeSummary?.batch_name || batchName
  const effectiveBatchStatus = storeSummary?.batch_status || batchStatus
  const effectiveStoreId = storeSummary?.store_id || activeStoreId
  const effectiveStoreIdRef = useRef(effectiveStoreId)
  effectiveStoreIdRef.current = effectiveStoreId
  const query = useStore(effectiveBatchId, effectiveStoreId)

  // 计算审核进度与剩余待审数量（基于整个批次或跨批次清单，便于高频审核实时掌握剩余工作量）。
  const progress = useMemo(() => {
    let pending = 0
    let confirmed = 0
    let skipped = 0
    for (const item of stores) {
      const dec = normalizeDecision(item.decision)
      if (dec === 'pending') pending += 1
      else if (dec === 'confirmed') confirmed += 1
      else skipped += 1
    }
    const reviewed = confirmed + skipped
    return { total: stores.length, reviewed, pending, confirmed, skipped }
  }, [stores])

  // 仅预加载相邻 ±1 家门店详情与首图，绝不整批预加载。
  useEffect(() => {
    if (!projectId) return
    const neighbors = [nextStoreId, prevStoreId].filter((id): id is string => Boolean(id && id !== activeStoreId))
    for (const neighborKey of neighbors) {
      const neighborItem =
        stores.find((item) => summaryQueueKey(item) === neighborKey) ??
        filteredStores.find((item) => summaryQueueKey(item) === neighborKey)
      const neighborBatchId = neighborItem?.batch_id || batchId
      const neighborStoreId = neighborItem?.store_id || neighborKey
      if (!neighborBatchId || !neighborStoreId) continue
      void queryClient
        .prefetchQuery({
          queryKey: materialsKeys.store(projectId, neighborBatchId, neighborStoreId),
          queryFn: ({ signal }) => fetchStore(neighborBatchId, neighborStoreId, signal, projectId),
          staleTime: 30_000,
        })
        .then(() => {
          const cached = queryClient.getQueryData<MaterialStoreDetail>(
            materialsKeys.store(projectId, neighborBatchId, neighborStoreId),
          )
          if (cached && cached.images.length > 0) {
            const { imageIndex } = findInitialImageAndRecommendation(cached)
            const preload = new Image()
            preload.src = storeImageUrl(neighborBatchId, neighborStoreId, imageIndex, projectId)
          }
        })
        .catch(() => undefined)
    }
  }, [activeStoreId, batchId, filteredStores, nextStoreId, prevStoreId, projectId, queryClient, stores])

  // 浏览器关闭/刷新保护。
  useEffect(() => {
    if (!hasDraft) return
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => window.removeEventListener('beforeunload', warnBeforeUnload)
  }, [hasDraft])

  const performOrGuard = useCallback(
    (action: () => void) => {
      if (!hasDraft) {
        action()
        return
      }
      pendingActionRef.current = action
      setGuardOpen(true)
    },
    [hasDraft],
  )

  const handleSaved = useCallback(
    (savedStoreId: string) => {
      // 防止异步回调串店：仅当前门店的保存结果才触发后续跳转。
      if (savedStoreId !== effectiveStoreIdRef.current) return
      setHasDraft(false)
      if (saveBeforeLeaveRef.current) {
        saveBeforeLeaveRef.current = false
        setGuardOpen(false)
        const action = pendingActionRef.current
        pendingActionRef.current = null
        action?.()
        return
      }
      void onDecisionSaved(activeStoreIdRef.current)
    },
    [onDecisionSaved],
  )

  const cancelGuard = () => {
    saveBeforeLeaveRef.current = false
    pendingActionRef.current = null
    setGuardOpen(false)
  }

  const parsedName = storeSummary ? formatStore(storeSummary.source_name).name : '门店审核'
  const decisionBadge = decisionMeta(query.data?.decision ?? storeSummary?.decision)

  return (
    <div className="fixed inset-0 z-30 flex flex-col bg-surface" role="region" aria-label="门店素材审核工作台">
      {/* 顶部精简导航与进度栏 */}
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-subtle bg-surface px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Button
            color="secondary"
            variant="outline"
            size="sm"
            onClick={() => performOrGuard(onClose)}
          >
            ← 返回列表
          </Button>
          <Button
            color="secondary"
            variant="ghost"
            size="sm"
            aria-pressed={queueCollapsed}
            onClick={() => setQueueCollapsed((prev) => !prev)}
          >
            {queueCollapsed ? '展开队列' : '收起队列'}
          </Button>
          <div className="flex min-w-0 items-center gap-2 pl-1">
            {effectiveBatchName ? (
              <span className="hidden max-w-44 truncate text-xs text-tertiary md:inline" title={effectiveBatchName}>
                {effectiveBatchName} /
              </span>
            ) : null}
            <h1 className="max-w-72 truncate text-sm font-semibold text-default" title={storeSummary?.source_name ?? parsedName}>
              {parsedName}
            </h1>
            <StatusBadge tone={decisionBadge.tone}>{decisionBadge.label}</StatusBadge>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs tabular-nums text-secondary">
            已审 {progress.reviewed}/{progress.total} · 剩余待审 {progress.pending}
          </span>
          <label className="hidden items-center gap-1.5 rounded-md border border-subtle px-2 py-1 text-xs text-secondary sm:flex">
            <input
              type="checkbox"
              checked={continuousReview}
              onChange={(event) => onContinuousReviewChange(event.target.checked)}
            />
            连续审核
          </label>
          <div className="flex items-center gap-1">
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              disabled={!hasPrev}
              onClick={() => performOrGuard(() => onNavigate(-1))}
            >
              上一家 (K)
            </Button>
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              disabled={!hasNext}
              onClick={() => performOrGuard(() => onNavigate(1))}
            >
              下一家 (J)
            </Button>
          </div>
          <Button
            color="secondary"
            variant="ghost"
            size="sm"
            aria-label="快捷键说明"
            onClick={() => setShortcutsOpen(true)}
          >
            ?
          </Button>
        </div>
      </header>

      {/* 三栏主体区域 */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* 左栏：门店审核队列（可折叠，切换门店保持滚动位置） */}
        {!queueCollapsed ? (
          <aside className="flex w-72 shrink-0 flex-col border-r border-subtle bg-surface" aria-label="门店审核队列">
            <div className="space-y-2 border-b border-subtle p-2.5">
              <SearchInput
                label="搜索门店"
                placeholder="搜索门店名或目录"
                value={search}
                onChange={onSearchChange}
              />
              <SegmentedControl<QueueFilter>
                value={filter}
                onChange={onFilterChange}
                aria-label="队列状态筛选"
                size="sm"
              >
                <SegmentedControl.Option value="all">全 {progress.total}</SegmentedControl.Option>
                <SegmentedControl.Option value="pending">待 {progress.pending}</SegmentedControl.Option>
                <SegmentedControl.Option value="confirmed">确 {progress.confirmed}</SegmentedControl.Option>
                <SegmentedControl.Option value="skipped">弃 {progress.skipped}</SegmentedControl.Option>
              </SegmentedControl>
            </div>

            <div ref={queueScrollRef} className="min-h-0 flex-1 overflow-y-auto p-1.5">
              {filteredStores.length === 0 ? (
                <p className="px-2 py-6 text-center text-xs text-tertiary">当前筛选下没有门店</p>
              ) : (
                <ul className="space-y-1">
                  {filteredStores.map((item) => {
                    const key = summaryQueueKey(item)
                    const active = key === activeStoreId
                    const meta = decisionMeta(item.decision)
                    const name = formatStore(item.source_name).name
                    const detailLabel = item.recommendation
                      ? RECOMMENDATION_LEVEL_LABELS[item.recommendation_level] ?? '已推荐'
                      : item.rejection
                        ? item.rejection.label || item.rejection.code
                        : item.analysis_status
                          ? analysisStatusLabel(item.analysis_status)
                          : ''
                    const subline = [item.batch_name, detailLabel].filter(Boolean).join(' · ')
                    return (
                      <li key={key}>
                        <button
                          type="button"
                          onClick={() => {
                            if (key === activeStoreId) return
                            performOrGuard(() => onSelectStore(key))
                          }}
                          aria-current={active ? 'true' : undefined}
                          className={[
                            'flex w-full items-start justify-between gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors',
                            active
                              ? 'border-primary-solid bg-secondary-soft text-default'
                              : 'border-transparent hover:bg-secondary-ghost-hover text-secondary',
                          ].join(' ')}
                        >
                          <div className="min-w-0 flex-1">
                            <span className="block truncate text-xs font-medium text-default" title={item.source_name}>
                              {name}
                            </span>
                            <span className="mt-0.5 block truncate text-2xs text-tertiary" title={subline}>
                              {subline || '—'}
                            </span>
                          </div>
                          <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}
            </div>

            <div className="shrink-0 border-t border-subtle px-3 py-2 text-2xs text-tertiary">
              列表 {filteredStores.length} 家 · 待审 {progress.pending} 家
            </div>
          </aside>
        ) : null}

        {/* 中栏 + 右栏：当前门店审核主体 */}
        {!storeSummary ? (
          <div className="flex flex-1 items-center justify-center p-6">
            <StatePanel variant="empty" title="未找到门店" description="请从左侧队列选择一家门店开始审核。" />
          </div>
        ) : query.isLoading && !query.data ? (
          <div className="flex flex-1 items-center justify-center p-6">
            <StatePanel variant="loading" title="正在读取门店素材…" />
          </div>
        ) : query.isError ? (
          <div className="flex flex-1 items-center justify-center p-6">
            <StatePanel
              variant="error"
              title="门店素材读取失败"
              description={errorMessage(query.error)}
              action={
                <Button color="secondary" variant="outline" size="sm" onClick={() => void query.refetch()}>
                  重试
                </Button>
              }
            />
          </div>
        ) : query.data ? (
          <WorkspaceStoreBody
            key={`${effectiveBatchId}:${query.data.store_id}`}
            batchId={effectiveBatchId}
            batchStatus={effectiveBatchStatus}
            detail={query.data}
            hasPrev={hasPrev}
            hasNext={hasNext}
            shortcutsOpen={shortcutsOpen}
            guardOpen={guardOpen}
            onToggleShortcuts={() => setShortcutsOpen((prev) => !prev)}
            onDirtyChange={setHasDraft}
            onDecisionSaved={handleSaved}
            onNavigateStore={(delta) => performOrGuard(() => onNavigate(delta))}
            onExitWorkspace={() => performOrGuard(onClose)}
            saveActionRef={saveActionRef}
          />
        ) : null}
      </div>

      {/* 未保存修改拦截弹窗 */}
      <AppDialog
        open={guardOpen}
        title="修改尚未保存"
        description="离开当前门店会丢弃尚未保存的框选、补图或不合格说明。"
        onOpenChange={(next) => {
          if (!next) cancelGuard()
        }}
        footer={
          <div className="flex justify-end gap-2">
            <Button color="secondary" variant="outline" size="sm" onClick={cancelGuard}>
              继续编辑
            </Button>
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              onClick={() => {
                setHasDraft(false)
                setGuardOpen(false)
                const action = pendingActionRef.current
                pendingActionRef.current = null
                action?.()
              }}
            >
              放弃修改并离开
            </Button>
            {saveActionRef.current ? (
              <Button
                color="primary"
                size="sm"
                onClick={() => {
                  saveBeforeLeaveRef.current = true
                  saveActionRef.current?.()
                }}
              >
                确认选码并离开
              </Button>
            ) : null}
          </div>
        }
      >
        <p className="text-sm text-secondary">确认选码会提交到共享后台；保存成功后将自动继续跳转。</p>
      </AppDialog>

      {/* 快捷键说明弹窗 */}
      <AppDialog
        open={shortcutsOpen}
        onOpenChange={setShortcutsOpen}
        title="审核工作台快捷键"
        widthClass="w-[min(460px,92vw)]"
      >
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-xs">
          <dt className="font-mono text-default">J / ]</dt>
          <dd className="text-secondary">切换到下一家门店</dd>
          <dt className="font-mono text-default">K / [</dt>
          <dd className="text-secondary">切换到上一家门店</dd>
          <dt className="font-mono text-default">← / →</dt>
          <dd className="text-secondary">切换当前门店的上一张 / 下一张原图</dd>
          <dt className="font-mono text-default">1 – 9</dt>
          <dd className="text-secondary">快速选择对应序号的二维码候选</dd>
          <dt className="font-mono text-default">Enter</dt>
          <dd className="text-secondary">确认当前已选候选或人工框选</dd>
          <dt className="font-mono text-default">M</dt>
          <dd className="text-secondary">进入 / 退出手动框选模式</dd>
          <dt className="font-mono text-default">R</dt>
          <dd className="text-secondary">打开「标记不合格」弹窗</dd>
          <dt className="font-mono text-default">Esc</dt>
          <dd className="text-secondary">取消手动框选或返回门店列表</dd>
          <dt className="font-mono text-default">?</dt>
          <dd className="text-secondary">打开 / 关闭本快捷键说明</dd>
        </dl>
      </AppDialog>
    </div>
  )
}

interface WorkspaceStoreBodyProps {
  batchId: string
  batchStatus: string
  detail: MaterialStoreDetail
  hasPrev: boolean
  hasNext: boolean
  shortcutsOpen: boolean
  guardOpen: boolean
  onToggleShortcuts: () => void
  onDirtyChange: (dirty: boolean) => void
  onDecisionSaved: (storeId: string) => void
  onNavigateStore: (delta: -1 | 1) => void
  onExitWorkspace: () => void
  saveActionRef: React.MutableRefObject<(() => void) | null>
}

function WorkspaceStoreBody({
  batchId,
  batchStatus,
  detail,
  hasPrev,
  hasNext,
  shortcutsOpen,
  guardOpen,
  onToggleShortcuts,
  onDirtyChange,
  onDecisionSaved,
  onNavigateStore,
  onExitWorkspace,
  saveActionRef,
}: WorkspaceStoreBodyProps) {
  const { projectId } = useProject()
  const toast = useToast()
  const { decide, reanalyze, supplement } = useStoreActions(batchId, detail.store_id)
  const decision = normalizeDecision(detail.decision)
  const canEdit = batchStatus === 'completed'
  const storeName = formatStore(detail.source_name).name

  const initial = useMemo(() => findInitialImageAndRecommendation(detail), [detail])
  const [imageIndex, setImageIndex] = useState(initial.imageIndex)
  // 待审核且有推荐候选时默认预选推荐候选（绝不自动提交）。
  const [candidateIndex, setCandidateIndex] = useState<number | null>(initial.recommendedCandidateIndex)
  const [userModifiedSelection, setUserModifiedSelection] = useState(false)
  const [manualMode, setManualMode] = useState(false)
  const [manualBox, setManualBox] = useState<Box | null>(null)
  const [rotationDeg, setRotationDeg] = useState(0)
  const [activeImgEl, setActiveImgEl] = useState<HTMLImageElement | null>(null)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [showLowQuality, setShowLowQuality] = useState(false)
  const [showSupplement, setShowSupplement] = useState(false)
  const [showDetails, setShowDetails] = useState(false)

  const [rejectOpen, setRejectOpen] = useState(false)
  const [rejectReason, setRejectReason] = useState('blur')
  const [rejectDetail, setRejectDetail] = useState('')
  const [rejectError, setRejectError] = useState('')

  const [files, setFiles] = useState<File[]>([])
  const [uploadMode, setUploadMode] = useState<UploadMode>('append')
  const [uploadBusy, setUploadBusy] = useState(false)
  const [uploadProgress, setUploadProgress] = useState('')
  const [uploadError, setUploadError] = useState('')
  const [decisionSavedLocally, setDecisionSavedLocally] = useState(false)

  const cropCanvasRef = useRef<HTMLCanvasElement>(null)
  const submittingRef = useRef(false)

  const currentImage = detail.images[imageIndex] ?? null
  const allCandidates = currentImage?.candidates ?? []
  const normalCandidates = useMemo(
    () => allCandidates.filter((item) => item.quality !== 'rejected'),
    [allCandidates],
  )
  const lowQualityCandidates = useMemo(
    () => allCandidates.filter((item) => item.quality === 'rejected'),
    [allCandidates],
  )
  const visibleCandidates = showLowQuality ? allCandidates : normalCandidates

  const savedCandidateIndex =
    decision === 'confirmed' && detail.selected?.image_index === imageIndex
      ? detail.selected.candidate_index
      : null

  const detectorError = useMemo(
    () => getDetectorErrorMessage(detail, currentImage),
    [currentImage, detail],
  )

  // 更新手动框选的实时裁剪预览 canvas。
  useEffect(() => {
    const canvas = cropCanvasRef.current
    if (!canvas || !manualMode || !isUsableBox(manualBox) || !activeImgEl) return
    drawCroppedPreview(canvas, activeImgEl, manualBox, rotationDeg)
  }, [activeImgEl, manualBox, manualMode, rotationDeg])

  const selectImage = useCallback(
    (nextIndex: number) => {
      if (nextIndex < 0 || nextIndex >= detail.images.length) return
      setImageIndex(nextIndex)
      const nextImg = detail.images[nextIndex]
      const rec = nextImg?.candidates.find((c) => c.recommendation && c.quality !== 'rejected')
      setCandidateIndex(decision === 'pending' ? (rec?.candidate_index ?? null) : null)
      setManualMode(false)
      setManualBox(null)
    },
    [decision, detail.images],
  )

  const selectCandidate = useCallback((candidate: QrCandidate) => {
    setCandidateIndex(candidate.candidate_index)
    setUserModifiedSelection(true)
    setManualMode(false)
    setManualBox(null)
  }, [])

  const toggleManualMode = useCallback(() => {
    if (!canEdit || decision !== 'pending') return
    setManualMode((prev) => {
      const next = !prev
      if (!next) setManualBox(null)
      return next
    })
  }, [canEdit, decision])

  const confirmSelection = useCallback(() => {
    if (!canEdit || decision !== 'pending' || decide.isPending || submittingRef.current) return
    const targetStoreId = detail.store_id

    if (manualMode) {
      if (!isUsableBox(manualBox)) return
      submittingRef.current = true
      decide.mutate(
        { decision: 'confirm', image_index: imageIndex, box: manualBox },
        {
          onSuccess: () => {
            submittingRef.current = false
            toast.push('已按人工框选确认二维码', 'success')
            setDecisionSavedLocally(true)
            onDirtyChange(false)
            onDecisionSaved(targetStoreId)
          },
          onError: (cause) => {
            submittingRef.current = false
            toast.push(errorMessage(cause), 'danger')
          },
        },
      )
      return
    }

    if (candidateIndex === null) return
    submittingRef.current = true
    decide.mutate(
      { decision: 'confirm', image_index: imageIndex, candidate_index: candidateIndex },
      {
        onSuccess: () => {
          submittingRef.current = false
          toast.push('已确认所选二维码', 'success')
          setDecisionSavedLocally(true)
          onDirtyChange(false)
          onDecisionSaved(targetStoreId)
        },
        onError: (cause) => {
          submittingRef.current = false
          toast.push(errorMessage(cause), 'danger')
        },
      },
    )
  }, [canEdit, candidateIndex, decide, decision, detail.store_id, imageIndex, manualBox, manualMode, onDecisionSaved, onDirtyChange, toast])

  const canConfirmNow =
    canEdit &&
    decision === 'pending' &&
    !decide.isPending &&
    (manualMode ? isUsableBox(manualBox) : candidateIndex !== null)

  saveActionRef.current =
    canConfirmNow && !rejectOpen && files.length === 0 && !rejectDetail.trim() ? confirmSelection : null

  // 仅在用户主动框选、修改候选、选择待上传文件或填写不合格说明时标记脏状态；自动预选推荐候选不阻塞切换门店。
  const draftDirty =
    !decisionSavedLocally &&
    decision === 'pending' &&
    (isUsableBox(manualBox) ||
      userModifiedSelection ||
      files.length > 0 ||
      rejectDetail.trim().length > 0 ||
      (rejectOpen && rejectReason !== 'blur'))

  useEffect(() => onDirtyChange(draftDirty), [draftDirty, onDirtyChange])

  const resetDecision = () => {
    if (!canEdit || decide.isPending) return
    decide.mutate(
      { decision: 'pending' },
      {
        onSuccess: () => {
          setCandidateIndex(null)
          setManualBox(null)
          setManualMode(false)
          setUserModifiedSelection(false)
          setDecisionSavedLocally(false)
          toast.push('已恢复为待审核', 'success')
        },
        onError: (cause) => toast.push(errorMessage(cause), 'danger'),
      },
    )
  }

  const submitReject = () => {
    if (decide.isPending || submittingRef.current) return
    if (!rejectReason) {
      setRejectError('请选择不合格原因')
      return
    }
    if (rejectReason === 'other' && !rejectDetail.trim()) {
      setRejectError('选择其他原因时必须填写说明')
      return
    }
    const targetStoreId = detail.store_id
    submittingRef.current = true
    decide.mutate(
      { decision: 'skip', reject_reason: rejectReason, reject_detail: rejectDetail.trim() },
      {
        onSuccess: () => {
          submittingRef.current = false
          setRejectOpen(false)
          setRejectError('')
          setRejectDetail('')
          toast.push('已标记为不合格', 'success')
          setDecisionSavedLocally(true)
          onDirtyChange(false)
          onDecisionSaved(targetStoreId)
        },
        onError: (cause) => {
          submittingRef.current = false
          setRejectError(errorMessage(cause))
        },
      },
    )
  }

  const runReanalyze = () => {
    if (reanalyze.isPending) return
    reanalyze.mutate(undefined, {
      onSuccess: () => toast.push('已重新分析本店', 'success'),
      onError: (cause) => toast.push(errorMessage(cause), 'danger'),
    })
  }

  const onFilesPicked = (event: ChangeEvent<HTMLInputElement>) => {
    setFiles(Array.from(event.target.files ?? []))
    setUploadError('')
  }

  const submitSupplement = async () => {
    if (!files.length) {
      setUploadError('请选择要补充的图片')
      return
    }
    if (files.length > MAX_STORE_UPLOAD_FILES) {
      setUploadError(`单店最多补充 ${MAX_STORE_UPLOAD_FILES} 张图片`)
      return
    }
    setUploadBusy(true)
    setUploadError('')
    try {
      const result = await supplement(files, uploadMode, (done, total) => setUploadProgress(`${done}/${total}`))
      setFiles([])
      setUploadProgress('')
      toast.push(
        uploadMode === 'replace'
          ? `已替换为 ${result.uploaded} 张图片，请重新分析本店`
          : `已补充 ${result.uploaded} 张图片，请重新分析本店`,
        'success',
      )
    } catch (cause) {
      setUploadError(errorMessage(cause))
    } finally {
      setUploadBusy(false)
    }
  }

  // 全局高频审核快捷键（在输入框、弹窗、全屏预览中自动禁用）。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      const target = event.target as HTMLElement | null
      if (
        target &&
        (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) || target.isContentEditable)
      ) {
        return
      }
      if (rejectOpen || previewOpen || guardOpen) return

      if (event.key === '?') {
        event.preventDefault()
        onToggleShortcuts()
        return
      }
      if (shortcutsOpen) return

      const lower = event.key.toLowerCase()
      if ((lower === 'j' || event.key === ']') && hasNext) {
        event.preventDefault()
        onNavigateStore(1)
      } else if ((lower === 'k' || event.key === '[') && hasPrev) {
        event.preventDefault()
        onNavigateStore(-1)
      } else if (event.key === 'ArrowLeft' && detail.images.length > 1) {
        event.preventDefault()
        selectImage((imageIndex - 1 + detail.images.length) % detail.images.length)
      } else if (event.key === 'ArrowRight' && detail.images.length > 1) {
        event.preventDefault()
        selectImage((imageIndex + 1) % detail.images.length)
      } else if (/^[1-9]$/.test(event.key) && canEdit && decision === 'pending') {
        const idx = Number(event.key) - 1
        const candidate = visibleCandidates[idx]
        if (candidate) {
          event.preventDefault()
          selectCandidate(candidate)
        }
      } else if (event.key === 'Enter' && canConfirmNow) {
        // 焦点位于普通按钮上时由浏览器原生点击触发，避免重复提交。
        if (target && target.tagName === 'BUTTON') return
        event.preventDefault()
        confirmSelection()
      } else if (lower === 'm' && canEdit && decision === 'pending' && currentImage) {
        event.preventDefault()
        toggleManualMode()
      } else if (lower === 'r' && canEdit && decision !== 'skipped') {
        event.preventDefault()
        setRejectError('')
        setRejectOpen(true)
      } else if (event.key === 'Escape') {
        event.preventDefault()
        if (manualMode) {
          setManualMode(false)
          setManualBox(null)
        } else {
          onExitWorkspace()
        }
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [
    canConfirmNow,
    canEdit,
    confirmSelection,
    currentImage,
    decision,
    detail.images.length,
    guardOpen,
    hasNext,
    hasPrev,
    imageIndex,
    manualMode,
    onExitWorkspace,
    onNavigateStore,
    onToggleShortcuts,
    previewOpen,
    rejectOpen,
    selectCandidate,
    selectImage,
    shortcutsOpen,
    toggleManualMode,
    visibleCandidates,
  ])

  const getImageUrl = useCallback(
    (idx: number) => storeImageUrl(batchId, detail.store_id, idx, projectId),
    [batchId, detail.store_id, projectId],
  )

  return (
    <>
      {/* 中栏：原图审核画布 */}
      <ReviewImageCanvas
        images={detail.images}
        imageIndex={imageIndex}
        onSelectImage={selectImage}
        getImageUrl={getImageUrl}
        storeName={storeName}
        visibleCandidates={visibleCandidates}
        selectedCandidateIndex={candidateIndex}
        savedCandidateIndex={savedCandidateIndex}
        canEdit={canEdit && decision === 'pending'}
        onSelectCandidate={(candidate) => {
          if (!canEdit || decision !== 'pending') return
          selectCandidate(candidate)
        }}
        manualMode={manualMode}
        manualBox={manualBox}
        onManualBoxChange={setManualBox}
        onToggleManualMode={toggleManualMode}
        onOpenFullscreen={() => setPreviewOpen(true)}
        onRotationChange={setRotationDeg}
        onImageElementChange={setActiveImgEl}
      />

      {/* 右栏：候选区与固定底部操作区 */}
      <aside className="flex w-96 shrink-0 flex-col border-l border-subtle bg-surface" aria-label="候选二维码与审核操作">
        <div className="min-h-0 flex-1 space-y-3.5 overflow-y-auto p-3.5">
          {/* 检测系统异常醒目提示（与正常无候选明确区分） */}
          {detectorError ? (
            <Alert
              color="danger"
              variant="soft"
              title="检测服务异常"
              description={detectorError}
            />
          ) : null}

          {/* 已确认 / 不合格状态摘要 */}
          {decision === 'confirmed' && detail.selected ? (
            <div className="flex items-center gap-3 rounded-xl border border-subtle bg-surface-secondary p-3">
              {detail.selected.candidate_index !== null ? (
                <img
                  src={storeSelectedUrl(batchId, detail.store_id, projectId)}
                  alt="已确认二维码"
                  className="h-16 w-16 shrink-0 rounded-lg border border-subtle bg-surface object-contain"
                />
              ) : null}
              <div className="min-w-0 text-xs text-secondary">
                <p className="font-medium text-default">
                  已确认：第 {detail.selected.image_index + 1} 张
                  {detail.selected.candidate_index !== null
                    ? ` · 候选 ${detail.selected.candidate_index + 1}`
                    : ' · 人工框选'}
                </p>
                {detail.reason ? <p className="mt-0.5 truncate text-2xs text-tertiary">{detail.reason}</p> : null}
              </div>
            </div>
          ) : null}

          {decision === 'skipped' && detail.rejection ? (
            <div className="rounded-xl border border-subtle bg-surface-secondary p-3 text-xs">
              <p className="font-medium text-danger">不合格：{detail.rejection.label || detail.rejection.code}</p>
              {detail.rejection.detail ? <p className="mt-1 text-secondary">{detail.rejection.detail}</p> : null}
            </div>
          ) : null}

          {/* 手动框选实时裁剪预览 */}
          {manualMode ? (
            <section className="space-y-2 rounded-xl border border-primary-solid bg-secondary-soft p-3" aria-label="人工框选实时预览">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-medium text-default">人工框选预览</span>
                {manualBox ? (
                  <Button color="secondary" variant="ghost" size="sm" onClick={() => setManualBox(null)}>
                    重画
                  </Button>
                ) : null}
              </div>
              {isUsableBox(manualBox) ? (
                <div className="flex items-center gap-3">
                  <canvas
                    ref={cropCanvasRef}
                    className="h-24 w-24 shrink-0 rounded-lg border border-subtle bg-surface"
                  />
                  <div className="min-w-0 space-y-1 text-2xs text-secondary">
                    <p className="font-medium text-default">已框选区域（可拖动或拉动四角微调）</p>
                    <p className="font-mono">
                      {Math.round(manualBox.width * 100)}% × {Math.round(manualBox.height * 100)}%
                    </p>
                    <p>按 Enter 或点击下方「确认人工框选」提交</p>
                  </div>
                </div>
              ) : (
                <p className="text-xs text-secondary">请在中间原图上按住左键拖拽框出二维码区域。</p>
              )}
            </section>
          ) : null}

          {/* 候选列表 */}
          <section className="space-y-2" aria-label="二维码候选列表">
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-xs font-semibold text-default">
                二维码候选（{normalCandidates.length}）
              </h2>
              {normalCandidates.length > 0 && decision === 'pending' ? (
                <span className="text-2xs text-tertiary">数字键 1–{Math.min(9, visibleCandidates.length)} 切换</span>
              ) : null}
            </div>

            {visibleCandidates.length === 0 ? (
              <div className="space-y-2.5 rounded-xl border border-subtle bg-surface-secondary p-3.5 text-xs">
                <p className="font-medium text-default">
                  {detectorError ? '因检测异常未生成候选' : '当前原图未检出可用候选'}
                </p>
                <p className="text-secondary">
                  {detail.images.length > 1
                    ? '可按 ←/→ 切换其他原图，或直接在原图上手动框选二维码。'
                    : '可直接在原图上手动框选二维码，或标记为不合格。'}
                </p>
                {canEdit && decision === 'pending' && currentImage ? (
                  <Button color="primary" variant="outline" size="sm" onClick={toggleManualMode}>
                    {manualMode ? '已在框选模式' : '开始手动框选 (M)'}
                  </Button>
                ) : null}
              </div>
            ) : (
              <div className="space-y-2">
                {visibleCandidates.map((candidate, listIdx) => {
                  const active = !manualMode && candidate.candidate_index === candidateIndex
                  const isSaved = savedCandidateIndex === candidate.candidate_index
                  const shortcutNum = listIdx < 9 ? listIdx + 1 : null
                  return (
                    <button
                      key={candidate.candidate_index}
                      type="button"
                      onClick={() => {
                        if (!canEdit || decision !== 'pending') return
                        selectCandidate(candidate)
                      }}
                      aria-pressed={active}
                      className={[
                        'flex w-full gap-3 rounded-xl border p-2.5 text-left transition-colors',
                        active || isSaved
                          ? 'border-primary-solid bg-secondary-soft ring-1 ring-primary-solid/30'
                          : 'border-subtle hover:bg-secondary-ghost-hover',
                        canEdit && decision === 'pending' ? 'cursor-pointer' : 'cursor-default',
                      ].join(' ')}
                    >
                      <div className="relative h-20 w-20 shrink-0 overflow-hidden rounded-lg border border-subtle bg-surface">
                        <img
                          src={storeCandidateUrl(batchId, detail.store_id, imageIndex, candidate.candidate_index, projectId)}
                          alt={`候选二维码 ${candidate.candidate_index + 1}`}
                          className="h-full w-full object-contain"
                          loading="lazy"
                        />
                        {shortcutNum ? (
                          <span className="absolute top-1 left-1 rounded bg-surface/90 px-1 font-mono text-2xs text-default shadow-sm">
                            {shortcutNum}
                          </span>
                        ) : null}
                      </div>
                      <div className="min-w-0 flex-1 space-y-1">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="text-xs font-medium text-default">候选 {candidate.candidate_index + 1}</span>
                          {candidate.recommendation ? <StatusBadge tone="info">推荐</StatusBadge> : null}
                          {candidate.quality === 'rejected' ? <StatusBadge tone="danger">低质</StatusBadge> : null}
                          {active && candidate.recommendation && decision === 'pending' ? (
                            <span className="text-2xs text-tertiary">已预选</span>
                          ) : null}
                        </div>
                        <p className="text-xs text-secondary">
                          {PURPOSE_LABELS[candidate.purpose] ?? candidate.purpose}
                          {candidate.purpose_source
                            ? ` · ${PURPOSE_SOURCE_LABELS[candidate.purpose_source] ?? candidate.purpose_source}`
                            : ''}
                        </p>
                        {candidate.purpose_reason ? (
                          <p className="line-clamp-2 text-2xs text-tertiary" title={candidate.purpose_reason}>
                            {candidate.purpose_reason}
                          </p>
                        ) : null}
                        {candidate.quality_reason ? (
                          <p className="text-2xs text-danger">{candidate.quality_reason}</p>
                        ) : null}
                      </div>
                    </button>
                  )
                })}
              </div>
            )}
          </section>

          {/* 折叠区 1：低质量候选 */}
          {lowQualityCandidates.length > 0 ? (
            <div className="rounded-xl border border-subtle">
              <button
                type="button"
                onClick={() => setShowLowQuality((prev) => !prev)}
                className="flex w-full items-center justify-between px-3 py-2 text-xs font-medium text-secondary hover:text-default"
              >
                <span>低质量候选（{lowQualityCandidates.length}）</span>
                <span>{showLowQuality ? '隐藏' : '展开显示'}</span>
              </button>
            </div>
          ) : null}

          {/* 折叠区 2：补图 / 替换图片 */}
          <div className="rounded-xl border border-subtle">
            <button
              type="button"
              onClick={() => setShowSupplement((prev) => !prev)}
              className="flex w-full items-center justify-between px-3 py-2 text-xs font-medium text-secondary hover:text-default"
            >
              <span>补图 / 替换图片</span>
              <span>{showSupplement ? '收起' : '展开'}</span>
            </button>
            {showSupplement ? (
              <div className="space-y-2.5 border-t border-subtle p-3">
                <p className="text-2xs text-tertiary">追加保留现有图片；替换会清空现有图片与结论，需重新分析。</p>
                <input
                  type="file"
                  multiple
                  accept=".jpg,.jpeg,.png,.webp"
                  onChange={onFilesPicked}
                  className="block w-full text-xs text-secondary file:mr-2 file:rounded-md file:border file:border-subtle file:bg-surface file:px-2.5 file:py-1 file:text-xs file:text-default"
                  disabled={uploadBusy}
                />
                {files.length > 0 ? (
                  <p className="text-2xs text-secondary">
                    已选 {files.length} 个文件 · {formatBytes(files.reduce((sum, file) => sum + file.size, 0))}
                  </p>
                ) : null}
                <div className="flex items-center gap-2">
                  <Select
                    aria-label="补图方式"
                    value={uploadMode}
                    options={[
                      { value: 'append', label: '追加图片' },
                      { value: 'replace', label: '替换全部图片' },
                    ]}
                    onChange={(option) => setUploadMode(option.value as UploadMode)}
                    triggerClassName="w-36"
                  />
                  <Button color="primary" size="sm" onClick={() => void submitSupplement()} disabled={uploadBusy || !files.length}>
                    {uploadBusy ? `上传中 ${uploadProgress}` : '上传保存'}
                  </Button>
                </div>
                {uploadError ? <Alert color="danger" variant="soft" title={uploadError} /> : null}
              </div>
            ) : null}
          </div>

          {/* 折叠区 3：详细检测信息 */}
          <div className="rounded-xl border border-subtle">
            <button
              type="button"
              onClick={() => setShowDetails((prev) => !prev)}
              className="flex w-full items-center justify-between px-3 py-2 text-xs font-medium text-secondary hover:text-default"
            >
              <span>详细检测信息</span>
              <span>{showDetails ? '收起' : '展开'}</span>
            </button>
            {showDetails ? (
              <dl className="space-y-1.5 border-t border-subtle p-3 text-2xs">
                <div className="flex justify-between gap-2">
                  <dt className="text-tertiary">推荐等级</dt>
                  <dd className="text-default">
                    {RECOMMENDATION_LEVEL_LABELS[detail.recommendation_level] ?? detail.recommendation_level}
                  </dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-tertiary">小程序校验</dt>
                  <dd className="text-default">
                    {WECHAT_VALIDATION_LABELS[detail.wechat_validation_status] ?? detail.wechat_validation_status}
                  </dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-tertiary">分析状态</dt>
                  <dd className="text-default">{analysisStatusLabel(detail.analysis_status)}</dd>
                </div>
                {detail.reason ? (
                  <div>
                    <dt className="text-tertiary">分析说明</dt>
                    <dd className="mt-0.5 text-secondary">{detail.reason}</dd>
                  </div>
                ) : null}
                {detail.source_folder ? (
                  <div>
                    <dt className="text-tertiary">来源目录</dt>
                    <dd className="mt-0.5 break-all font-mono text-secondary">{detail.source_folder}</dd>
                  </div>
                ) : null}
              </dl>
            ) : null}
          </div>
        </div>

        {/* 固定底部操作栏 */}
        <footer className="shrink-0 space-y-2 border-t border-subtle bg-surface p-3" aria-label="审核操作栏">
          {!canEdit ? (
            <p className="text-2xs text-tertiary">批次分析未完成，暂不能提交审核决策</p>
          ) : null}
          <div className="flex items-center gap-2">
            {decision === 'pending' ? (
              <Button
                color="primary"
                size="sm"
                className="flex-1"
                disabled={!canConfirmNow}
                onClick={confirmSelection}
              >
                {decide.isPending ? '保存中…' : manualMode ? '确认人工框选 (Enter)' : '确认选码 (Enter)'}
              </Button>
            ) : (
              <Button
                color="secondary"
                variant="outline"
                size="sm"
                className="flex-1"
                disabled={!canEdit || decide.isPending}
                onClick={resetDecision}
              >
                恢复待审核
              </Button>
            )}
            {decision === 'pending' && currentImage ? (
              <Button
                color={manualMode ? 'primary' : 'secondary'}
                variant={manualMode ? 'soft' : 'outline'}
                size="sm"
                disabled={!canEdit}
                onClick={toggleManualMode}
              >
                {manualMode ? '退出框选' : '手动框选'}
              </Button>
            ) : null}
          </div>
          <div className="flex items-center gap-2">
            {decision !== 'skipped' ? (
              <Button
                color="danger"
                variant="outline"
                size="sm"
                className="flex-1"
                disabled={!canEdit || decide.isPending}
                onClick={() => {
                  setRejectError('')
                  setRejectOpen(true)
                }}
              >
                标记不合格 (R)
              </Button>
            ) : null}
            <Button
              color="secondary"
              variant="ghost"
              size="sm"
              className={decision === 'skipped' ? 'flex-1' : ''}
              disabled={reanalyze.isPending}
              onClick={runReanalyze}
            >
              {reanalyze.isPending ? '重分析中…' : '重新分析'}
            </Button>
          </div>
        </footer>
      </aside>

      {/* 标记不合格弹窗 */}
      <AppDialog
        open={rejectOpen}
        onOpenChange={(next) => {
          if (!next && !decide.isPending) {
            setRejectOpen(false)
            setRejectError('')
          }
        }}
        title={`标记不合格：${storeName}`}
        widthClass="w-[min(520px,94vw)]"
        footer={
          <div className="flex justify-end gap-2">
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              onClick={() => setRejectOpen(false)}
              disabled={decide.isPending}
            >
              取消
            </Button>
            <Button color="danger" size="sm" onClick={submitReject} disabled={decide.isPending}>
              {decide.isPending ? '正在保存…' : '确认标记'}
            </Button>
          </div>
        }
      >
        <div className="space-y-3">
          <label className="block text-sm text-default" htmlFor="workspace-reject-reason">
            不合格原因
          </label>
          <Select
            id="workspace-reject-reason"
            aria-label="不合格原因"
            value={rejectReason}
            options={REJECTION_REASONS.map((reason) => ({ value: reason.code, label: reason.label }))}
            onChange={(option) => {
              setRejectReason(option.value)
              setRejectError('')
            }}
          />
          <Input
            aria-label="不合格说明"
            value={rejectDetail}
            maxLength={200}
            placeholder={rejectReason === 'other' ? '请填写其他原因（必填）' : '补充说明（可选，最多 200 字）'}
            onChange={(event) => setRejectDetail(event.target.value)}
          />
          {rejectError ? <Alert color="danger" variant="soft" title={rejectError} /> : null}
        </div>
      </AppDialog>

      {/* 全屏图片查看器 */}
      <ImagePreview
        open={previewOpen}
        src={currentImage ? getImageUrl(imageIndex) : ''}
        alt={`${storeName} · 原图 ${imageIndex + 1}`}
        caption={`${storeName} · 原图 ${imageIndex + 1}`}
        items={detail.images.map((_, idx) => ({
          src: getImageUrl(idx),
          alt: `${storeName} · 原图 ${idx + 1}`,
        }))}
        initialIndex={imageIndex}
        onOpenChange={setPreviewOpen}
      />
    </>
  )
}
