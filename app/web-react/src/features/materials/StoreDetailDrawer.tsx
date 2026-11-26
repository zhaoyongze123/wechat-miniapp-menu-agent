import { useEffect, useRef, useState, type ChangeEvent, type MutableRefObject } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDialog } from '../../components/AppDialog'
import { AppDrawer } from '../../components/AppDrawer'
import { useUnsavedChanges } from '../../components/UnsavedChangesProvider'
import { ImagePreview } from '../../components/ImagePreview'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { storeCandidateUrl, storeImageUrl, storeSelectedUrl } from './api'
import {
  MAX_STORE_UPLOAD_FILES,
  PURPOSE_LABELS,
  PURPOSE_SOURCE_LABELS,
  RECOMMENDATION_LEVEL_LABELS,
  REJECTION_REASONS,
  WECHAT_VALIDATION_LABELS,
  decisionMeta,
  formatBytes,
  normalizeDecision,
} from './format'
import { useStore, useStoreActions } from './hooks'
import { useProject } from '../projects/ProjectProvider'
import { ManualQrCanvas } from './ManualQrCanvas'
import type { Box, MaterialStoreDetail, MaterialStoreSummary, QrCandidate, UploadMode } from './types'
import { formatStore } from '../hub/format'

interface StoreDetailDrawerProps {
  open: boolean
  batchId: string
  batchStatus: string
  store: MaterialStoreSummary | null
  onOpenChange: (open: boolean) => void
  onNavigate: (delta: -1 | 1) => void
  positionLabel: string
  hasPrev: boolean
  hasNext: boolean
  onDecisionSaved: (storeId: string) => void
}

export function StoreDetailDrawer({
  open,
  batchId,
  batchStatus,
  store,
  onOpenChange,
  onNavigate,
  positionLabel,
  hasPrev,
  hasNext,
  onDecisionSaved,
}: StoreDetailDrawerProps) {
  const [hasDraft, setHasDraft] = useState(false)
  const [guardOpen, setGuardOpen] = useState(false)
  useUnsavedChanges(open && hasDraft, '门店选码或补图尚未保存，切换项目后这些修改将丢失。')
  const pendingAction = useRef<(() => void) | null>(null)
  const saveBeforeLeave = useRef(false)
  const saveSelection = useRef<(() => void) | null>(null)
  const query = useStore(batchId, store?.store_id ?? '')
  const title = store ? formatStore(store.source_name).name : '门店详情'
  const decision = decisionMeta(store?.decision)

  useEffect(() => {
    if (!hasDraft) return
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => window.removeEventListener('beforeunload', warnBeforeUnload)
  }, [hasDraft])

  const performOrGuard = (action: () => void) => {
    if (!hasDraft) {
      action()
      return
    }
    pendingAction.current = action
    setGuardOpen(true)
  }

  const handleSaved = (storeId: string) => {
    setHasDraft(false)
    if (saveBeforeLeave.current) {
      saveBeforeLeave.current = false
      setGuardOpen(false)
      const action = pendingAction.current
      pendingAction.current = null
      action?.()
      return
    }
    onDecisionSaved(storeId)
  }

  const cancelGuard = () => {
    saveBeforeLeave.current = false
    pendingAction.current = null
    setGuardOpen(false)
  }

  return (
    <>
      <AppDrawer
        open={open}
        onOpenChange={(next) => {
          if (!next) performOrGuard(() => onOpenChange(false))
          else onOpenChange(true)
        }}
        title={title}
        description={store ? `${store.source_folder || store.source_name} · ${positionLabel}` : '选择门店查看详情'}
        headerActions={<StatusBadge tone={decision.tone}>{decision.label}</StatusBadge>}
        footer={
          <div className="flex items-center justify-between">
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              disabled={!hasPrev}
              onClick={() => performOrGuard(() => onNavigate(-1))}
            >
              上一条
            </Button>
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              disabled={!hasNext}
              onClick={() => performOrGuard(() => onNavigate(1))}
            >
              下一条
            </Button>
          </div>
        }
      >
        {!store ? (
          <StatePanel variant="empty" title="请选择门店" description="从左侧门店列表选择一家门店查看选码详情。" />
        ) : query.isLoading ? (
          <StatePanel variant="loading" title="正在读取门店素材…" />
        ) : query.isError ? (
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
        ) : query.data ? (
          <StoreDetailBody
            key={query.data.store_id}
            batchId={batchId}
            batchStatus={batchStatus}
            detail={query.data}
            onDirtyChange={setHasDraft}
            onDecisionSaved={handleSaved}
            saveActionRef={saveSelection}
          />
        ) : null}
      </AppDrawer>
      <AppDialog
        open={guardOpen}
        title="修改尚未保存"
        description="离开当前门店会丢弃尚未保存的选码、补图或不合格说明。"
        onOpenChange={(next) => { if (!next) cancelGuard() }}
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
                const action = pendingAction.current
                pendingAction.current = null
                action?.()
              }}
            >
              放弃修改并离开
            </Button>
            {saveSelection.current ? (
              <Button
                color="primary"
                size="sm"
                onClick={() => {
                  saveBeforeLeave.current = true
                  saveSelection.current?.()
                }}
              >
                确认选码并离开
              </Button>
            ) : null}
          </div>
        }
      >
        <p className="text-sm text-secondary">确认选码会提交到共享后台；只有服务端保存成功后才会继续离开。</p>
      </AppDialog>
    </>
  )
}

