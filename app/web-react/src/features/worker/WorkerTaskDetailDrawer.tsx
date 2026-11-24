import { useEffect, useMemo, useState } from 'react'
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDrawer } from '../../components/AppDrawer'
import { CellText } from '../../components/CellText'
import { ImagePreview } from '../../components/ImagePreview'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { errorMessage } from '../../api/client'
import { fetchTaskActions, fetchTaskArtifacts, type WorkerAction } from './api'
import { formatStore, formatTime, stateMetaOf } from './format'
import type { WorkerTask } from './types'

interface WorkerTaskDetailDrawerProps {
  task: WorkerTask | null
  debugView: boolean
  onOpenChange: (open: boolean) => void
}

const ACTION_LABELS: Record<string, string> = {
  ocr: '本地文字识别', model: '视觉模型判断', page_check: '检查页面变化',
  wechat_marker_check: '匹配本轮采集标记', click: '点击', double_click: '双击',
  right_click: '右击', noop: '等待', screenshot: '截图', scroll_capture: '滚动截图',
  resize: '调整窗口', discover: '查找窗口', task_result: '任务结果',
}
const RESULT_LABELS: Record<string, string> = { ok: '成功', error: '失败', unknown: '结果未知', running: '尚无结果' }
const EVIDENCE_KEYS = [
  ['before', '点击前画面'], ['after', '点击后画面'],
  ['screenshot_evidence', '失败时画面'], ['frame_evidence', '关联画面'],
] as const

