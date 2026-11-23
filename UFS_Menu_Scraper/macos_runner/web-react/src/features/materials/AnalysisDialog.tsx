import { useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { AppDialog } from '../../components/AppDialog'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { formatTime } from '../hub/format'
import { formatDuration, isJobOpen, jobStatusMeta } from './format'
import { useBatch, useBatchActions, useJob, useJobs } from './hooks'

interface AnalysisDialogProps {
  open: boolean
  batchId: string
  onOpenChange: (open: boolean) => void
}

/** 分析作业进度：运行态从前端轮询，暂停/继续/取消都回到同一作业事实源。 */
export function AnalysisDialog({ open, batchId, onOpenChange }: AnalysisDialogProps) {
  const toast = useToast()
  const batchQuery = useBatch(batchId)
  const jobsQuery = useJobs(batchId, open)
  const actions = useBatchActions(batchId)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)

  const current = batchQuery.data?.job ?? null
  const activeJobId = expanded ?? current?.id ?? ''
  const detailQuery = useJob(activeJobId, open && !!activeJobId)
  const job = detailQuery.data ?? current

  const jobs = jobsQuery.data?.jobs ?? []
  const progress = job && job.total_stores ? Math.round((job.done_stores / job.total_stores) * 100) : 0

  const control = (action: 'pause' | 'resume') => {
    if (!job) return
    const mutation = action === 'pause' ? actions.pause : actions.resume
    mutation.mutate(job.id, {
      onSuccess: () => toast.push(action === 'pause' ? '已请求暂停分析' : '已请求继续分析', 'success'),
      onError: (cause) => toast.push(errorMessage(cause), 'danger'),
    })
  }

  return (
    <>
      <AppDialog
        open={open}
        onOpenChange={onOpenChange}
        title="分析进度"
        description={`批次：${batchQuery.data?.name ?? batchId}`}
        widthClass="w-[min(760px,94vw)]"
        footer={
          <div className="flex justify-end">
            <Button color="secondary" variant="outline" size="sm" onClick={() => onOpenChange(false)}>
              关闭
            </Button>
          </div>
        }
      >
        <div className="space-y-5">
          {job ? (
            <section className="space-y-3 rounded-xl border border-subtle p-4" aria-label="当前作业">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <StatusBadge tone={jobStatusMeta(job.status).tone}>{jobStatusMeta(job.status).label}</StatusBadge>
                  <span className="text-sm text-default">
                    {job.kind === 'reanalyze' ? '重新分析' : '首次分析'} · 并发 {job.concurrency}
                  </span>
                </div>
                <div className="flex flex-wrap gap-2">
                  {job.status === 'running' || job.status === 'queued' ? (
                    <Button
                      color="secondary"
                      variant="outline"
                      size="sm"
                      onClick={() => control('pause')}
                      disabled={actions.pause.isPending}
                    >
                      暂停
                    </Button>
                  ) : null}
                  {job.status === 'paused' || job.status === 'interrupted' || job.status === 'pause_requested' ? (
                    <Button
                      color="primary"
                      size="sm"
                      onClick={() => control('resume')}
                      disabled={actions.resume.isPending || job.status === 'pause_requested'}
                    >
                      继续
                    </Button>
                  ) : null}
                  {isJobOpen(job.status) ? (
                    <Button color="danger" variant="outline" size="sm" onClick={() => setCancelOpen(true)}>
                      取消作业
                    </Button>
                  ) : null}
                </div>
              </div>

              <div className="space-y-1">
                <div className="h-2 overflow-hidden rounded-full bg-surface-secondary">
                  <div className="h-full bg-primary-solid transition-all" style={{ width: `${progress}%` }} />
                </div>
                <div className="flex flex-wrap justify-between gap-2 text-xs text-secondary">
                  <span>
                    {job.done_stores} / {job.total_stores} 家 · 失败 {job.failed_stores}
                    {job.queue_position ? ` · 队列第 ${job.queue_position} 位` : ''}
                    {job.eta_seconds !== undefined ? ` · 预计剩余 ${formatDuration(job.eta_seconds)}` : ''}
                  </span>
                  <span>{job.stage || jobStatusMeta(job.status).label}</span>
                </div>
              </div>

              {job.current_store_name ? (
                <p className="text-xs text-secondary">正在处理：{job.current_store_name}</p>
              ) : null}
              {job.message ? <p className="text-xs text-secondary">{job.message}</p> : null}
              {job.error ? <Alert color="danger" variant="soft" title={job.error} /> : null}

              {(job.events ?? []).length ? (
                <div className="max-h-48 space-y-1 overflow-y-auto rounded-lg bg-surface-secondary p-3">
                  {job.events?.slice(0, 20).map((event) => (
                    <p key={event.id} className="text-2xs text-secondary">
                      <span className="text-tertiary">{formatTime(event.time)}</span>{' '}
                      {event.store_name ? <span className="text-default">{event.store_name} </span> : null}
                      {event.message}
                    </p>
                  ))}
                </div>
              ) : null}
            </section>
          ) : (
            <Alert color="info" variant="soft" title="当前没有分析作业" description="该批次的分析已结束，或尚未开始。" />
          )}

          <section className="space-y-2" aria-label="历史作业">
            <h3 className="heading-xs">历史作业</h3>
            {jobs.length === 0 ? (
              <p className="text-xs text-secondary">暂无作业记录。</p>
            ) : (
              <ul className="divide-y divide-subtle rounded-lg border border-subtle">
                {jobs.map((item) => (
                  <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                    <div className="min-w-0">
                      <p className="flex flex-wrap items-center gap-2 text-sm text-default">
                        <StatusBadge tone={jobStatusMeta(item.status).tone}>{jobStatusMeta(item.status).label}</StatusBadge>
                        <span>{item.kind === 'reanalyze' ? '重新分析' : '首次分析'}</span>
                        <span className="text-xs text-secondary">
                          {item.done_stores}/{item.total_stores} 家
                          {item.failed_stores ? ` · 失败 ${item.failed_stores}` : ''}
                        </span>
                      </p>
                      <p className="text-2xs text-secondary">
                        {formatTime(item.started_at || item.created_at)} → {item.finished_at ? formatTime(item.finished_at) : '进行中'}
                      </p>
                    </div>
                    <Button
                      color="secondary"
                      variant="ghost"
                      size="sm"
                      onClick={() => setExpanded(expanded === item.id ? null : item.id)}
                    >
                      {expanded === item.id ? '收起' : '查看事件'}
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </AppDialog>

      <ConfirmActionDialog
        open={cancelOpen}
        title="取消分析作业"
        description={
          <div className="space-y-1">
            <p>取消后正在处理的图片会停止分析，已完成的门店结果保留。</p>
            <p>未完成门店需要重新发起分析后才能审核。</p>
          </div>
        }
        confirmLabel="确认取消作业"
        tone="danger"
        busy={actions.cancel.isPending}
        onOpenChange={(next) => {
          if (!next && !actions.cancel.isPending) setCancelOpen(false)
        }}
        onConfirm={() => {
          if (!job) return
          actions.cancel.mutate(job.id, {
            onSuccess: () => {
              setCancelOpen(false)
              toast.push('已取消分析作业', 'success')
            },
            onError: (cause) => toast.push(errorMessage(cause), 'danger'),
          })
        }}
      />
    </>
  )
}
