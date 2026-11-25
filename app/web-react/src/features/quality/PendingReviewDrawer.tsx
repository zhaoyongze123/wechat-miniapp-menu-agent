import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { SegmentedControl } from '@openai/apps-sdk-ui/components/SegmentedControl'
import { AppDrawer } from '../../components/AppDrawer'
import { ImagePreview } from '../../components/ImagePreview'
import { KeyValuePair } from '../../components/KeyValueList'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { errorMessage } from '../../api/client'
import { fetchTaskDetail } from '../task-center/api'
import { formatTime, statusOf } from '../task-center/format'
import { fetchTaskActions } from '../worker/api'
import { formatStore } from '../hub/format'
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
  /** 确认无需继续采集，退回素材清洗重新确认二维码。 */
  onReturn: (record: PendingReviewRecord) => void
}

interface PendingReviewActionsProps {
  record: PendingReviewRecord
  onRerun: (record: PendingReviewRecord) => void
  onReject: (record: PendingReviewRecord) => void
  onReturn: (record: PendingReviewRecord) => void
}

type DetailSection = 'current' | 'history' | 'diagnostics'

const ACTION_LABELS: Record<string, string> = {
  ocr: '本地文字识别', model: '视觉模型判断', page_check: '检查页面变化',
  wechat_marker_check: '匹配本轮采集标记', click: '点击', double_click: '双击',
  right_click: '右击', noop: '等待', screenshot: '截图', scroll_capture: '滚动截图',
  resize: '调整窗口', discover: '查找窗口', task_result: '任务结果',
}

const RESULT_LABELS: Record<string, string> = { ok: '成功', error: '失败', unknown: '结果未知', running: '尚无结果' }

/** 处置按钮组：抽屉脚部与任务详情就地处置共用同一组文案与禁用规则。 */
export function PendingReviewActionButtons({ record, onRerun, onReject, onReturn }: PendingReviewActionsProps) {
  return (
    <>
      <Button color="danger" variant="outline" size="sm" onClick={() => onReject(record)}>
        标记不合格
      </Button>
      <Button
        color="secondary"
        variant="outline"
        size="sm"
        disabled={record.can_return_to_materials === false}
        title={record.return_to_materials_disabled_reason || undefined}
        onClick={() => onReturn(record)}
      >
        打回素材清洗
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
    </>
  )
}

/**
 * 待检查门店详情内容：当前结果、历史记录、诊断证据三分区。
 *
 * 历史与诊断只展示后台已有事实：轮次未同步、动作记录被清理时如实说明，
 * 不用门店名或本地文件猜造记录，也不在前端推断审核资格。
 */
