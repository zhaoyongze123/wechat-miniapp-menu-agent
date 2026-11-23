import { useEffect, useMemo, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDialog } from '../../components/AppDialog'
import { ApiError, errorMessage } from '../../api/client'
import { assignTasks } from './api'
import { conflictReasonText, deviceStateOf, formatStore } from './format'
import { isWorkerActive } from './hooks'
import type { AssignConflict, HubSelectedTask, HubWorker } from './types'

interface AssignDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  tasks: HubSelectedTask[]
  workers: HubWorker[]
  /** 冲突门店从父级选择中移除，保证关闭后选择与列表一致。 */
  onExclude: (ids: string[]) => void
  onAssigned: (count: number, workerName: string) => void
}

function parseConflicts(value: unknown[] | null): AssignConflict[] {
  if (!value) return []
  return value.flatMap((item) => {
    if (typeof item !== 'object' || item === null) return []
    const record = item as { task_id?: unknown; reason?: unknown; worker_name?: unknown }
    if (typeof record.task_id !== 'string' || typeof record.reason !== 'string') return []
    return [{
      task_id: record.task_id,
      reason: record.reason as AssignConflict['reason'],
      ...(typeof record.worker_name === 'string' ? { worker_name: record.worker_name } : {}),
    }]
  })
}

/**
 * 分配确认窗口：一次提交一组门店，服务端在同一事务里核对占用。
 * 409 表示整批未生效；窗口列出冲突门店、从本次选择中移除，并允许提交剩余部分。
 */
export function AssignDialog({ open, onOpenChange, tasks, workers, onExclude, onAssigned }: AssignDialogProps) {
  const [workerId, setWorkerId] = useState('')
  const [excluded, setExcluded] = useState<Set<string>>(new Set())
  const [conflicts, setConflicts] = useState<AssignConflict[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    setWorkerId('')
    setExcluded(new Set())
    setConflicts([])
    setError('')
  }, [open])

  const activeWorkers = useMemo(() => workers.filter(isWorkerActive), [workers])
  const taskIndex = useMemo(() => new Map(tasks.map((task) => [task.id, task])), [tasks])
  const remaining = useMemo(() => tasks.filter((task) => !excluded.has(task.id)), [tasks, excluded])
  const selectedWorker = activeWorkers.find((worker) => worker.id === workerId)
  const workerTone = selectedWorker ? deviceStateOf(selectedWorker) : null

  const batchSummary = useMemo(() => {
    const counts = new Map<string, number>()
    for (const task of remaining) {
      const name = task.batch_name || '批次未记录'
      counts.set(name, (counts.get(name) ?? 0) + 1)
    }
    return [...counts].map(([name, count]) => `${name} ${count} 家`).join(' · ')
  }, [remaining])

  const mutation = useMutation({
    mutationFn: (payload: { workerId: string; taskIds: string[] }) => assignTasks(payload.workerId, payload.taskIds),
    onSuccess: (result) => {
      const count = result.assignments?.length ?? remaining.length
      onAssigned(count, selectedWorker?.name ?? '采集电脑')
    },
    onError: (cause: unknown) => {
      if (cause instanceof ApiError) {
        const parsed = parseConflicts(cause.conflicts)
        if (parsed.length) {
          const ids = parsed.map((item) => item.task_id)
          setConflicts(parsed)
          setExcluded((current) => new Set([...current, ...ids]))
          onExclude(ids)
          setError(cause.message)
          return
        }
      }
      setError(errorMessage(cause))
    },
  })

  const submit = () => {
    if (!workerId || !remaining.length) return
    setError('')
    setConflicts([])
    mutation.mutate({ workerId, taskIds: remaining.map((task) => task.id) })
  }

  const close = () => {
    if (mutation.isPending) return
    onOpenChange(false)
  }

  return (
    <AppDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) close()
      }}
      title={`分配 ${remaining.length} 家门店`}
      description="选择一台已配对的采集电脑。分配后门店会进入该电脑的下载队列，离线电脑会在重新连接后同步。"
      widthClass="w-[min(640px,94vw)]"
      footer={
        <div className="flex items-center justify-between gap-3">
          <span className="text-xs text-secondary">已选 {tasks.length} 家{conflicts.length ? ` · 冲突 ${conflicts.length} 家` : ''}</span>
          <div className="flex gap-2">
            <Button color="secondary" variant="outline" size="sm" onClick={close} disabled={mutation.isPending}>
              取消
            </Button>
            <Button
              color="primary"
              size="sm"
              onClick={submit}
              disabled={mutation.isPending || !workerId || remaining.length === 0}
            >
              {mutation.isPending ? '正在分配…' : conflicts.length ? `分配剩余 ${remaining.length} 家` : `确认分配 ${remaining.length} 家`}
            </Button>
          </div>
        </div>
      }
    >
      <div className="space-y-4">
        {activeWorkers.length === 0 ? (
          <Alert
            color="warning"
            variant="soft"
            title="尚未配对采集电脑"
            description="请先在“采集电脑”页面生成配对码并在 Windows 采集端完成配对。"
          />
        ) : (
          <div className="space-y-1.5">
            <label className="block text-sm text-default" htmlFor="hub-assign-worker">
              目标采集电脑
            </label>
            <Select
              id="hub-assign-worker"
              aria-label="目标采集电脑"
              placeholder="请选择采集电脑"
              value={workerId}
              options={activeWorkers.map((worker) => ({
                value: worker.id,
                label: `${worker.name} · ${deviceStateOf(worker).label}`,
              }))}
              onChange={(option) => setWorkerId(option.value)}
            />
            {workerTone && workerTone.key === 'offline' ? (
              <p className="text-xs text-warning">
                该电脑当前离线：任务会保留在共享后台，待其重新连接并同步后再下载。
              </p>
            ) : null}
          </div>
        )}

        <div className="rounded-xl border border-subtle bg-surface-secondary px-4 py-3 text-sm">
          <p className="text-default">本次将分配 {remaining.length} 家门店</p>
          <p className="mt-1 text-xs text-secondary">{batchSummary || '批次信息未记录'}</p>
        </div>

        {conflicts.length ? (
          <div className="space-y-2">
            <Alert
              color="warning"
              variant="soft"
              title={`${conflicts.length} 家门店暂时无法分配，本次提交未生效`}
              description="冲突门店已从本次选择中移除，可核对原因后分配剩余门店。"
            />
            <ul className="max-h-48 space-y-1 overflow-y-auto rounded-lg border border-subtle bg-surface p-2 text-xs">
              {conflicts.map((item) => {
                const task = taskIndex.get(item.task_id)
                const store = formatStore(task?.source_name ?? '')
                return (
                  <li key={item.task_id} className="flex items-start justify-between gap-3 px-1 py-1">
                    <span className="min-w-0 truncate text-default">{store.name || item.task_id}</span>
                    <span className="shrink-0 text-danger">{conflictReasonText(item.reason, item.worker_name)}</span>
                  </li>
                )
              })}
            </ul>
          </div>
        ) : null}

        {error && !conflicts.length ? <Alert color="danger" variant="soft" title={error} /> : null}
      </div>
    </AppDialog>
  )
}
