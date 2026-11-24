import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { errorMessage } from '../../api/client'
import { PageHeader } from '../../components/PageHeader'
import { ImagePreview } from '../../components/ImagePreview'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { useRole } from '../../app/RoleProvider'
import {
  clickAnnotation,
  completeAnnotation,
  decideAnnotation,
  fetchAnnotationHistory,
  pauseAnnotation,
  refreshAnnotation,
  rejectAnnotation,
  resumeAnnotation,
  retryAnnotation,
  skipAnnotation,
  startAnnotation,
  stopAnnotation,
} from './api'
import { ANNOTATION_STATE_KEY, ANNOTATION_TASKS_KEY, useAnnotationState, useAnnotationTasks } from './hooks'
import { AnnotationCanvas } from './AnnotationCanvas'
import { RejectDialog } from './RejectDialog'
import {
  annotationStatusLabel,
  annotationStatusTone,
  categoryLabel,
  categoryOptions,
  countTaskBuckets,
  decisionSourceLabel,
  effectText,
  effectTone,
  formatMapping,
  formatStepMapping,
  taskBucket,
  taskClaimable,
  taskDisplayName,
  taskStatusLabel,
  taskStatusTone,
} from './format'
import type { TaskBucket } from './format'
import type { AnnotationBox, AnnotationHistory, AnnotationState, AnnotationStep, AnnotationTask } from './types'

interface ConfirmRequest {
  title: string
  description: ReactNode
  confirmLabel: string
  tone?: 'primary' | 'danger'
  action: () => void
}

const FILTERS: { key: 'all' | TaskBucket; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'pending', label: '待处理' },
  { key: 'review', label: '待复核' },
  { key: 'captured', label: '已采集' },
  { key: 'annotated', label: '已标注' },
  { key: 'rejected', label: '不合格' },
]

function countOf(buckets: ReturnType<typeof countTaskBuckets>, key: 'all' | TaskBucket): number {
  return key === 'all' ? buckets.all : buckets[key]
}

export function AnnotationPage() {
  const role = useRole()
  if (role.loading) return <StatePanel variant="loading" title="正在确认当前角色…" />
  if (role.error) {
    return (
      <StatePanel
        variant="error"
        title="无法确认当前服务角色"
        description={role.error}
        action={
          <Button color="secondary" variant="outline" size="sm" onClick={role.reload}>
            重试
          </Button>
        }
      />
    )
  }
  if (role.role === 'hub') {
    return (
      <StatePanel
        variant="info"
        title="仅 Windows 采集端可人工标注"
        description="人工标注需要控制本机微信窗口并截取桌面画面；共享后台请到“采集电脑”查看设备与分配状态。"
      />
    )
  }
  return <AnnotationWorkbench />
}