export function PendingReviewContent({ record }: { record: PendingReviewRecord }) {
  const [preview, setPreview] = useState<{ src: string; alt: string } | null>(null)
  const [section, setSection] = useState<DetailSection>('current')
  const taskId = record.task_id
  const runId = record.run_id ?? ''
  const status = statusOf(record.status)
  const previewItems = [
    { src: taskSourceUrl(record.task_id), alt: '任务源图' },
    { src: taskLastFrameUrl(record.task_id), alt: '最近画面' },
  ]

  // 切换门店时回到当前结果，避免看到上一家的历史。
  useEffect(() => {
    setSection('current')
  }, [taskId])

  const historyQuery = useQuery({
    queryKey: ['review-history', runId],
    queryFn: ({ signal }) => fetchTaskDetail('capture', runId, signal),
    enabled: section === 'history' && Boolean(runId),
    retry: false,
  })
  const actionsQuery = useQuery({
    queryKey: ['review-actions', taskId],
    queryFn: ({ signal }) => fetchTaskActions(taskId, null, signal),
    enabled: section === 'diagnostics' && Boolean(taskId),
    retry: false,
  })

  const historyDetail = historyQuery.data?.kind === 'capture' ? historyQuery.data : null
  const historyRow = historyDetail?.tasks.find((task) => task.task_id === taskId)
  const actions = actionsQuery.data?.actions ?? []

  return (
    <>
      <div className="space-y-5">
        <dl className="grid gap-3 sm:grid-cols-2" aria-label="待检查概要">
          <KeyValuePair label="任务状态">
            <span className="flex flex-wrap items-center gap-2">
              <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
              <StatusBadge tone="secondary">{bucketLabelOf(record)}</StatusBadge>
            </span>
          </KeyValuePair>
          <KeyValuePair label="失败标签">{record.label || record.group || '—'}</KeyValuePair>
          <KeyValuePair label="批次 / 阶段">{`${record.batch_name || '—'}${record.workflow?.label ? ` · ${record.workflow.label}` : ''}`}</KeyValuePair>
          <KeyValuePair label="更新时间">{record.updated_at ? formatTime(record.updated_at) : '—'}</KeyValuePair>
        </dl>

        <SegmentedControl<DetailSection> value={section} onChange={setSection} aria-label="门店详情分区">
          <SegmentedControl.Option value="current">当前结果</SegmentedControl.Option>
          <SegmentedControl.Option value="history">历史记录</SegmentedControl.Option>
          <SegmentedControl.Option value="diagnostics">诊断证据</SegmentedControl.Option>
        </SegmentedControl>

        {section === 'current' ? (
          <div className="space-y-5">
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
        ) : null}

        {section === 'history' ? (
          <section className="space-y-3" aria-label="历史记录">
            {!runId ? (
              <StatePanel
                variant="empty"
                title="没有可用的采集轮次"
                description="这条记录尚未关联到采集轮次，无法查看当次结果；可能来自旧数据或共享端尚未同步。"
              />
            ) : historyQuery.isError ? (
              <Alert
                color="danger"
                variant="soft"
                title="历史记录读取失败"
                description={errorMessage(historyQuery.error)}
              />
            ) : historyQuery.isPending ? (
              <StatePanel variant="loading" title="正在读取历史记录…" />
            ) : historyDetail ? (
              <div className="space-y-3">
                <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <KeyValuePair label="轮次状态"><StatusBadge tone={statusOf(historyDetail.status).tone}>{statusOf(historyDetail.status).label}</StatusBadge></KeyValuePair>
                  <KeyValuePair label="执行设备">{historyDetail.worker_name || '本机'}</KeyValuePair>
                  <KeyValuePair label="开始时间">{historyDetail.created_at ? formatTime(historyDetail.created_at) : '—'}</KeyValuePair>
                  <KeyValuePair label="同步状态">{historyDetail.history_status === 'partial' ? '部分记录已上报' : historyDetail.worker_name ? '已同步' : '本机记录'}</KeyValuePair>
                </dl>
                {historyDetail.history_status === 'partial' ? (
                  <Alert color="warning" variant="soft" title="共享端逐店记录可能尚未全部上报" description="这里只显示已收到的当次结果。" />
                ) : null}
                {historyRow ? (
                  <div className="rounded-lg border border-subtle p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusBadge tone={statusOf(historyRow.status).tone}>
                        {historyRow.label || historyRow.status || '未记录'}
                      </StatusBadge>
                      <span className="text-xs text-secondary">{historyRow.batch_name || record.batch_name || '—'}{historyRow.version ? ` · ${historyRow.version}` : ''}</span>
                    </div>
                    <p className="mt-2 whitespace-pre-wrap break-words text-xs text-secondary">
                      {historyRow.reject_reason || historyRow.message || '这次轮次没有保存该门店的说明。'}
                    </p>
                  </div>
                ) : (
                  <p className="text-xs text-tertiary">这次轮次没有保存该门店的逐店结果，可能来自旧记录。</p>
                )}
              </div>
            ) : null}
          </section>
        ) : null}

        {section === 'diagnostics' ? (
          <section className="space-y-4" aria-label="诊断证据">
            <dl className="grid gap-3 sm:grid-cols-2">
              <KeyValuePair label="错误码">{record.error_code || '—'}</KeyValuePair>
              <KeyValuePair label="任务编号">{record.task_id || '—'}</KeyValuePair>
              <KeyValuePair label="轮次编号">{runId || '—'}</KeyValuePair>
              <KeyValuePair label="源路径">{record.source_path || '—'}</KeyValuePair>
              <KeyValuePair label="输出目录">{record.output_dir || '—'}</KeyValuePair>
            </dl>
            <div className="space-y-2">
              <h3 className="text-xs font-medium text-secondary">动作记录</h3>
              {actionsQuery.isError ? (
                <Alert color="warning" variant="soft" title="动作记录读取失败" description={errorMessage(actionsQuery.error)} />
              ) : actionsQuery.isPending ? (
                <StatePanel variant="loading" title="正在读取动作记录…" />
              ) : actions.length ? (
                <ol className="space-y-2">
                  {actions.slice(0, 20).map((action) => (
                    <li key={action.id} className="rounded-lg border border-subtle p-2.5 text-xs">
                      <div className="flex flex-wrap items-center gap-2">
                        <time className="text-secondary tabular-nums">{action.started_at ? formatTime(action.started_at) : '—'}</time>
                        <strong className="text-default">{ACTION_LABELS[action.action] ?? action.action}</strong>
                        <StatusBadge tone={action.result === 'error' ? 'danger' : action.result === 'ok' ? 'success' : 'secondary'}>
                          {RESULT_LABELS[action.result] ?? action.result}
                        </StatusBadge>
                        {action.elapsed_ms !== null ? <span className="text-tertiary">{action.elapsed_ms} ms</span> : null}
                      </div>
                      {action.stage ? <p className="mt-1 text-tertiary">{action.stage}</p> : null}
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="text-xs text-tertiary">暂无动作记录；调试记录可能已按容量清理。</p>
              )}
            </div>
          </section>
        ) : null}
      </div>

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

export function PendingReviewDrawer({
  record,
  hasPrev,
  hasNext,
  onOpenChange,
  onPrev,
  onNext,
  onRerun,
  onReject,
  onReturn,
}: PendingReviewDrawerProps) {
  return (
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
              <PendingReviewActionButtons
                record={record}
                onRerun={onRerun}
                onReject={onReject}
                onReturn={onReturn}
              />
            </div>
          </div>
        ) : null
      }
    >
      {!record ? (
        <StatePanel variant="empty" title="请选择门店" description="从待检查列表选择一家门店查看失败原因。" />
      ) : (
        <PendingReviewContent record={record} />
      )}
    </AppDrawer>
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