/** 本机任务详情复用受控文件接口；不会向界面回显素材或输出目录的绝对路径。 */
export function WorkerTaskDetailDrawer({ task, debugView, onOpenChange }: WorkerTaskDetailDrawerProps) {
  const [preview, setPreview] = useState<{ src: string; alt: string } | null>(null)
  const open = Boolean(task)
  const taskId = task?.id ?? ''
  const store = task ? formatStore(task.source_name) : null
  const meta = task ? stateMetaOf(task) : null
  const artifactsQuery = useQuery({
    queryKey: ['worker-task-artifacts', taskId],
    queryFn: ({ signal }) => fetchTaskArtifacts(taskId, signal),
    enabled: open,
    retry: false,
  })
  const actionsQuery = useInfiniteQuery({
    queryKey: ['worker-task-actions', taskId],
    queryFn: ({ pageParam, signal }) => fetchTaskActions(taskId, pageParam, signal),
    initialPageParam: null as number | null,
    getNextPageParam: (lastPage) => lastPage.actions.length === 100 ? lastPage.actions.at(-1)?.id : undefined,
    enabled: open && debugView,
    retry: false,
  })
  const actions = useMemo(
    () => [...(actionsQuery.data?.pages ?? []).flatMap((page) => page.actions)]
      .sort((left, right) => left.started_at.localeCompare(right.started_at) || left.id - right.id),
    [actionsQuery.data],
  )
  const runs = useMemo(() => {
    const unique = new Map<string, string>()
    for (const action of actions) {
      if (/^[0-9a-f]{32}$/.test(action.run_id) && !unique.has(action.run_id)) unique.set(action.run_id, action.started_at)
    }
    return [...unique].map(([id, startedAt]) => ({ id, startedAt }))
  }, [actions])
  const images = (artifactsQuery.data?.files ?? []).filter((file) => /^page-\d+\.png$/i.test(file))
  // 证据图与已发布截图合并为一个序列，打开后可连续浏览。
  const previewItems = task
    ? [
        { src: `/api/tasks/${encodeURIComponent(task.id)}/source`, alt: '原始二维码' },
        ...(task.reject_evidence
          ? [{ src: `/api/rejections/tasks/${encodeURIComponent(task.id)}/evidence`, alt: '不合格判断证据' }]
          : []),
        ...(['needs_review', 'needs_retry', 'error', 'blocked', 'failed'].includes(task.status)
          ? [{ src: `/api/tasks/${encodeURIComponent(task.id)}/last-frame`, alt: '最近采集画面' }]
          : []),
        ...images.map((file) => ({
          src: `/api/artifact/${encodeURIComponent(task.id)}/${encodeURIComponent(file)}`,
          alt: file,
        })),
      ]
    : []

  if (!task || !store || !meta) return null
  const statusDescription = [task.message, task.reject_label].filter((value): value is string => Boolean(value)).map(safeEvidenceText).join(' · ') || '尚无运行说明'

  return (
    <AppDrawer
      open={open}
      onOpenChange={onOpenChange}
      title={store.name || task.source_name || '未命名门店'}
      description={[store.code, task.batch_name, task.version ? `v${task.version}` : ''].filter(Boolean).join(' · ')}
      footer={<p className="text-xs text-secondary">任务 ID：<CellText copyable copyLabel="任务 ID" mono>{task.id}</CellText></p>}
    >
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
          {task.updated_at ? <span className="text-xs text-secondary">最近更新 {formatTime(task.updated_at)}</span> : null}
        </div>
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-secondary">{statusDescription}</p>

        <section className="space-y-2">
          <h3 className="heading-xs">采集证据</h3>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <EvidenceImage key={`${task.id}-source`} src={`/api/tasks/${encodeURIComponent(task.id)}/source`} alt="原始二维码" onPreview={setPreview} />
            {task.reject_evidence ? (
              <EvidenceImage key={`${task.id}-rejection`} src={`/api/rejections/tasks/${encodeURIComponent(task.id)}/evidence`} alt="不合格判断证据" onPreview={setPreview} />
            ) : null}
            {['needs_review', 'needs_retry', 'error', 'blocked', 'failed'].includes(task.status) ? (
              <EvidenceImage key={`${task.id}-frame`} src={`/api/tasks/${encodeURIComponent(task.id)}/last-frame`} alt="最近采集画面" onPreview={setPreview} />
            ) : null}
          </div>
        </section>

        <section className="space-y-2">
          <h3 className="heading-xs">已发布截图</h3>
          {artifactsQuery.isError ? (
            <Alert color="danger" variant="soft" title="截图清单读取失败" description={errorMessage(artifactsQuery.error)} />
          ) : artifactsQuery.isPending ? (
            <StatePanel variant="loading" title="正在读取截图清单…" />
          ) : images.length ? (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              {images.map((file) => {
                const url = `/api/artifact/${encodeURIComponent(task.id)}/${encodeURIComponent(file)}`
                return <EvidenceImage key={file} src={url} alt={file} onPreview={setPreview} />
              })}
            </div>
          ) : <p className="text-xs text-tertiary">没有已发布截图。</p>}
        </section>

        {debugView ? (
          <section className="space-y-3 border-t border-subtle pt-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h3 className="heading-xs">动作时间线</h3>
                <p className="mt-1 text-xs text-secondary">按发生时间从早到晚排列；调试细节仅在调试视图开启时读取。</p>
              </div>
              <Button color="secondary" variant="outline" size="sm" onClick={() => void actionsQuery.refetch()} disabled={actionsQuery.isFetching}>
                刷新记录
              </Button>
            </div>
            {runs.length ? (
              <div className="flex flex-wrap items-center gap-2 rounded-lg bg-surface-secondary p-3">
                <RunExportControl runs={runs} />
              </div>
            ) : null}
            {actionsQuery.isError ? (
              <Alert color="danger" variant="soft" title="动作记录读取失败" description={errorMessage(actionsQuery.error)} />
            ) : actionsQuery.isPending ? (
              <StatePanel variant="loading" title="正在读取动作记录…" />
            ) : actions.length ? (
              <ol className="space-y-2">
                {actions.map((action) => <ActionItem key={action.id} action={action} />)}
              </ol>
            ) : <p className="text-xs text-tertiary">暂无动作记录。</p>}
            {actionsQuery.hasNextPage ? (
              <Button color="secondary" variant="outline" size="sm" disabled={actionsQuery.isFetchingNextPage} onClick={() => void actionsQuery.fetchNextPage()}>
                {actionsQuery.isFetchingNextPage ? '正在读取…' : '查看更早的动作'}
              </Button>
            ) : null}
          </section>
        ) : null}
      </div>
      <ImagePreview
        open={Boolean(preview)}
        src={preview?.src ?? ''}
        alt={preview?.alt ?? ''}
        {...(preview ? { caption: preview.alt } : {})}
        items={previewItems}
        initialIndex={Math.max(0, previewItems.findIndex((item) => item.src === preview?.src))}
        onOpenChange={(next) => { if (!next) setPreview(null) }}
      />
    </AppDrawer>
  )
}