interface StoreDetailBodyProps {
  batchId: string
  batchStatus: string
  detail: MaterialStoreDetail
  onDirtyChange: (dirty: boolean) => void
  onDecisionSaved: (storeId: string) => void
  saveActionRef: MutableRefObject<(() => void) | null>
}

function StoreDetailBody({ batchId, batchStatus, detail, onDirtyChange, onDecisionSaved, saveActionRef }: StoreDetailBodyProps) {
  const { projectId } = useProject()
  const toast = useToast()
  const { decide, reanalyze, supplement } = useStoreActions(batchId, detail.store_id)
  const decision = normalizeDecision(detail.decision)
  const decisionBadge = decisionMeta(detail.decision)
  const canEdit = batchStatus === 'completed'

  const initialImageIndex = (() => {
    if (detail.selected?.image_index !== undefined) return detail.selected.image_index
    const index = detail.images.findIndex((image) => image.candidates.length > 0)
    return index >= 0 ? index : 0
  })()

  const [imageIndex, setImageIndex] = useState(initialImageIndex)
  const [previewImageIndex, setPreviewImageIndex] = useState<number | null>(null)
  const [candidateIndex, setCandidateIndex] = useState<number | null>(detail.selected?.candidate_index ?? null)
  const [manualMode, setManualMode] = useState(false)
  const [manualBox, setManualBox] = useState<Box | null>(null)
  const [showRejected, setShowRejected] = useState(false)
  const [rejectOpen, setRejectOpen] = useState(false)
  const [rejectReason, setRejectReason] = useState('blur')
  const [rejectDetail, setRejectDetail] = useState('')
  const [rejectError, setRejectError] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [decisionSavedLocally, setDecisionSavedLocally] = useState(false)
  const [uploadMode, setUploadMode] = useState<UploadMode>('append')
  const [uploadBusy, setUploadBusy] = useState(false)
  const [uploadProgress, setUploadProgress] = useState('')
  const [uploadError, setUploadError] = useState('')

  const image = detail.images[imageIndex]
  const candidates = image?.candidates ?? []
  const visibleCandidates = showRejected ? candidates : candidates.filter((item) => item.quality !== 'rejected')
  const selectedCandidate =
    decision === 'confirmed' && detail.selected?.image_index === imageIndex
      ? candidates.find((item) => item.candidate_index === detail.selected?.candidate_index) ?? null
      : null

  const selectImage = (index: number) => {
    setImageIndex(index)
    setCandidateIndex(null)
    setManualMode(false)
    setManualBox(null)
  }

  const selectCandidate = (candidate: QrCandidate) => {
    setCandidateIndex(candidate.candidate_index)
    setManualMode(false)
    setManualBox(null)
  }

  const confirmSelection = () => {
    if (!canEdit || decision !== 'pending') return
    if (manualMode && manualBox) {
      decide.mutate(
        { decision: 'confirm', image_index: imageIndex, box: manualBox },
        {
          onSuccess: () => {
            toast.push('已按人工框选确认二维码', 'success')
            setDecisionSavedLocally(true)
            onDirtyChange(false)
            onDecisionSaved(detail.store_id)
          },
          onError: (cause) => toast.push(errorMessage(cause), 'danger'),
        },
      )
      return
    }
    if (candidateIndex === null) return
    decide.mutate(
      { decision: 'confirm', image_index: imageIndex, candidate_index: candidateIndex },
      {
        onSuccess: () => {
          toast.push('已确认所选二维码', 'success')
          setDecisionSavedLocally(true)
          onDirtyChange(false)
          onDecisionSaved(detail.store_id)
        },
        onError: (cause) => toast.push(errorMessage(cause), 'danger'),
      },
    )
  }

  const canSaveSelection = canEdit && decision === 'pending' && !rejectOpen && files.length === 0 &&
    !rejectDetail.trim() && (manualMode ? manualBox !== null : candidateIndex !== null)
  saveActionRef.current = canSaveSelection
    ? confirmSelection
    : null
  const draftDirty = !decisionSavedLocally && decision === 'pending' && (
    manualBox !== null ||
    (candidateIndex !== null && (
      candidateIndex !== detail.selected?.candidate_index || imageIndex !== detail.selected?.image_index
    )) ||
    files.length > 0 ||
    rejectDetail.trim().length > 0 ||
    (rejectOpen && rejectReason !== 'blur')
  )
  useEffect(() => onDirtyChange(draftDirty), [draftDirty, onDirtyChange])

  const resetDecision = () => {
    decide.mutate(
      { decision: 'pending' },
      {
        onSuccess: () => {
          setCandidateIndex(null)
          setManualBox(null)
          toast.push('已恢复为待审核', 'success')
        },
        onError: (cause) => toast.push(errorMessage(cause), 'danger'),
      },
    )
  }

  const submitReject = () => {
    if (!rejectReason) {
      setRejectError('请选择不合格原因')
      return
    }
    if (rejectReason === 'other' && !rejectDetail.trim()) {
      setRejectError('选择其他原因时必须填写说明')
      return
    }
    decide.mutate(
      { decision: 'skip', reject_reason: rejectReason, reject_detail: rejectDetail.trim() },
      {
        onSuccess: () => {
          setRejectOpen(false)
          setRejectError('')
          setRejectDetail('')
          toast.push('已标记为不合格', 'success')
          setDecisionSavedLocally(true)
          onDirtyChange(false)
          onDecisionSaved(detail.store_id)
        },
        onError: (cause) => setRejectError(errorMessage(cause)),
      },
    )
  }

  const runReanalyze = () => {
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

  return (
    <div className="space-y-6">
      <section className="rounded-xl border border-subtle bg-surface-secondary p-4" aria-label="当前结论">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={decisionBadge.tone}>{decisionBadge.label}</StatusBadge>
          {detail.recommendation ? <StatusBadge tone="info">建议自动确认</StatusBadge> : null}
          <span className="text-xs text-secondary">
            {RECOMMENDATION_LEVEL_LABELS[detail.recommendation_level] ?? detail.recommendation_level}
            {detail.wechat_validation_status
              ? ` · ${WECHAT_VALIDATION_LABELS[detail.wechat_validation_status] ?? detail.wechat_validation_status}`
              : ''}
          </span>
        </div>
        {detail.reason ? <p className="mt-2 text-xs text-secondary">分析说明：{detail.reason}</p> : null}

        {decision === 'confirmed' && detail.selected ? (
          <div className="mt-3 flex items-start gap-3">
            {detail.selected.candidate_index !== null ? (
              <img
                src={storeSelectedUrl(batchId, detail.store_id, projectId)}
                alt="已确认二维码"
                className="h-20 w-20 rounded-lg border border-subtle bg-surface object-contain"
              />
            ) : null}
            <div className="text-xs text-secondary">
              <p className="text-sm text-default">
                图片来源：第 {detail.selected.image_index + 1} 张
                {detail.selected.candidate_index !== null
                  ? ` · 候选 ${detail.selected.candidate_index + 1}`
                  : ' · 人工框选'}
              </p>
              {detail.selected.box ? (
                <p className="mt-1 font-mono text-2xs">
                  box {Math.round(detail.selected.box.x)},{Math.round(detail.selected.box.y)} ·{' '}
                  {Math.round(detail.selected.box.width)}×{Math.round(detail.selected.box.height)}
                </p>
              ) : null}
            </div>
          </div>
        ) : null}

        {decision === 'skipped' && detail.rejection ? (
          <div className="mt-3 rounded-lg border border-subtle bg-surface p-3 text-sm">
            <p className="text-default">{detail.rejection.label || detail.rejection.code}</p>
            {detail.rejection.detail ? <p className="mt-1 text-xs text-secondary">{detail.rejection.detail}</p> : null}
          </div>
        ) : null}

        <div className="mt-4 flex flex-wrap items-center gap-2">
          {decision === 'pending' ? (
            <Button
              color="primary"
              size="sm"
              disabled={!canEdit || (manualMode ? !manualBox : candidateIndex === null) || decide.isPending}
              onClick={confirmSelection}
            >
              {decide.isPending ? '正在确认…' : manualMode ? '确认人工框选' : '确认选码'}
            </Button>
          ) : (
            <Button color="secondary" variant="outline" size="sm" onClick={resetDecision} disabled={!canEdit || decide.isPending}>
              恢复待审核
            </Button>
          )}
          {decision !== 'skipped' ? (
            <Button
              color="danger"
              variant="outline"
              size="sm"
              disabled={!canEdit || decide.isPending}
              onClick={() => {
                setRejectError('')
                setRejectOpen(true)
              }}
            >
              标记不合格
            </Button>
          ) : null}
          <Button color="secondary" variant="ghost" size="sm" onClick={runReanalyze} disabled={reanalyze.isPending}>
            {reanalyze.isPending ? '正在重新分析…' : '重新分析本店'}
          </Button>
          <span className="text-2xs text-tertiary">
            {canEdit ? '决策仅在分析完成后可修改' : '批次分析未完成，暂不能修改决策'}
          </span>
        </div>
      </section>

      <section className="space-y-3" aria-label="原图与候选">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="heading-xs">原图与候选</h3>
          {image ? (
            <Button
              color={manualMode ? 'primary' : 'secondary'}
              variant={manualMode ? 'solid' : 'outline'}
              size="sm"
              onClick={() => {
                setManualMode((current) => !current)
                setManualBox(null)
                setCandidateIndex(null)
              }}
              disabled={!canEdit || decision !== 'pending'}
            >
              {manualMode ? '退出手动框选' : '手动框选'}
            </Button>
          ) : null}
        </div>

        {detail.images.length === 0 ? (
          <StatePanel variant="empty" title="该门店没有可用图片" description="请通过下方补图上传图片后重新分析。" />
        ) : (
          <>
            <div className="flex flex-wrap gap-2">
              {detail.images.map((item, index) => {
                const active = index === imageIndex
                return (
                  <button
                    key={item.sha256 || index}
                    type="button"
                    onClick={() => selectImage(index)}
                    className={[
                      'rounded-lg border px-3 py-1.5 text-left text-xs transition-colors',
                      active
                        ? 'border-primary-solid bg-secondary-soft text-default'
                        : 'border-subtle text-secondary hover:bg-secondary-ghost-hover',
                    ].join(' ')}
                  >
                    <span className="block">第 {index + 1} 张 · {item.candidates.length} 候选</span>
                    <span className="block text-2xs text-tertiary">
                      {item.possible_missed_code ? '可能漏码' : item.ordering_text_seen ? '可见点餐字样' : '普通截图'}
                    </span>
                  </button>
                )
              })}
            </div>
            {image ? (
              <Button color="secondary" variant="outline" size="sm" onClick={() => setPreviewImageIndex(imageIndex)}>
                查看原图
              </Button>
            ) : null}

            {manualMode && image ? (
              <ManualQrCanvas
                imageUrl={storeImageUrl(batchId, detail.store_id, imageIndex, projectId)}
                box={manualBox}
                onChange={setManualBox}
                disabled={!canEdit || decision !== 'pending'}
              />
            ) : null}

            {visibleCandidates.length === 0 ? (
              <Alert
                color="warning"
                variant="soft"
                title="当前图片没有可用候选"
                description="可切换其他图片，或使用「手动框选」直接圈出二维码。"
              />
            ) : (
              <div className="grid grid-cols-2 gap-3">
                {visibleCandidates.map((candidate) => {
                  const active = candidate.candidate_index === candidateIndex
                  const isSaved = selectedCandidate?.candidate_index === candidate.candidate_index
                  return (
                    <button
                      key={candidate.candidate_index}
                      type="button"
                      onClick={() => {
                        if (!canEdit || decision !== 'pending') return
                        selectCandidate(candidate)
                      }}
                      className={[
                        'flex gap-3 rounded-xl border p-3 text-left transition-colors',
                        active || isSaved
                          ? 'border-primary-solid bg-secondary-soft'
                          : 'border-subtle hover:bg-secondary-ghost-hover',
                        canEdit && decision === 'pending' ? '' : 'cursor-default',
                      ].join(' ')}
                      aria-pressed={active}
                    >
                      <img
                        src={storeCandidateUrl(batchId, detail.store_id, imageIndex, candidate.candidate_index, projectId)}
                        alt={`候选二维码 ${candidate.candidate_index + 1}`}
                        className="h-16 w-16 shrink-0 rounded-lg border border-subtle bg-surface object-contain"
                        loading="lazy"
                      />
                      <span className="min-w-0 space-y-1">
                        <span className="flex flex-wrap items-center gap-1.5">
                          <span className="text-sm text-default">候选 {candidate.candidate_index + 1}</span>
                          {candidate.recommendation ? <StatusBadge tone="info">推荐</StatusBadge> : null}
                          {candidate.quality === 'rejected' ? <StatusBadge tone="danger">低质</StatusBadge> : null}
                        </span>
                        <span className="block text-xs text-secondary">
                          {PURPOSE_LABELS[candidate.purpose] ?? candidate.purpose}
                          {candidate.purpose_source
                            ? ` · ${PURPOSE_SOURCE_LABELS[candidate.purpose_source] ?? candidate.purpose_source}`
                            : ''}
                        </span>
                        {candidate.purpose_reason ? (
                          <span className="block text-2xs text-tertiary">{candidate.purpose_reason}</span>
                        ) : null}
                        {candidate.quality_reason ? (
                          <span className="block text-2xs text-danger">{candidate.quality_reason}</span>
                        ) : null}
                      </span>
                    </button>
                  )
                })}
              </div>
            )}

            <Checkbox
              label="显示低质量候选"
              checked={showRejected}
              onCheckedChange={(next) => setShowRejected(next)}
            />
          </>
        )}
      </section>

      <section className="space-y-3 rounded-xl border border-subtle p-4" aria-label="补图">
        <div>
          <h3 className="heading-xs">补图</h3>
          <p className="mt-1 text-xs text-secondary">
            追加会保留现有图片；替换会清空当前门店的图片与选码结论，需要重新分析。
          </p>
        </div>
        <input
          type="file"
          multiple
          accept=".jpg,.jpeg,.png,.webp"
          onChange={onFilesPicked}
          className="block w-full text-sm text-secondary file:mr-3 file:rounded-md file:border file:border-subtle file:bg-surface file:px-3 file:py-1.5 file:text-sm file:text-default"
          disabled={uploadBusy}
        />
        {files.length ? (
          <p className="text-xs text-secondary">
            已选择 {files.length} 个文件 · {formatBytes(files.reduce((sum, file) => sum + file.size, 0))}
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-3">
          <Select
            aria-label="补图方式"
            value={uploadMode}
            options={[
              { value: 'append', label: '追加图片' },
              { value: 'replace', label: '替换全部图片' },
            ]}
            onChange={(option) => setUploadMode(option.value as UploadMode)}
            triggerClassName="w-40"
          />
          <Button color="primary" size="sm" onClick={() => void submitSupplement()} disabled={uploadBusy || !files.length}>
            {uploadBusy ? `上传中 ${uploadProgress}` : '上传并保存'}
          </Button>
        </div>
        {uploadError ? <Alert color="danger" variant="soft" title={uploadError} /> : null}
      </section>

      <AppDialog
        open={rejectOpen}
        onOpenChange={(next) => {
          if (!next && !decide.isPending) {
            setRejectOpen(false)
            setRejectError('')
          }
        }}
        title={`标记不合格：${formatStore(detail.source_name).name}`}
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
          <label className="block text-sm text-default" htmlFor="reject-reason">
            不合格原因
          </label>
          <Select
            id="reject-reason"
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
      <ImagePreview
        open={previewImageIndex !== null}
        src={previewImageIndex === null ? '' : storeImageUrl(batchId, detail.store_id, previewImageIndex, projectId)}
        alt={previewImageIndex === null ? '' : `${formatStore(detail.source_name).name} · 原图 ${previewImageIndex + 1}`}
        caption={previewImageIndex === null ? '' : `${formatStore(detail.source_name).name} · 原图 ${previewImageIndex + 1}`}
        items={detail.images.map((_, index) => ({
          src: storeImageUrl(batchId, detail.store_id, index, projectId),
          alt: `${formatStore(detail.source_name).name} · 原图 ${index + 1}`,
        }))}
        initialIndex={previewImageIndex ?? 0}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) setPreviewImageIndex(null)
        }}
      />
    </div>
  )
}
