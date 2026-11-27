import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { EmptyMessage } from '@openai/apps-sdk-ui/components/EmptyMessage'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { useProject } from '../projects/ProjectProvider'
import { projectQueryKey } from '../projects/scope'
import { useRole } from '../../app/RoleProvider'
import { cancelJob, pauseJob, resumeJob } from '../materials/api'
import { pauseCapture, resumeCapture, terminateCapture } from '../worker/api'
import { fetchActiveTasks } from './api'
import { KIND_LABEL, formatTime, statusOf } from './format'
import type { ActiveTask } from './types'

interface ActiveTasksPanelProps {
  onOpen: (task: ActiveTask) => void
}

type PendingAction = 'pause' | 'resume' | 'terminate' | 'cancel'

const CONFIRM_COPY: Record<'terminate' | 'cancel', { title: string; description: string; confirm: string }> = {
  terminate: {
    title: '终止本轮采集',
    description: '本轮立即结束，已完成的门店结果保留；未处理门店仍在队列中，可重新开始采集。',
    confirm: '终止本轮',
  },
  cancel: {
    title: '取消素材分析',
    description: '本次分析任务会被取消，未处理门店不会继续分析；已完成的分析结果保留，可重新发起分析。',
    confirm: '取消分析',
  },
}

/**
 * 运行面板：轮询 /api/tasks/active，页面隐藏时 TanStack 自动停表，
 * 重新可见立即补一次。素材任务与桌面运行都使用同一份活动查询。
 *
 * 控制权限严格按角色与服务端语义：素材分析由当前实例执行，可直接暂停/继续/取消；
 * 本机采集仅 Worker/独立模式的当前有效轮次可控制；Hub 上的远程轮次只读。
 */