function EvidenceImage({ src, alt, onPreview }: { src: string; alt: string; onPreview: (value: { src: string; alt: string }) => void }) {
  const [failed, setFailed] = useState(false)
  if (failed) return <p className="rounded-lg border border-subtle px-3 py-5 text-center text-xs text-tertiary">{alt}暂不可读取</p>
  return (
    <button type="button" onClick={() => onPreview({ src, alt })} className="overflow-hidden rounded-lg border border-subtle bg-surface-secondary text-left">
      <img src={src} alt={alt} className="aspect-[4/3] w-full object-contain" onError={() => setFailed(true)} />
      <span className="block truncate px-2 py-1 text-2xs text-tertiary">{alt}</span>
    </button>
  )
}

function RunExportControl({ runs }: { runs: { id: string; startedAt: string }[] }) {
  const [selected, setSelected] = useState(runs[0]?.id ?? '')
  useEffect(() => {
    if (!runs.some((run) => run.id === selected)) setSelected(runs[0]?.id ?? '')
  }, [runs, selected])
  const valid = runs.some((run) => run.id === selected)
  return valid ? (
    <>
      <label className="text-xs text-secondary" htmlFor="worker-debug-run">导出调试包</label>
      <div className="min-w-0 flex-1">
        <Select
          id="worker-debug-run"
          aria-label="要导出调试包的运行"
          value={selected}
          size="xs"
          options={runs.map((run) => ({
            value: run.id,
            label: `${formatTime(run.startedAt)} · ${run.id.slice(0, 8)}`,
          }))}
          onChange={(option) => setSelected(option.value)}
        />
      </div>
      <a className="inline-flex min-h-8 items-center rounded-md border border-subtle px-2 text-xs text-default hover:bg-secondary-ghost-hover" href={`/api/debug/runs/${encodeURIComponent(selected)}/export`}>
        下载调试包
      </a>
    </>
  ) : null
}

function ActionItem({ action }: { action: WorkerAction }) {
  const details = action.details ?? {}
  const evidence = EVIDENCE_KEYS.flatMap(([key, label]) => {
    const name = details[key]
    return typeof name === 'string' && /^[0-9a-f]{32}\.png$/.test(name) ? [{ name, label }] : []
  })
  const info = [
    action.stage,
    `运行 ${action.run_id.slice(0, 8)}`,
    action.attempt_id ? `尝试 ${action.attempt_id.slice(0, 8)}` : '',
    typeof details.target === 'string' ? safeEvidenceText(details.target) : '',
    typeof details.error_type === 'string' ? details.error_type : '',
  ].filter(Boolean)
  const state = action.debug_state === 'expired' || details.debug_state === 'expired' ? '调试详情已清理'
    : action.debug_state === 'pending' || details.debug_state === 'pending' ? '调试记录写入中'
      : action.debug_state === 'dropped' || details.debug_state === 'dropped' ? '部分低优先级记录因容量限制未保存' : ''
  return (
    <li className="rounded-lg border border-subtle p-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <time className="text-secondary tabular-nums">{formatTime(action.started_at)}</time>
        <strong className="text-default">{action.step ? `第 ${action.step} 步 · ` : ''}{ACTION_LABELS[action.action] ?? action.action}</strong>
        <StatusBadge tone={action.result === 'error' ? 'danger' : action.result === 'ok' ? 'success' : 'secondary'}>{RESULT_LABELS[action.result] ?? action.result}</StatusBadge>
        {action.elapsed_ms !== null ? <span className="text-tertiary">{action.elapsed_ms} ms</span> : null}
      </div>
      {info.length ? <p className="mt-2 whitespace-pre-wrap break-words text-xs leading-relaxed text-secondary">{info.join(' · ')}</p> : null}
      {state ? <p className="mt-2 text-xs text-tertiary">{state}</p> : null}
      {evidence.length ? <div className="mt-2 flex flex-wrap gap-2">{evidence.map(({ name, label }) => <a key={`${name}-${label}`} className="text-xs text-default underline-offset-2 hover:underline" href={`/api/action-evidence/${encodeURIComponent(name)}`} target="_blank" rel="noreferrer">{label}</a>)}</div> : null}
    </li>
  )
}

function safeEvidenceText(value: string): string {
  return value
    .replace(/\b(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+/gi, '$1=[已隐藏]')
    .replace(/(?:[A-Za-z]:\\|\\\\[^\\\s]+\\[^\\\s]+|\/(?:Users|private|tmp|var|home|data)\/)[^\s"'<>]+/g, '[本机路径]')
}
