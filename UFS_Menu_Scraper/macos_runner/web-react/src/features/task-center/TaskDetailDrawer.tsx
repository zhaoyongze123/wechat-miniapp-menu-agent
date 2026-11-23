import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Badge } from '@openai/apps-sdk-ui/components/Badge'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { AppDrawer } from '../../components/AppDrawer'
import { AppDataTable } from '../../components/AppDataTable'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { fetchTaskDetail, renameCaptureRun } from './api'
import { KIND_LABEL, formatTime, shortTime, statusOf } from './format'
import type { HistoryRecord, TaskDetail } from './types'

interface Selection {
  kind: string
  id: string
}

interface TaskDetailDrawerProps {
  selection: Selection | null
  summary: HistoryRecord | null
  onClose: () => void
  onRenamed: (record: HistoryRecord) => void
}

export function TaskDetailDrawer({ selection, summary, onClose, onRenamed }: TaskDetailDrawerProps) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [renaming, setRenaming] = useState(false)
  const [name, setName] = useState('')
  const [renameError, setRenameError] = useState('')

  const detailQuery = useQuery({
    queryKey: ['task-detail', selection?.kind ?? '', selection?.id ?? ''],
    queryFn: ({ signal }) => fetchTaskDetail(selection?.kind ?? '', selection?.id ?? '', signal),
    enabled: selection !== null,
    staleTime: 5_000,
  })

  const detail = detailQuery.data

  useEffect(() => {
    setRenaming(false)
    setName('')
    setRenameError('')
  }, [selection?.kind, selection?.id])

  const renameMutation = useMutation({
    mutationFn: (value: string) => renameCaptureRun(selection?.id ?? '', value),
    onSuccess: (updated) => {
      if (selection) {
        queryClient.setQueryData<TaskDetail>(['task-detail', selection.kind, selection.id], (current) => {
          if (!current) return current
          const next: TaskDetail = { ...current }
          if (updated.title !== undefined) next.title = updated.title
          if (updated.name !== undefined) next.name = updated.name
          return next
        })
      }
      onRenamed(updated)
      setRenaming(false)
      setRenameError('')
      toast.push('任务名称已更新', 'success')
    },
    onError: (error: unknown) => {
      // 保存失败保留原输入，方便直接修改后重试。
      setRenameError(errorMessage(error))
    },
  })

  const title = detail?.title || summary?.title || detail?.id || selection?.id || '任务详情'
  const status = statusOf(detail?.status ?? summary?.status)
  const canRename = (detail?.kind ?? selection?.kind) === 'capture'

  function startRename() {
    setName(detail?.name || detail?.title || '')
    setRenameError('')
    setRenaming(true)
  }

  function submitRename() {
    const value = name.trim()
    if (!value) {
      setRenameError('任务名称不能为空')
      return
    }
    if (value.length > 64) {
      setRenameError('任务名称不能超过 64 个字符')
      return
    }
    renameMutation.mutate(value)
  }

  return (
    <AppDrawer
      open={selection !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
      title={title}
      description={`${KIND_LABEL[detail?.kind ?? selection?.kind ?? ''] ?? ''} · ${detail?.id ?? selection?.id ?? ''}`}
      headerActions={
        canRename ? (
          <Button color="secondary" variant="outline" size="sm" onClick={startRename} disabled={!detail || renaming}>
            重命名
          </Button>
        ) : null
      }
    >
      {detailQuery.isPending ? (
        <StatePanel variant="loading" title="正在读取任务详情…" />
      ) : detailQuery.isError ? (
        <StatePanel
          variant="error"
          title="详情读取失败"
          description={errorMessage(detailQuery.error)}
          action={
            <Button color="secondary" variant="outline" size="sm" onClick={() => void detailQuery.refetch()}>
              重试
            </Button>
          }
        />
      ) : detail ? (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
            <span className="text-secondary">开始于 {formatTime(detail.created_at)}</span>
            {detail.updated_at && detail.updated_at !== detail.created_at ? (
              <span className="text-secondary">更新于 {formatTime(detail.updated_at)}</span>
            ) : null}
          </div>

          {renaming ? (
            <div className="rounded-xl bg-surface-secondary p-3">
              <label className="mb-1 block text-xs text-secondary" htmlFor="task-rename-input">
                任务名称（1–64 个字符）
              </label>
              <div className="flex flex-wrap items-center gap-2">
                <div className="min-w-[220px] flex-1">
                  <Input
                    id="task-rename-input"
                    value={name}
                    maxLength={64}
                    invalid={Boolean(renameError)}
                    placeholder="任务名称"
                    onChange={(event) => {
                      setName(event.target.value)
                      setRenameError('')
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') submitRename()
                      if (event.key === 'Escape') setRenaming(false)
                    }}
                  />
                </div>
                <Button color="primary" size="sm" onClick={submitRename} disabled={renameMutation.isPending}>
                  {renameMutation.isPending ? '保存中…' : '保存'}
                </Button>
                <Button color="secondary" variant="ghost" size="sm" onClick={() => setRenaming(false)}>
                  取消
                </Button>
              </div>
              {renameError ? <Alert color="danger" variant="soft" title={renameError} className="mt-2" /> : null}
            </div>
          ) : null}

          {detail.kind === 'capture' ? (
            <>
              <CountChips detail={detail} />
              {detail.message ? <p className="text-sm text-secondary">{detail.message}</p> : null}
              <AppDataTable
                caption="本轮逐店采集结果"
                rows={detail.tasks}
                rowKey={(row) => row.task_id}
                minWidthClass="min-w-[600px]"
                emptyState={
                  <StatePanel
                    variant="empty"
                    title="没有门店明细"
                    description="旧记录可能未保存当轮逐店结果。"
                  />
                }
                columns={[
                  {
                    id: 'store',
                    header: '门店',
                    cell: (row) => <span>{row.store_name || row.task_id}</span>,
                  },
                  {
                    id: 'batch',
                    header: '批次 / 版本',
                    cell: (row) => (
                      <span className="text-secondary">
                        {[row.batch_name, row.version].filter(Boolean).join(' · ') || '—'}
                      </span>
                    ),
                  },
                  {
                    id: 'result',
                    header: '结果',
                    cell: (row) => row.label || row.status || '未处理',
                  },
                  {
                    id: 'message',
                    header: '说明',
                    cell: (row) => (
                      <span className="text-secondary">{row.reject_reason || row.message || '—'}</span>
                    ),
                  },
                ]}
              />
            </>
          ) : (
            <>
              <CountChips detail={detail} />
              {detail.message ? <p className="text-sm text-secondary">{detail.message}</p> : null}
              {detail.events.length ? (
                <ol className="space-y-2">
                  {detail.events.map((event, index) => (
                    <li
                      key={`${event.time ?? ''}-${index}`}
                      className="flex gap-3 rounded-xl bg-surface-secondary px-3 py-2 text-sm"
                    >
                      <time className="font-mono shrink-0 text-xs text-tertiary">
                        {shortTime(event.time) || '—'}
                      </time>
                      <span className={event.level === 'error' ? 'text-danger' : ''}>
                        {event.message || '—'}
                      </span>
                    </li>
                  ))}
                </ol>
              ) : (
                <StatePanel variant="empty" title="没有更多明细" />
              )}
            </>
          )}
        </div>
      ) : null}
    </AppDrawer>
  )
}

function CountChips({ detail }: { detail: TaskDetail }) {
  const counts = detail.counts ?? {}
  const chips =
    detail.kind === 'capture'
      ? [
          `已处理 ${counts.recorded ?? 0} / ${counts.total ?? 0}`,
          `已截图待复核 ${counts.done ?? 0}`,
          `待检查 ${counts.attention ?? 0}`,
          `不合格 ${counts.rejected ?? 0}`,
        ]
      : [`已分析 ${counts.done ?? 0} / ${counts.total ?? 0}`, `失败 ${counts.failed ?? 0}`]
  return (
    <div className="flex flex-wrap gap-2">
      {chips.map((chip) => (
        <Badge key={chip} color="secondary" variant="soft" size="sm">
          {chip}
        </Badge>
      ))}
      {detail.kind === 'capture' && counts.unknown ? (
        <Badge color="warning" variant="soft" size="sm">
          {counts.unknown} 家旧记录缺少本轮结果
        </Badge>
      ) : null}
    </div>
  )
}
