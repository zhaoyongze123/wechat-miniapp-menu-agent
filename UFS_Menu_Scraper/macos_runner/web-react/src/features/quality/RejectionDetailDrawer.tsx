import { useState, type ReactNode } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { AppDrawer } from '../../components/AppDrawer'
import { ImagePreview } from '../../components/ImagePreview'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { formatStore, formatTime } from '../hub/format'
import { statusOf } from '../task-center/format'
import { captureEvidenceUrl, taskSourceUrl } from './api'
import {
  canContinueMaterials,
  canReturnToMaterials,
  isRetryable,
  sourceLabelOf,
} from './format'
import type { RejectionRecord } from './types'

interface RejectionDetailDrawerProps {
  record: RejectionRecord | null
  workflowLabel?: string
  running: boolean
  busy: boolean
  hasPrev: boolean
  hasNext: boolean
  onOpenChange: (open: boolean) => void
  onPrev: () => void
  onNext: () => void
  onToggleReviewed: (record: RejectionRecord) => void
  onUndo: (record: RejectionRecord) => void
  onReturn: (record: RejectionRecord) => void
  onRetry: (record: RejectionRecord) => void
  onContinue: (record: RejectionRecord) => void
}

/** 不合格详情：证据、原因、人工确认与按来源区分的处置动作。 */
export function RejectionDetailDrawer({
  record,
  workflowLabel,
  running,
  busy,
  hasPrev,
  hasNext,
  onOpenChange,
  onPrev,
  onNext,
  onToggleReviewed,
  onUndo,
  onReturn,
  onRetry,
  onContinue,
}: RejectionDetailDrawerProps) {
  const [preview, setPreview] = useState<{ src: string; alt: string } | null>(null)
  const status = statusOf(record?.status)

  return (
    <>
      <AppDrawer
        open={record !== null}
        onOpenChange={onOpenChange}
        title={record ? formatStore(record.store_name).name : '不合格详情'}
        description={
          record ? `${sourceLabelOf(record)} · ${record.batch_name || '未关联批次'}` : ''
        }
        headerActions={
          <div className="flex items-center gap-2">
            <Button color="secondary" variant="outline" size="sm" disabled={!hasPrev} onClick={onPrev}>
              上一条
            </Button>
            <Button color="secondary" variant="outline" size="sm" disabled={!hasNext} onClick={onNext}>
              下一条
            </Button>
          </div>
        }
        footer={
          record ? (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <Checkbox
                label="已人工确认"
                checked={Boolean(record.reviewed_at)}
                disabled={busy}
                onCheckedChange={() => onToggleReviewed(record)}
              />
              <div className="flex flex-wrap items-center gap-2">
                {canContinueMaterials(record) ? (
                  <Button color="secondary" variant="outline" size="sm" onClick={() => onContinue(record)}>
                    继续处理
                  </Button>
                ) : null}
                {record.source_type === 'capture' && record.task_id ? (
                  <Button
                    color="secondary"
                    variant="outline"
                    size="sm"
                    disabled={running || busy || !canReturnToMaterials(record)}
                    title={record.return_to_materials_disabled_reason || undefined}
                    onClick={() => onReturn(record)}
                  >
                    打回素材清洗
                  </Button>
                ) : null}
                {record.source_type === 'capture' ? (
                  <Button
                    color="secondary"
                    variant="outline"
                    size="sm"
                    disabled={running || busy || !isRetryable(record.status)}
                    onClick={() => onRetry(record)}
                  >
                    重新采集
                  </Button>
                ) : null}
                <Button
                  color="danger"
                  variant="ghost"
                  size="sm"
                  disabled={running || busy}
                  onClick={() => onUndo(record)}
                >
                  撤销不合格
                </Button>
              </div>
            </div>
          ) : null
        }
      >
        {!record ? (
          <StatePanel variant="empty" title="请选择门店" description="从列表中选择一条不合格记录查看证据。" />
        ) : (
          <div className="space-y-5">
            <section className="grid gap-3 sm:grid-cols-2" aria-label="不合格概要">
              <Field label="来源" value={sourceLabelOf(record)} />
              <Field label="批次 / 阶段" value={`${record.batch_name || '—'}${workflowLabel ? ` · ${workflowLabel}` : ''}`} />
              <Field label="不合格原因" value={record.reason_label || record.reason_code || '—'} />
              <Field label="标记时间" value={record.marked_at ? formatTime(record.marked_at) : '—'} />
              <Field label="人工确认" value={record.reviewed_at ? `已确认 · ${formatTime(record.reviewed_at)}` : '未确认'} />
              <Field
                label="任务状态"
                value={
                  <span className="flex items-center gap-2">
                    <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
                    {record.stage ? <span className="text-2xs text-tertiary">{record.stage}</span> : null}
                  </span>
                }
              />
              <Field label="来源目录" value={record.source_folder || record.source_item || '—'} />
              <Field label="记录编号" value={record.task_id || record.record_id || '—'} />
            </section>

            {record.detail ? (
              <section aria-label="不合格说明" className="space-y-2">
                <h3 className="text-xs font-medium text-secondary">说明</h3>
                <p className="rounded-lg border border-subtle bg-surface-secondary p-3 text-sm text-default">
                  {record.detail}
                </p>
              </section>
            ) : null}

            {running ? (
              <Alert
                color="warning"
                variant="soft"
                title="采集运行中"
                description="运行结束后才能撤销不合格、打回或重新采集。"
              />
            ) : null}

            {record.source_type === 'capture' && record.task_id && !canReturnToMaterials(record) ? (
              <Alert
                color="warning"
                variant="soft"
                title="无法打回素材清洗"
                description={record.return_to_materials_disabled_reason || '未能验证该任务与原素材门店的关联。'}
              />
            ) : null}

            <section aria-label="证据图片" className="space-y-3">
              <h3 className="text-xs font-medium text-secondary">证据</h3>
              <div className="grid gap-3 sm:grid-cols-2">
                {record.source_type === 'capture' && record.task_id ? (
                  <>
                    <EvidenceImage
                      src={captureEvidenceUrl(record.task_id)}
                      alt="不合格证据"
                      caption={`${formatStore(record.store_name).name} · 不合格证据`}
                      onOpen={setPreview}
                    />
                    <EvidenceImage
                      src={taskSourceUrl(record.task_id)}
                      alt="任务源图"
                      caption={`${formatStore(record.store_name).name} · 任务源图`}
                      onOpen={setPreview}
                    />
                  </>
                ) : record.source_type === 'materials' && record.batch_id && record.store_id ? (
                  <EvidenceImage
                    src={`/api/materials/batches/${encodeURIComponent(record.batch_id)}/stores/${encodeURIComponent(record.store_id)}/selected`}
                    alt="当前使用的候选二维码"
                    caption={`${formatStore(record.store_name).name} · 当前使用的候选二维码`}
                    onOpen={setPreview}
                  />
                ) : (
                  <p className="text-sm text-secondary">该记录没有可预览的证据图片。</p>
                )}
              </div>
            </section>
          </div>
        )}
      </AppDrawer>

      <ImagePreview
        open={preview !== null}
        src={preview?.src ?? ''}
        alt={preview?.alt ?? ''}
        caption={preview?.alt ?? ''}
        onOpenChange={(open) => {
          if (!open) setPreview(null)
        }}
      />
    </>
  )
}

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs text-tertiary">{label}</dt>
      <dd className="mt-0.5 text-sm break-words text-default">{value || '—'}</dd>
    </div>
  )
}

interface EvidenceImageProps {
  src: string
  alt: string
  caption: string
  onOpen: (preview: { src: string; alt: string }) => void
}

function EvidenceImage({ src, alt, caption, onOpen }: EvidenceImageProps) {
  const [failed, setFailed] = useState(false)
  if (failed) {
    return (
      <div className="flex h-40 items-center justify-center rounded-lg border border-subtle bg-surface-secondary text-xs text-tertiary">
        图片不可用
      </div>
    )
  }
  return (
    <button
      type="button"
      className="overflow-hidden rounded-lg border border-subtle bg-surface-secondary text-left"
      onClick={() => onOpen({ src, alt: caption })}
    >
      <img src={src} alt={alt} className="h-40 w-full object-contain" onError={() => setFailed(true)} />
      <span className="block px-3 py-2 text-2xs text-secondary">{caption}</span>
    </button>
  )
}
