import { useQuery } from '@tanstack/react-query'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { errorMessage } from '../../api/client'
import { fetchActiveTasks } from './api'
import { KIND_LABEL, formatTime, statusOf } from './format'
import type { ActiveTask } from './types'

interface ActiveTasksPanelProps {
  onOpen: (task: ActiveTask) => void
}

/**
 * 运行面板：轮询 /api/tasks/active，页面隐藏时 TanStack 自动停表，
 * 重新可见立即补一次。素材任务与桌面运行都使用同一份活动查询。
 */
export function ActiveTasksPanel({ onOpen }: ActiveTasksPanelProps) {
  const query = useQuery({
    queryKey: ['tasks-active'],
    queryFn: ({ signal }) => fetchActiveTasks(signal),
    refetchInterval: 5_000,
    staleTime: 4_000,
  })

  if (query.isError) {
    return (
      <div className="flex items-center justify-between gap-3 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">
        <span>运行中任务读取失败：{errorMessage(query.error)}</span>
        <Button color="secondary" variant="outline" size="sm" onClick={() => void query.refetch()}>
          重试
        </Button>
      </div>
    )
  }

  if (query.isPending) {
    return <StatePanel variant="loading" title="正在读取运行中任务…" />
  }

  if (!query.data.tasks.length) {
    return (
      <div className="rounded-xl border border-dashed border-shell-line/60 px-4 py-4 text-sm text-shell-muted">
        当前没有运行中的任务。开始一次采集或素材分析后，进度会显示在这里。
      </div>
    )
  }

  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {query.data.tasks.map((task) => {
        const status = statusOf(task.status)
        const total = Number(task.progress?.total) || 0
        const done = Number(task.progress?.done) || 0
        const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0
        return (
          <article
            key={`${task.source}:${task.id}`}
            className="app-panel flex flex-col gap-3 p-4"
            aria-label={`活动任务 ${task.title}`}
          >
            <header className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h3 className="truncate text-sm font-semibold" title={task.title}>
                  {task.title || task.id}
                </h3>
                <p className="mt-0.5 text-xs text-shell-muted">
                  {KIND_LABEL[task.source] ?? task.source} · 更新于 {formatTime(task.updated_at)}
                </p>
              </div>
              <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
            </header>
            <div>
              <div className="mb-1 flex justify-between text-xs text-shell-muted">
                <span>
                  已处理 {done} / {total}
                </span>
                <span>{percent}%</span>
              </div>
              <div
                className="h-1.5 overflow-hidden rounded-full bg-shell-700"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={total}
                aria-valuenow={done}
                aria-label="任务进度"
              >
                <div className="h-full rounded-full bg-accent-400 transition-[width]" style={{ width: `${percent}%` }} />
              </div>
            </div>
            <div className="flex justify-end">
              <Button color="secondary" variant="outline" size="sm" onClick={() => onOpen(task)}>
                查看详情
              </Button>
            </div>
          </article>
        )
      })}
    </div>
  )
}
