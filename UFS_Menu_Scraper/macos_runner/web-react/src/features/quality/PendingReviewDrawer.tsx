import { useState, type ReactNode } from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { AppDrawer } from '../../components/AppDrawer'
import { ImagePreview } from '../../components/ImagePreview'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { formatStore, formatTime } from '../hub/format'
import { statusOf } from '../task-center/format'
import { taskLastFrameUrl, taskSourceUrl } from './api'
import { bucketLabelOf, isRetryable } from './format'
import type { PendingReviewRecord } from './types'

interface PendingReviewDrawerProps {
  record: PendingReviewRecord | null
  hasPrev: boolean
  hasNext: boolean
  onOpenChange: (open: boolean) => void
  onPrev: () => void
  onNext: () => void
  onRerun: (record: PendingReviewRecord) => void
  /** 人工判定为不合格，直接移入不合格清单。 */
  onReject: (record: PendingReviewRecord) => void
}

/** 待检查门店详情：失败原因、分类、源图与最近画面，重跑走分配流程。 */
export function PendingReviewDrawer({
  record,
  hasPrev,
  hasNext,
  onOpenChange,
  onPrev,
  onNext,
  onRerun,
  onReject,
}: PendingReviewDrawerProps) {
  const [preview, setPreview] = useState<{ src: string; alt: string } | null>(null)
  const status = statusOf(record?.status)
  const previewItems = record?.task_id
    ? [
        { src: taskSourceUrl(record.task_id), alt: '任务源图' },
        { src: taskLastFrameUrl(record.task_id), alt: '最近画面' },
      ]
    : []

  return (
    <>
      <AppDrawer
        open={record !== null}
        onOpenChange={onOpenChange}
        title={record ? formatStore(record.store_name).name : '待检查门店'}
        description={record ? `${record.batch_name || '未关联批次'} · ${record.stage || '—'}` : ''}
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
              <span className="text-xs text-secondary">
                当前分类：{bucketLabelOf(record)}
              </span>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  color="danger"
                  variant="outline"
                  size="sm"
                  onClick={() => onReject(record)}
                >
                  标记不合格
                </Button>
                <Button
                  color="secondary"
                  variant="outline"
                  size="sm"
                  disabled={!isRetryable(record.status)}
                  onClick={() => onRerun(record)}
                >
                  重跑（分配采集电脑）
                </Button>
              </div>
            </div>
          ) : null
        }
      >
        {!record ? (
          <StatePanel variant="empty" title="请选择门店" description="从待检查列表选择一家门店查看失败原因。" />
        ) : (
          <div className="space-y-5">
            <section className="grid gap-3 sm:grid-cols-2" aria-label="待检查概要">
              <Field
                label="任务状态"
                value={
                  <span className="flex flex-wrap items-center gap-2">
                    <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
                    <StatusBadge tone="secondary">{bucketLabelOf(record)}</StatusBadge>
                  </span>
                }
              />
              <Field label="失败标签" value={record.label || record.group || '—'} />
              <Field label="批次 / 阶段" value={`${record.batch_name || '—'}${record.workflow?.label ? ` · ${record.workflow.label}` : ''}`} />
              <Field label="更新时间" value={record.updated_at ? formatTime(record.updated_at) : '—'} />
              <Field label="错误码" value={record.error_code || '—'} />
              <Field label="任务编号" value={record.task_id || '—'} />
              <Field label="源路径" value={record.source_path || '—'} />
              <Field label="输出目录" value={record.output_dir || '—'} />
            </section>

            {record.message || record.reject_detail ? (
              <section aria-label="失败说明" className="space-y-2">
                <h3 className="text-xs font-medium text-secondary">说明</h3>
                <p className="rounded-lg border border-subtle bg-surface-secondary p-3 text-sm break-words text-default">
                  {record.reject_detail || record.message}
                </p>
              </section>
            ) : null}

            {record.reject_reason ? (
              <section className="space-y-2">
                <h3 className="text-xs font-medium text-secondary">已标记不合格</h3>
                <p className="text-sm text-default">原因：{record.reject_reason}</p>
              </section>
            ) : null}

            <section aria-label="门店画面" className="space-y-3">
              <h3 className="text-xs font-medium text-secondary">门店画面</h3>
              <div className="grid gap-3 sm:grid-cols-2">
                <EvidenceImage
                  src={taskSourceUrl(record.task_id)}
                  alt="任务源图"
                  caption={`${formatStore(record.store_name).name} · 源图`}
                  onOpen={setPreview}
                />
                <EvidenceImage
                  src={taskLastFrameUrl(record.task_id)}
                  alt="最近画面"
                  caption={`${formatStore(record.store_name).name} · 最近画面`}
                  onOpen={setPreview}
                />
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
        items={previewItems}
        initialIndex={Math.max(0, previewItems.findIndex((item) => item.src === preview?.src))}
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