export function ActiveTasksPanel({ onOpen }: ActiveTasksPanelProps) {
  const { projectId } = useProject()
  const role = useRole()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState<{ task: ActiveTask; action: 'terminate' | 'cancel' } | null>(null)
  const [confirmError, setConfirmError] = useState('')

  const query = useQuery({
    queryKey: projectQueryKey(projectId, 'tasks-active'),
    queryFn: ({ signal }) => fetchActiveTasks(signal, projectId),
    enabled: Boolean(projectId),
    refetchInterval: 5_000,
    staleTime: 4_000,
  })

  const control = useMutation({
    mutationFn: async ({ task, action }: { task: ActiveTask; action: PendingAction }) => {
      if (task.source === 'materials') {
        const targetProject = task.project_id ?? projectId
        if (action === 'pause') return pauseJob(task.id, targetProject)
        if (action === 'resume') return resumeJob(task.id, targetProject)
        return cancelJob(task.id, targetProject)
      }
      if (!task.project_id) throw new Error('任务缺少项目归属，无法安全控制')
      const captureProjectId = task.project_id
      if (action === 'pause') return pauseCapture(captureProjectId)
      if (action === 'resume') return resumeCapture(undefined, captureProjectId)
      return terminateCapture(captureProjectId)
    },
    onSuccess: (_data, variables) => {
      const label = variables.action === 'pause' ? '已请求暂停'
        : variables.action === 'resume' ? '已继续'
          : variables.task.source === 'materials' ? '已取消分析' : '已终止本轮'
      toast.push(label, 'success')
      setConfirming(null)
      setConfirmError('')
      void queryClient.invalidateQueries({ queryKey: projectQueryKey(variables.task.project_id ?? projectId, 'tasks-active') })
      if (variables.task.project_id) {
        void queryClient.invalidateQueries({ queryKey: ['project', variables.task.project_id, 'task-history'] })
        void queryClient.invalidateQueries({ queryKey: ['project', variables.task.project_id, 'worker-status'] })
      }
    },
    onError: (error: unknown) => {
      const message = errorMessage(error)
      if (confirming) setConfirmError(message)
      else toast.push(message, 'danger')
    },
  })

  if (query.isError) {
    return (
      <Alert
        color="danger"
        variant="soft"
        title="运行中任务读取失败"
        description={errorMessage(query.error)}
        actions={
          <Button color="secondary" variant="outline" size="sm" onClick={() => void query.refetch()}>
            重试
          </Button>
        }
        actionsPlacement="end"
      />
    )
  }

  if (query.isPending) {
    return <StatePanel variant="loading" title="正在读取运行中任务…" />
  }

  if (!query.data.tasks.length) {
    return (
      <EmptyMessage className="rounded-xl border border-subtle bg-surface px-4 py-6">
        <EmptyMessage.Title>当前没有运行中的任务</EmptyMessage.Title>
        <EmptyMessage.Description>开始一次采集或素材分析后，进度会显示在这里。</EmptyMessage.Description>
      </EmptyMessage>
    )
  }

  const busy = control.isPending

  return (
    <>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {query.data.tasks.map((task) => {
          const status = statusOf(task.status)
          const total = Number(task.progress?.total) || 0
          const done = Number(task.progress?.done) || 0
          const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0
          // Hub 上的远程轮次只读；采集类记录携带 worker_id 即来自其他采集电脑。
          const remote = task.source === 'capture' && (role.role === 'hub' || Boolean(task.extra?.worker_id))
          const canPause = task.status === 'running' || task.status === 'pause_requested'
          const canResume = task.status === 'paused' || task.status === 'blocked'
          const canStop = task.status === 'running' || task.status === 'paused' || task.status === 'pause_requested'
          return (
            <article
              key={`${task.source}:${task.id}`}
              className="rounded-xl border border-subtle bg-surface flex flex-col gap-3 p-4"
              aria-label={`活动任务 ${task.title}`}
            >
              <header className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="truncate heading-xs" title={task.title}>
                    {task.title || task.id}
                  </h3>
                  <p className="mt-0.5 text-xs text-secondary">
                    {KIND_LABEL[task.source] ?? task.source}
                    {remote ? ' · 远程设备' : ''} · 更新于 {formatTime(task.updated_at)}
                  </p>
                </div>
                <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
              </header>
              <div>
                <div className="mb-1 flex justify-between text-xs text-secondary">
                  <span>
                    已处理 {done} / {total}
                  </span>
                  <span>{percent}%</span>
                </div>
                <div
                  className="h-1.5 overflow-hidden rounded-full bg-surface-tertiary"
                  role="progressbar"
                  aria-valuemin={0}
                  aria-valuemax={total}
                  aria-valuenow={done}
                  aria-label="任务进度"
                >
                  <div className="h-full rounded-full bg-primary-solid transition-[width]" style={{ width: `${percent}%` }} />
                </div>
              </div>
              <div className="flex flex-wrap items-center justify-end gap-2">
                {remote ? (
                  <span className="mr-auto text-2xs text-tertiary">远程采集电脑的轮次仅可查看</span>
                ) : null}
                {!remote && task.source === 'capture' ? (
                  <>
                    {canPause ? (
                      <Button color="secondary" variant="outline" size="sm" disabled={busy} onClick={() => control.mutate({ task, action: 'pause' })}>
                        暂停采集
                      </Button>
                    ) : null}
                    {canResume ? (
                      <Button color="secondary" variant="outline" size="sm" disabled={busy} onClick={() => control.mutate({ task, action: 'resume' })}>
                        继续采集
                      </Button>
                    ) : null}
                    {canStop ? (
                      <Button
                        color="danger"
                        variant="outline"
                        size="sm"
                        disabled={busy}
                        onClick={() => {
                          setConfirmError('')
                          setConfirming({ task, action: 'terminate' })
                        }}
                      >
                        终止本轮
                      </Button>
                    ) : null}
                  </>
                ) : null}
                {!remote && task.source === 'materials' ? (
                  <>
                    {canPause ? (
                      <Button color="secondary" variant="outline" size="sm" disabled={busy} onClick={() => control.mutate({ task, action: 'pause' })}>
                        暂停分析
                      </Button>
                    ) : null}
                    {canResume ? (
                      <Button color="secondary" variant="outline" size="sm" disabled={busy} onClick={() => control.mutate({ task, action: 'resume' })}>
                        继续分析
                      </Button>
                    ) : null}
                    {canStop ? (
                      <Button
                        color="danger"
                        variant="outline"
                        size="sm"
                        disabled={busy}
                        onClick={() => {
                          setConfirmError('')
                          setConfirming({ task, action: 'cancel' })
                        }}
                      >
                        取消分析
                      </Button>
                    ) : null}
                  </>
                ) : null}
                <Button color="secondary" variant="outline" size="sm" onClick={() => onOpen(task)}>
                  查看详情
                </Button>
              </div>
            </article>
          )
        })}
      </div>

      <ConfirmActionDialog
        open={confirming !== null}
        title={confirming ? CONFIRM_COPY[confirming.action].title : ''}
        description={
          confirming ? (
            <>
              <p>{CONFIRM_COPY[confirming.action].description}</p>
              <p className="mt-2 text-xs text-secondary">对象：{confirming.task.title || confirming.task.id}</p>
            </>
          ) : null
        }
        confirmLabel={confirming ? CONFIRM_COPY[confirming.action].confirm : '确认'}
        tone="danger"
        busy={control.isPending}
        error={confirmError}
        onConfirm={() => {
          if (confirming) control.mutate({ task: confirming.task, action: confirming.action })
        }}
        onOpenChange={(open) => {
          if (!open && !control.isPending) {
            setConfirming(null)
            setConfirmError('')
          }
        }}
      />
    </>
  )
}
