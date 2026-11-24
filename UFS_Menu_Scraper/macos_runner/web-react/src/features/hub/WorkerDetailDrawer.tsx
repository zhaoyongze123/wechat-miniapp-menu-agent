import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Textarea } from '@openai/apps-sdk-ui/components/Textarea'
import { AppDrawer } from '../../components/AppDrawer'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { useUnsavedChanges } from '../../components/UnsavedChangesProvider'
import { errorMessage } from '../../api/client'
import { editWorker } from './api'
import { assignmentStatusOf, deviceStateOf, formatStore, formatTime } from './format'
import type { HubAssignment, HubWorker } from './types'

interface WorkerDetailDrawerProps {
  open: boolean
  worker: HubWorker | null
  assignments: HubAssignment[]
  onOpenChange: (open: boolean) => void
  onUpdated: () => void
  onRequestRevoke: (worker: HubWorker) => void
  onRequestArchive: (worker: HubWorker) => void
}

/** 设备详情：稳定设备 ID、连接事实、占用门店与编辑入口；解除与移除统一走父级确认窗口。 */
export function WorkerDetailDrawer({
  open,
  worker,
  assignments,
  onOpenChange,
  onUpdated,
  onRequestRevoke,
  onRequestArchive,
}: WorkerDetailDrawerProps) {
  const toast = useToast()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [formError, setFormError] = useState('')

  useEffect(() => {
    if (!worker) return
    setName(worker.name)
    setDescription(worker.description ?? '')
    setFormError('')
  }, [worker?.id, worker?.name, worker?.description])

  const mutation = useMutation({
    mutationFn: (payload: { id: string; name: string; description: string }) =>
      editWorker(payload.id, payload.name, payload.description),
    onSuccess: () => {
      toast.push('设备信息已保存', 'success')
      onUpdated()
    },
  })

  const dirty = Boolean(
    open && worker && (name.trim() !== worker.name || description.trim() !== (worker.description ?? '')),
  )
  useUnsavedChanges(dirty, '设备信息有未保存的修改，离开后会丢失。')

  if (!open) return null
  if (!worker) {
    return (
      <AppDrawer open={open} onOpenChange={onOpenChange} title="设备详情">
        <StatePanel variant="info" title="设备已不在当前列表中" description="列表可能刚刚刷新，请关闭后重试。" />
      </AppDrawer>
    )
  }

  const state = deviceStateOf(worker)

  const save = () => {
    const trimmed = name.trim()
    if (!trimmed) {
      setFormError('设备名称不能为空')
      return
    }
    if (trimmed.length > 64) {
      setFormError('设备名称不能超过 64 个字符')
      return
    }
    if (description.trim().length > 500) {
      setFormError('备注不能超过 500 个字符')
      return
    }
    setFormError('')
    mutation.mutate({ id: worker.id, name: trimmed, description: description.trim() })
  }

  const footer = worker.archived_at ? (
    <p className="text-xs text-tertiary">设备已从列表移除；稳定设备 ID 与历史分配记录仍保留。</p>
  ) : worker.revoked_at ? (
    <div className="flex items-center justify-between gap-3">
      <p className="text-xs text-tertiary">解除配对不会删除历史分配记录。</p>
      <Button color="danger" size="sm" onClick={() => onRequestArchive(worker)}>
        从设备列表移除
      </Button>
    </div>
  ) : (
    <div className="space-y-2">
      <Button
        color="danger"
        size="sm"
        disabled={assignments.length > 0}
        onClick={() => onRequestRevoke(worker)}
      >
        解除配对
      </Button>
      <p className="text-xs text-tertiary">
        {assignments.length > 0
          ? `该电脑仍有 ${assignments.length} 家门店未回收，请先在“分配记录”中回收后再解除。`
          : '解除后该电脑立即失去访问权限，历史分配记录与稳定设备 ID 保留。'}
      </p>
    </div>
  )

  return (
    <AppDrawer
      open={open}
      onOpenChange={onOpenChange}
      title={worker.name}
      description={`稳定设备 ID：${worker.id}`}
      footer={footer}
    >
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={state.tone}>{state.label}</StatusBadge>
          {worker.pending_uploads ? (
            <StatusBadge tone="warning">待补传 {worker.pending_uploads} 份</StatusBadge>
          ) : null}
          <StatusBadge tone={assignments.length ? 'info' : 'secondary'}>占用 {assignments.length} 家</StatusBadge>
        </div>

        <dl className="grid grid-cols-1 gap-x-4 gap-y-3 text-sm sm:grid-cols-2">
          <Fact label="最近连接">{formatTime(worker.last_seen)}</Fact>
          <Fact label="配对时间">{formatTime(worker.paired_at)}</Fact>
          <Fact label="待补传文件">{worker.pending_uploads ? `${worker.pending_uploads} 份` : '无'}</Fact>
          <Fact label="最近任务">
            {worker.last_run
              ? [
                  worker.last_run.status || '状态未记录',
                  worker.last_run.total != null
                    ? `${worker.last_run.processed ?? 0}/${worker.last_run.total}`
                    : '',
                  worker.last_run.reported_at ? formatTime(worker.last_run.reported_at) : '',
                ]
                  .filter(Boolean)
                  .join(' · ')
              : '暂无记录'}
          </Fact>
          {worker.revoked_at ? <Fact label="解除配对">{formatTime(worker.revoked_at)}</Fact> : null}
          {worker.archived_at ? <Fact label="移除时间">{formatTime(worker.archived_at)}</Fact> : null}
        </dl>

        <section className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="heading-xs">当前分配</h3>
            <Link
              to={`/assignments?worker=${encodeURIComponent(worker.id)}&from=devices`}
              className="text-xs text-default underline-offset-2 hover:underline"
            >
              查看该设备分配记录
            </Link>
          </div>
          {assignments.length === 0 ? (
            <p className="rounded-lg bg-surface-secondary px-3 py-2 text-xs text-secondary">没有占用中的门店。</p>
          ) : (
            <ul className="max-h-56 space-y-1 overflow-y-auto">
              {assignments.map((assignment) => {
                const store = formatStore(assignment.store)
                const meta = assignmentStatusOf(assignment.status)
                return (
                  <li key={assignment.id} className="flex items-center justify-between gap-3 rounded-lg px-2 py-1.5 hover:bg-secondary-ghost-hover">
                    <span className="min-w-0">
                      <span className="block truncate text-sm text-default">{store.name || assignment.store}</span>
                      <span className="block truncate text-2xs text-tertiary">
                        {[store.code, assignment.batch_name, assignment.revision].filter(Boolean).join(' · ')}
                      </span>
                    </span>
                    <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
                  </li>
                )
              })}
            </ul>
          )}
        </section>

        <section className="space-y-3">
          <h3 className="heading-xs">设备信息</h3>
          <div className="space-y-1.5">
            <label className="block text-sm text-default" htmlFor="worker-name">
              设备名称
            </label>
            <Input
              id="worker-name"
              value={name}
              maxLength={64}
              disabled={mutation.isPending || Boolean(worker.archived_at)}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <label className="block text-sm text-default" htmlFor="worker-description">
              备注
            </label>
            <Textarea
              id="worker-description"
              rows={3}
              maxLength={500}
              value={description}
              disabled={mutation.isPending || Boolean(worker.archived_at)}
              placeholder="例如：一楼前台电脑、负责城东片区"
              onChange={(event) => setDescription(event.target.value)}
            />
            <p className="text-xs text-tertiary">{description.length}/500</p>
          </div>
          {formError ? <Alert color="danger" variant="soft" title={formError} /> : null}
          {mutation.isError ? <Alert color="danger" variant="soft" title={errorMessage(mutation.error)} /> : null}
          <div className="flex justify-end">
            <Button
              color="primary"
              size="sm"
              disabled={!dirty || mutation.isPending || Boolean(worker.archived_at)}
              onClick={save}
            >
              {mutation.isPending ? '保存中…' : '保存修改'}
            </Button>
          </div>
        </section>
      </div>
    </AppDrawer>
  )
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-tertiary">{label}</dt>
      <dd className="mt-0.5 text-default">{children}</dd>
    </div>
  )
}
