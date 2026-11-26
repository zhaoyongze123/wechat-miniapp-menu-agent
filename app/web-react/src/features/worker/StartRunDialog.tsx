import { useEffect, useMemo, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { AppDialog } from '../../components/AppDialog'
import { useUnsavedChanges } from '../../components/UnsavedChangesProvider'
import { errorMessage } from '../../api/client'
import { useProject } from '../projects/ProjectProvider'
import { startCapture } from './api'
import { CAPTURE_MODE_LABEL, formatStore } from './format'
import type { CaptureMode, WorkerTask } from './types'
import { START_RUN_LIMIT } from './types'

interface StartRunDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  tasks: WorkerTask[]
  defaultMode: CaptureMode
  /** 已有运行中的批次时不允许再开；由服务端最终裁决。 */
  blocked: boolean
  onStarted: (mode: CaptureMode, name: string) => void
}

function defaultName(): string {
  const now = new Date()
  const pad = (value: number) => String(value).padStart(2, '0')
  return `采集 ${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`
}

/** 开跑确认：展示门店范围、批次与模式，一次提交锁定任务，避免边跑边改选择。 */
export function StartRunDialog({ open, onOpenChange, tasks, defaultMode, blocked, onStarted }: StartRunDialogProps) {
  const { projectId } = useProject()
  const [name, setName] = useState('')
  const [mode, setMode] = useState<CaptureMode>(defaultMode)
  const [error, setError] = useState('')
  useUnsavedChanges(open && tasks.length > 0, '已选门店尚未开跑，切换项目后选择和开跑设置将清除。')

  useEffect(() => {
    if (!open) return
    setName(defaultName())
    setMode(defaultMode)
    setError('')
  }, [open, defaultMode])

  const batches = useMemo(() => {
    const counts = new Map<string, number>()
    for (const task of tasks) {
      const label = task.batch_name || '批次未记录'
      counts.set(label, (counts.get(label) ?? 0) + 1)
    }
    return [...counts].map(([label, count]) => `${label} ${count} 家`).join(' · ')
  }, [tasks])

  const preview = tasks.slice(0, 6).map((task) => formatStore(task.source_name).name)

  const mutation = useMutation({
    mutationFn: () => startCapture({ mode, taskIds: tasks.map((task) => task.id), name: name.trim(), projectId }),
    onSuccess: () => {
      onOpenChange(false)
      onStarted(mode, name.trim())
    },
    onError: (cause) => setError(errorMessage(cause)),
  })

  const tooMany = tasks.length > START_RUN_LIMIT
  const submit = () => {
    setError('')
    if (!tasks.length) return
    if (!name.trim()) {
      setError('请填写批次名称，便于在任务历史中定位。')
      return
    }
    mutation.mutate()
  }

  return (
    <AppDialog
      open={open}
      onOpenChange={onOpenChange}
      busy={mutation.isPending}
      title="开跑采集"
      widthClass="w-[min(640px,94vw)]"
      footer={
        <div className="flex justify-end gap-2">
          <Button color="secondary" variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={mutation.isPending}>
            取消
          </Button>
          <Button
            color="primary"
            size="sm"
            onClick={submit}
            disabled={mutation.isPending || !tasks.length || blocked || tooMany}
          >
            {mutation.isPending ? '正在启动…' : '开始采集'}
          </Button>
        </div>
      }
    >
      <div className="space-y-4">
        {blocked ? (
          <Alert color="warning" variant="soft" title="已有批次在运行" description="请先暂停并结束当前批次，再开始新的采集。" />
        ) : null}
        {tooMany ? (
          <Alert color="danger" variant="soft" title={`一次最多开跑 ${START_RUN_LIMIT} 家`} description="请减少选择后重试。" />
        ) : null}
        <label className="block space-y-2">
          <span className="text-xs font-medium text-secondary">批次名称</span>
          <Input value={name} maxLength={80} onChange={(event) => setName(event.target.value)} aria-label="批次名称" />
        </label>
        <div className="space-y-2">
          <span className="text-xs font-medium text-secondary">采集模式</span>
          <div className="grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="采集模式">
            {(['auto', 'manual'] as const).map((value) => (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={mode === value}
                onClick={() => setMode(value)}
                className={[
                  'rounded-lg border px-3 py-2 text-left text-sm transition-colors',
                  mode === value
                    ? 'border-primary-solid bg-secondary-soft text-default'
                    : 'border-subtle text-secondary hover:bg-secondary-ghost-hover',
                ].join(' ')}
              >
                <span className="block font-medium">{CAPTURE_MODE_LABEL[value]}</span>
                <span className="mt-0.5 block text-xs text-secondary">
                  {value === 'auto' ? 'Agent 自动操作微信，逐店截图并保存' : '人工操作微信，程序负责截图与提交'}
                </span>
              </button>
            ))}
          </div>
        </div>
        <div className="rounded-lg border border-subtle bg-surface-secondary px-3 py-2 text-xs text-secondary">
          <p>批次：{batches || '—'}</p>
          <p className="mt-1">
            门店：{preview.join('、')}
            {tasks.length > preview.length ? ` 等 ${tasks.length} 家` : ''}
          </p>
        </div>
        {error ? <Alert color="danger" variant="soft" title={error} /> : null}
      </div>
    </AppDialog>
  )
}