function AnnotationWorkbench() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState<'all' | TaskBucket>('all')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [busyLabel, setBusyLabel] = useState('')
  const [deciding, setDeciding] = useState(false)
  const [box, setBox] = useState<AnnotationBox | null>(null)
  const [category, setCategory] = useState('')
  const [target, setTarget] = useState('')
  const [hardNegative, setHardNegative] = useState(false)
  const [note, setNote] = useState('')
  const [stepCursor, setStepCursor] = useState(0)
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null)
  const [rejectOpen, setRejectOpen] = useState(false)
  const [rejectError, setRejectError] = useState('')
  const [reviewTaskId, setReviewTaskId] = useState('')
  const [review, setReview] = useState<AnnotationHistory | null>(null)
  const [preview, setPreview] = useState<{ src: string; alt: string; caption?: string } | null>(null)
  const autoDecideKey = useRef('')

  const busy = Boolean(busyLabel)
  const stateQuery = useAnnotationState({ enabled: true, paused: busy || deciding })
  const tasksQuery = useAnnotationTasks(true)
  const state = stateQuery.data
  const tasks = tasksQuery.data ?? []
  const buckets = useMemo(() => countTaskBuckets(tasks), [tasks])
  const claimableIds = useMemo(() => tasks.filter(taskClaimable).map((task) => task.id), [tasks])
  const claimableKey = claimableIds.join('|')

  // 任务列表变化时剔除已不可标注的选择；默认不自动勾选，避免误开全部门店。
  useEffect(() => {
    setSelected((previous) => {
      const valid = new Set(claimableIds)
      return new Set([...previous].filter((id) => valid.has(id)))
    })
    // claimableKey 已覆盖列表内容变化。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [claimableKey])

  const frameKey = state?.frame ? `${state.session_id ?? ''}:${state.frame.name}` : ''
  useEffect(() => {
    setBox(null)
  }, [frameKey])

  const liveSteps = state?.current_steps ?? []
  const reviewSteps = review?.steps ?? []
  const steps = review ? reviewSteps : liveSteps
  const stepScope = review ? `review:${review.task?.id ?? ''}` : `live:${state?.session_id ?? ''}:${state?.task?.id ?? ''}`
  useEffect(() => {
    setStepCursor(Math.max(0, steps.length - 1))
    // 只在新门店、新会话或回看对象切换时重置游标。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepScope])

  const activeStep: AnnotationStep | undefined = steps[Math.min(stepCursor, Math.max(0, steps.length - 1))]

  const historyQuery = useQuery({
    queryKey: ['annotation', 'history', reviewTaskId],
    queryFn: ({ signal }) => fetchAnnotationHistory(reviewTaskId, signal),
    enabled: Boolean(reviewTaskId),
    staleTime: 0,
  })
  // 会话一旦开始，回看必须退出，避免与实时画面互相干扰。
  useEffect(() => {
    if (state?.active && review) setReview(null)
  }, [state?.active, review])
  useEffect(() => {
    if (historyQuery.data) {
      setReview(historyQuery.data)
      setReviewTaskId('')
    }
  }, [historyQuery.data])
  useEffect(() => {
    if (historyQuery.error) {
      toast.push(errorMessage(historyQuery.error), 'danger')
      setReviewTaskId('')
    }
  }, [historyQuery.error, toast])

  const applyState = (next: AnnotationState) => queryClient.setQueryData(ANNOTATION_STATE_KEY, next)

  const run = async (label: string, action: () => Promise<AnnotationState>, refreshTasks = false): Promise<boolean> => {
    setBusyLabel(label)
    try {
      const next = await action()
      applyState(next)
      if (refreshTasks) await queryClient.invalidateQueries({ queryKey: ANNOTATION_TASKS_KEY })
      if (next.active) setReview(null)
      return true
    } catch (error) {
      toast.push(errorMessage(error), 'danger')
      return false
    } finally {
      setBusyLabel('')
    }
  }

  // 新画面没有判断时自动请求一次；失败后由“生成建议”手动重试。
  useEffect(() => {
    if (!state || review) return
    if (!state.active || state.paused) return
    if (!state.frame || state.decision) return
    if (state.status !== 'ready' && state.status !== 'clicked') return
    const key = `${state.session_id ?? ''}:${state.frame.name}`
    if (autoDecideKey.current === key) return
    autoDecideKey.current = key
    setDeciding(true)
    decideAnnotation()
      .then((next) => applyState(next))
      .catch((error) => toast.push(errorMessage(error), 'danger'))
      .finally(() => setDeciding(false))
    // applyState 只依赖稳定的 queryClient。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state?.session_id, state?.frame?.name, state?.decision, state?.status, state?.active, state?.paused, review])

  const visibleTasks = useMemo(
    () => (filter === 'all' ? tasks : tasks.filter((task) => taskBucket(task) === filter)),
    [tasks, filter],
  )
  const visibleClaimable = visibleTasks.filter(taskClaimable).map((task) => task.id)
  const allVisibleSelected = visibleClaimable.length > 0 && visibleClaimable.every((id) => selected.has(id))

  const toggle = (id: string, checked: boolean) => {
    setSelected((previous) => {
      const next = new Set(previous)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  const toggleAll = () => {
    setSelected((previous) => {
      const next = new Set(previous)
      if (allVisibleSelected) visibleClaimable.forEach((id) => next.delete(id))
      else visibleClaimable.forEach((id) => next.add(id))
      return next
    })
  }

  const start = () => {
    const ids = [...selected]
    if (!ids.length) {
      toast.push('请先选择需要标注的门店', 'info')
      return
    }
    setConfirm({
      title: '开始人工标注',
      description: (
        <>
          将依次打开选中的 {ids.length} 家门店。请确认微信已停留在“文件传输助手”，并保持屏幕解锁。
        </>
      ),
      confirmLabel: '开始标注',
      action: () => {
        void run('正在打开第一家门市…', () => startAnnotation(ids)).then((ok) => {
          if (ok) setSelected(new Set())
        })
      },
    })
  }

  const submitClick = () => {
    if (!box) {
      toast.push('请先在截图上框选目标位置', 'info')
      return
    }
    if (!category) {
      toast.push('请选择标注品类', 'info')
      return
    }
    const centerX = box.x + box.width / 2
    const centerY = box.y + box.height / 2
    setConfirm({
      title: '确认点击位置',
      description: (
        <>
          将在门店 <span className="font-medium">{state?.task?.source_name || '当前门店'}</span> 的 (
          {Math.round(centerX)}, {Math.round(centerY)}) 处点击，并记录前后截图。
        </>
      ),
      confirmLabel: '确认点击',
      action: () => {
        void run('正在点击并截图…', () =>
          clickAnnotation({
            box,
            category,
            target: target.trim() || undefined,
            hard_negative: hardNegative ? true : undefined,
            note: note.trim() || undefined,
          }),
        )
      },
    })
  }

  const complete = () => {
    setConfirm({
      title: '完成当前门店标注',
      description: (
        <>
          当前门店的 {liveSteps.length} 个步骤将归档，并自动进入下一家。归档后仍可在门店列表“查看效果”。
        </>
      ),
      confirmLabel: '完成并继续',
      action: () => {
        void run('正在归档标注…', completeAnnotation, true)
      },
    })
  }

  const skip = () => {
    setConfirm({
      title: '跳过当前门店',
      description: <>将跳过当前门店，本次标注不会归档；之后可在任务中心重新处理。</>,
      confirmLabel: '跳过',
      action: () => {
        void run('正在跳过…', () => skipAnnotation(''), true)
      },
    })
  }

  const stop = () => {
    setConfirm({
      title: '结束标注会话',
      description: <>将结束本次人工标注；已归档的门店记录保留，未完成的门店不归档。</>,
      confirmLabel: '结束会话',
      action: () => {
        void run('正在结束会话…', stopAnnotation, true)
      },
    })
  }

  const reject = (reason: string, detail: string) => {
    setRejectError('')
    void (async () => {
      const ok = await run('正在标记不合格…', () => rejectAnnotation(reason, detail), true)
      if (ok) setRejectOpen(false)
      else setRejectError('标记失败，请重试')
    })()
  }

  const adoptDecision = () => {
    const decision = state?.decision
    if (!decision) return
    if (decision.box_px) setBox(decision.box_px)
    if (decision.target_label) setTarget(decision.target_label)
  }

  const applySuggestion = () => {
    const suggestion = state?.suggestion
    if (!suggestion) return
    if (suggestion.category) setCategory(suggestion.category)
    if (suggestion.target) setTarget(suggestion.target)
    if (suggestion.box_px) setBox(suggestion.box_px)
  }

  const openReview = (task: AnnotationTask) => {
    if (state?.active) {
      toast.push('请先结束当前标注，再回看已标注门店', 'info')
      return
    }
    setReviewTaskId(task.id)
    setStepCursor(0)
  }

  const exitReview = () => {
    setReview(null)
    setStepCursor(0)
  }

  const canvasUrl = review
    ? activeStep?.before?.url ?? activeStep?.after?.url ?? ''
    : state?.frame?.url ?? ''
  const canvasBox = review ? activeStep?.box_px ?? null : box
  const suggestionBox = review ? null : state?.suggestion?.box_px ?? null
  const canvasRevision = review ? `${review.session_id ?? ''}:${activeStep?.index ?? 0}` : state?.frame?.name ?? ''
  const mappingText = review ? formatStepMapping(activeStep) : formatMapping(box, state?.frame)
  const statusText = review ? '已标注 · 回看' : annotationStatusLabel(state?.status)
  const statusTone = review ? 'success' : annotationStatusTone(state?.status)
  const statusIsDanger = !review && ['error', 'open_failed', 'close_failed'].includes(state?.status ?? '')
  const active = Boolean(state?.active)
  const paused = Boolean(state?.paused)
  const formDisabled = Boolean(review) || !active || paused || busy
  const currentTask = review?.task ?? state?.task
  const currentTaskId = currentTask?.id ?? ''

  return (
    <section className="space-y-6" aria-labelledby="annotation-heading">
      <PageHeader title="人工标注" headingId="annotation-heading" />

      {stateQuery.isPending ? <StatePanel variant="loading" title="正在读取标注状态…" /> : null}
      {stateQuery.isError ? (
        <StatePanel
          variant="error"
          title="标注状态读取失败"
          description={errorMessage(stateQuery.error)}
          action={
            <Button color="secondary" variant="outline" size="sm" onClick={() => void stateQuery.refetch()}>
              重试
            </Button>
          }
        />
      ) : null}

      {state ? (
        <div className="grid gap-4 xl:grid-cols-[340px_minmax(0,1fr)]">
          <aside className="space-y-3 self-start rounded-xl border border-subtle p-4" aria-label="待标注门店">
            <div className="flex items-center justify-between gap-2">
              <h2 className="heading-xs">待标注门店</h2>
              <span className="text-2xs text-tertiary">共 {buckets.all} 家</span>
            </div>

            <div className="flex flex-wrap gap-1.5" role="group" aria-label="按状态筛选门店">
              {FILTERS.map((item) => {
                const current = filter === item.key
                return (
                  <button
                    key={item.key}
                    type="button"
                    aria-pressed={current}
                    className={`rounded-full border px-2.5 py-1 text-2xs transition-colors ${
                      current
                        ? 'border-primary-solid bg-secondary-soft text-default'
                        : 'border-subtle text-secondary hover:bg-secondary-ghost-hover'
                    }`}
                    onClick={() => setFilter(item.key)}
                  >
                    {item.label} {countOf(buckets, item.key)}
                  </button>
                )
              })}
            </div>

            <Checkbox
              label={`全选当前分组（${visibleClaimable.length} 家可标注）`}
              checked={allVisibleSelected}
              disabled={!visibleClaimable.length || active}
              onCheckedChange={toggleAll}
            />

            <ul className="max-h-96 space-y-1 overflow-y-auto pr-1">
              {visibleTasks.slice(0, 300).map((task) => {
                const claimable = taskClaimable(task)
                const annotated = taskBucket(task) === 'annotated'
                return (
                  <li key={task.id} className="flex items-start gap-2 rounded-lg border border-subtle px-2 py-1.5">
                    <div className="min-w-0 flex-1 space-y-0.5">
                      <Checkbox
                        className="min-w-0"
                        checked={selected.has(task.id)}
                        disabled={!claimable || active || busy}
                        onCheckedChange={(checked) => toggle(task.id, checked)}
                        label={
                          <span className="block min-w-0 truncate text-sm text-default" title={taskDisplayName(task)}>
                            {taskDisplayName(task)}
                          </span>
                        }
                      />
                      <div className="flex min-w-0 items-center gap-2 pl-7">
                        <span className="min-w-0 flex-1 truncate text-2xs text-tertiary">{task.id}</span>
                        <span className="shrink-0">
                          <StatusBadge tone={taskStatusTone(task)}>{taskStatusLabel(task)}</StatusBadge>
                        </span>
                      </div>
                    </div>
                    {annotated ? (
                      <button
                        type="button"
                        className="shrink-0 text-2xs text-primary underline-offset-2 hover:underline"
                        onClick={() => openReview(task)}
                      >
                        查看效果
                      </button>
                    ) : null}
                  </li>
                )
              })}
              {visibleTasks.length === 0 ? (
                <li className="py-6 text-center text-xs text-tertiary">
                  {tasks.length === 0 ? '当前没有可标注的任务。请先在共享后台发布素材并分配门店。' : '该分组暂无门店'}
                </li>
              ) : null}
              {visibleTasks.length > 300 ? (
                <li className="py-2 text-center text-2xs text-tertiary">仅显示前 300 家，请用筛选缩小范围。</li>
              ) : null}
            </ul>

            <p className="text-2xs leading-relaxed text-tertiary">
              开始前请把微信停留在“文件传输助手”，并保持屏幕解锁；标注过程中不要手动切换窗口。
            </p>

            <div className="flex flex-wrap gap-2">
              <Button color="primary" size="sm" disabled={active || busy || !selected.size} onClick={start}>
                开始标注（{selected.size}）
              </Button>
              <Button color="danger" variant="outline" size="sm" disabled={!active || busy} onClick={stop}>
                结束会话
              </Button>
            </div>
          </aside>

          <div className="space-y-4">
            <section className="space-y-4 rounded-xl border border-subtle p-4" aria-label="标注画面">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="heading-xs">{review ? '标注回看' : '当前门店'}</h2>
                  <p className="mt-1 truncate text-sm text-default">
                    {currentTask?.source_name || currentTaskId || '尚未选择门店'}
                  </p>
                  <p className="text-2xs text-tertiary">
                    {currentTaskId ? `${currentTaskId} · ` : ''}
                    {active ? `第 ${state.index + 1} / ${state.total} 家` : review ? '只读' : '等待开始'}
                    {paused ? ' · 已暂停' : ''}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <StatusBadge tone={statusTone}>{statusText}</StatusBadge>
                  {deciding ? <StatusBadge tone="info">正在生成建议</StatusBadge> : null}
                  {review ? (
                    <Button color="secondary" variant="outline" size="sm" onClick={exitReview}>
                      退出回看
                    </Button>
                  ) : null}
                </div>
              </div>

              {!review && state.message ? (
                <p className={`text-sm ${statusIsDanger ? 'text-danger' : 'text-secondary'}`}>{state.message}</p>
              ) : null}
              {busyLabel ? (
                <p className="text-xs text-secondary" aria-live="polite">
                  {busyLabel}
                </p>
              ) : null}

              {canvasUrl ? (
                <>
                  <AnnotationCanvas
                    url={canvasUrl}
                    revision={canvasRevision}
                    box={canvasBox}
                    suggestionBox={suggestionBox}
                    readOnly={Boolean(review)}
                    onBoxChange={setBox}
                  />
                  <p className="text-2xs text-tertiary">{mappingText || '回看步骤缺少坐标记录'}</p>
                </>
              ) : (
                <div className="grid min-h-72 place-items-center rounded-lg border border-subtle bg-surface-secondary px-4 text-center text-sm text-secondary">
                  {review ? '该门店没有归档的标注步骤。' : '开始标注后，这里会显示微信当前画面。'}
                </div>
              )}

              {!review ? (
                <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                  <div className="space-y-3">
                    <div className="space-y-1">
                      <label className="text-xs text-secondary" htmlFor="annotation-category">
                        标注品类
                      </label>
                      <Select
                        aria-label="标注品类"
                        value={category}
                        options={[{ value: '', label: '请选择品类' }, ...categoryOptions(state.categories)]}
                        onChange={(option) => setCategory(option.value)}
                        disabled={formDisabled}
                      />
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-secondary" htmlFor="annotation-target">
                        目标名称
                      </label>
                      <Input
                        id="annotation-target"
                        value={target}
                        placeholder="例如：招牌牛肉面"
                        disabled={formDisabled}
                        onChange={(event) => setTarget(event.target.value)}
                      />
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-secondary" htmlFor="annotation-note">
                        备注
                      </label>
                      <Input
                        id="annotation-note"
                        value={note}
                        placeholder="可选，记录判断依据"
                        disabled={formDisabled}
                        onChange={(event) => setNote(event.target.value)}
                      />
                    </div>
                  </div>

                  <div className="space-y-2">
                    <Checkbox
                      label="硬负样本"
                      checked={hardNegative}
                      disabled={formDisabled}
                      onCheckedChange={setHardNegative}
                    />
                    <p className="text-2xs leading-relaxed text-tertiary">
                      仅记录“看似目标但不该点”的位置；误点或无变化不要勾选。
                    </p>
                    <div className="flex flex-wrap gap-2 pt-1">
                      <Button color="primary" size="sm" disabled={formDisabled || !box || !category} onClick={submitClick}>
                        确认点击
                      </Button>
                      <Button
                        color="secondary"
                        variant="outline"
                        size="sm"
                        disabled={formDisabled || deciding}
                        onClick={() => {
                          setDeciding(true)
                          void run('正在生成系统判断…', decideAnnotation)
                            .finally(() => setDeciding(false))
                        }}
                      >
                        生成建议
                      </Button>
                      <Button
                        color="secondary"
                        variant="outline"
                        size="sm"
                        disabled={formDisabled}
                        onClick={() => void run('正在刷新截图…', refreshAnnotation)}
                      >
                        刷新截图
                      </Button>
                    </div>
                  </div>
                </div>
              ) : (
                <dl className="grid gap-2 text-sm sm:grid-cols-2">
                  <div>
                    <dt className="text-2xs text-tertiary">品类</dt>
                    <dd>{categoryLabel(activeStep?.category)}</dd>
                  </div>
                  <div>
                    <dt className="text-2xs text-tertiary">目标</dt>
                    <dd>{activeStep?.target || '未填写'}</dd>
                  </div>
                  <div>
                    <dt className="text-2xs text-tertiary">样本类型</dt>
                    <dd>{activeStep?.hard_negative ? '硬负样本' : '正样本'}</dd>
                  </div>
                  <div>
                    <dt className="text-2xs text-tertiary">记录时间</dt>
                    <dd>{activeStep?.at || '—'}</dd>
                  </div>
                  {activeStep?.note ? (
                    <div className="sm:col-span-2">
                      <dt className="text-2xs text-tertiary">备注</dt>
                      <dd className="whitespace-pre-wrap">{activeStep.note}</dd>
                    </div>
                  ) : null}
                </dl>
              )}

              {!review && active ? (
                <div className="flex flex-wrap gap-2">
                  <Button color="secondary" variant="outline" size="sm" disabled={busy} onClick={() => void run(paused ? '正在恢复…' : '正在暂停…', paused ? resumeAnnotation : pauseAnnotation)}>
                    {paused ? '恢复标注' : '暂停'}
                  </Button>
                  <Button color="primary" variant="outline" size="sm" disabled={busy || !liveSteps.length} onClick={complete}>
                    完成标注
                  </Button>
                  <Button color="secondary" variant="outline" size="sm" disabled={busy} onClick={skip}>
                    跳过
                  </Button>
                  <Button
                    color="danger"
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    onClick={() => {
                      setRejectError('')
                      setRejectOpen(true)
                    }}
                  >
                    标记不合格
                  </Button>
                  {['error', 'open_failed', 'close_failed'].includes(state.status) ? (
                    <Button color="secondary" variant="outline" size="sm" disabled={busy} onClick={() => void run('正在重试…', retryAnnotation)}>
                      重试
                    </Button>
                  ) : null}
                </div>
              ) : null}

              <p className="text-2xs leading-relaxed text-tertiary">
                {review
                  ? '回看只读，不会修改已有标注。'
                  : active
                    ? '确认点击后会保存前后截图并判定画面变化；标记不合格会自动继续下一家。'
                    : '开始标注后会逐家打开门店，并在本页展示桌面截图与系统建议。'}
              </p>
            </section>

            {!review && state.suggestion ? (
              <section className="space-y-2 rounded-xl border border-subtle p-4" aria-label="系统建议">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h2 className="heading-xs">系统建议</h2>
                  {typeof state.suggestion.confidence === 'number' ? (
                    <StatusBadge tone="info">{Math.round(state.suggestion.confidence * 100)}%</StatusBadge>
                  ) : null}
                </div>
                <p className="text-sm text-secondary">
                  建议品类：{categoryLabel(state.suggestion.category)}
                  {state.suggestion.target ? ` · 目标：${state.suggestion.target}` : ''}
                </p>
                {state.suggestion.matched_text ? (
                  <p className="text-xs text-tertiary">匹配文本：{state.suggestion.matched_text}</p>
                ) : null}
                {state.suggestion.note ? <p className="text-xs text-secondary">{state.suggestion.note}</p> : null}
                {state.suggestion.error ? <Alert color="warning" variant="soft" title={state.suggestion.error} /> : null}
                <Button
                  color="secondary"
                  variant="outline"
                  size="sm"
                  disabled={formDisabled}
                  onClick={applySuggestion}
                >
                  应用建议{state.suggestion.box_px ? '（含位置）' : ''}
                </Button>
              </section>
            ) : null}

            {!review && state.decision ? (
              <section className="space-y-2 rounded-xl border border-subtle p-4" aria-label="系统判断">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h2 className="heading-xs">系统判断</h2>
                  <span className="text-2xs text-tertiary">来源：{decisionSourceLabel(state.decision)}</span>
                </div>
                <p className="text-sm text-default">
                  目标：{state.decision.target_label || '未命中目标'}
                  {state.decision.hit_stage ? ` · 命中阶段：${state.decision.hit_stage}` : ''}
                  {state.decision.action ? ` · 建议动作：${state.decision.action}` : ''}
                </p>
                {state.decision.method || state.decision.reason ? (
                  <p className="text-xs text-secondary">
                    {[state.decision.method, state.decision.reason].filter(Boolean).join(' · ')}
                  </p>
                ) : null}
                {state.decision.resolution_reason ? (
                  <p className="text-xs text-tertiary">{state.decision.resolution_reason}</p>
                ) : null}
                {state.decision.candidates?.length ? (
                  <div className="space-y-1">
                    <p className="text-2xs text-tertiary">候选目标</p>
                    <ul className="max-h-28 space-y-0.5 overflow-y-auto text-xs text-secondary">
                      {state.decision.candidates.slice(0, 8).map((candidate, index) => (
                        <li key={`${candidate.id ?? candidate.text ?? 'candidate'}-${index}`} className="truncate">
                          {candidate.text || candidate.id || '未命名'}
                          {candidate.kind ? ` · ${candidate.kind}` : ''}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                {state.decision.error ? <Alert color="warning" variant="soft" title={state.decision.error} /> : null}
                <Button
                  color="secondary"
                  variant="outline"
                  size="sm"
                  disabled={formDisabled || (!state.decision.box_px && !state.decision.target_label)}
                  onClick={adoptDecision}
                >
                  采用判断结果
                </Button>
              </section>
            ) : null}

            <section className="space-y-3 rounded-xl border border-subtle p-4" aria-label="标注步骤">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="heading-xs">标注步骤</h2>
                <div className="flex items-center gap-2">
                  <span className="text-2xs text-tertiary">
                    {steps.length ? `${Math.min(stepCursor + 1, steps.length)} / ${steps.length}` : '0'}
                  </span>
                  <Button
                    color="secondary"
                    variant="outline"
                    size="sm"
                    disabled={stepCursor <= 0}
                    onClick={() => setStepCursor((value) => Math.max(0, value - 1))}
                  >
                    上一步
                  </Button>
                  <Button
                    color="secondary"
                    variant="outline"
                    size="sm"
                    disabled={stepCursor >= steps.length - 1}
                    onClick={() => setStepCursor((value) => Math.min(steps.length - 1, value + 1))}
                  >
                    下一步
                  </Button>
                </div>
              </div>

              {activeStep ? (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge tone={effectTone(activeStep)}>{effectText(activeStep)}</StatusBadge>
                    <span className="text-xs text-secondary">
                      {categoryLabel(activeStep.category)}
                      {activeStep.target ? ` · ${activeStep.target}` : ''}
                      {activeStep.hard_negative ? ' · 硬负样本' : ''}
                    </span>
                  </div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {activeStep.before ? (
                      <button
                        type="button"
                        className="overflow-hidden rounded-lg border border-subtle text-left transition-colors hover:bg-secondary-ghost-hover"
                        onClick={() =>
                          setPreview({ src: activeStep.before!.url, alt: `第 ${activeStep.index} 步点击前截图` })
                        }
                      >
                        <img src={activeStep.before.url} alt={`第 ${activeStep.index} 步点击前`} className="block w-full" />
                        <span className="block px-2 py-1 text-2xs text-secondary">点击前</span>
                      </button>
                    ) : null}
                    {activeStep.after ? (
                      <button
                        type="button"
                        className="overflow-hidden rounded-lg border border-subtle text-left transition-colors hover:bg-secondary-ghost-hover"
                        onClick={() =>
                          setPreview({ src: activeStep.after!.url, alt: `第 ${activeStep.index} 步点击后截图` })
                        }
                      >
                        <img src={activeStep.after.url} alt={`第 ${activeStep.index} 步点击后`} className="block w-full" />
                        <span className="block px-2 py-1 text-2xs text-secondary">点击后</span>
                      </button>
                    ) : null}
                  </div>
                  {activeStep.suggestion ? (
                    <p className="text-2xs text-tertiary">
                      保存时判断：{decisionSourceLabel(activeStep.suggestion)}
                      {activeStep.suggestion.target_label ? ` · ${activeStep.suggestion.target_label}` : ''}
                    </p>
                  ) : null}
                </div>
              ) : (
                <p className="text-sm text-secondary">
                  {review ? '该门店没有归档步骤。' : '当前门店还没有标注步骤。'}
                </p>
              )}
            </section>
          </div>
        </div>
      ) : null}

      <ConfirmActionDialog
        open={Boolean(confirm)}
        title={confirm?.title ?? ''}
        description={confirm?.description ?? null}
        confirmLabel={confirm?.confirmLabel ?? '确认'}
        tone={confirm?.tone ?? 'primary'}
        busy={busy}
        onConfirm={() => {
          const action = confirm?.action
          setConfirm(null)
          action?.()
        }}
        onOpenChange={(open) => {
          if (!open) setConfirm(null)
        }}
      />

      <RejectDialog
        open={rejectOpen}
        taskName={currentTask ? taskDisplayName(currentTask) : '当前门店'}
        busy={busy}
        error={rejectError}
        onConfirm={reject}
        onOpenChange={setRejectOpen}
      />

      <ImagePreview
        open={Boolean(preview)}
        src={preview?.src ?? ''}
        alt={preview?.alt ?? ''}
        {...(preview?.caption ? { caption: preview.caption } : {})}
        onOpenChange={(open) => {
          if (!open) setPreview(null)
        }}
      />
    </section>
  )
}
