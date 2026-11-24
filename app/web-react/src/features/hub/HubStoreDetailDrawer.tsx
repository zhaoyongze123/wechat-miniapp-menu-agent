import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { AppDrawer } from '../../components/AppDrawer'
import { AppPagination } from '../../components/AppPagination'
import { CellText } from '../../components/CellText'
import { ImagePreview } from '../../components/ImagePreview'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { errorMessage } from '../../api/client'
import { AssignmentDetailDrawer } from './AssignmentDetailDrawer'
import { fetchAssignmentRecords } from './api'
import { assignmentStatusOf, formatStore, storeStateOf } from './format'
import type { AssignmentRecord, HubTask } from './types'

const PAGE_SIZE = 20

interface HubStoreDetailDrawerProps {
  task: HubTask | null
  onClose: () => void
  /** 在当前筛选结果内连续查看；跨页只在稳定查询范围内提供。 */
  onPrev?: () => void
  onNext?: () => void
  hasPrev?: boolean
  hasNext?: boolean
  position?: string
}

/** 只读门店详情。用当前任务 ID 精确关联分配结果，不按同名门店拼接其他批次/版本。 */
export function HubStoreDetailDrawer({
  task,
  onClose,
  onPrev,
  onNext,
  hasPrev = false,
  hasNext = false,
  position,
}: HubStoreDetailDrawerProps) {
  const [page, setPage] = useState(1)
  const [assignment, setAssignment] = useState<AssignmentRecord | null>(null)
  const [preview, setPreview] = useState<{ src: string; alt: string } | null>(null)
  const navigate = useNavigate()
  const taskId = task?.id ?? ''
  useEffect(() => { setPage(1); setAssignment(null); setPreview(null) }, [taskId])
  const history = useQuery({
    queryKey: ['hub-store-assignments', taskId, page],
    queryFn: ({ signal }) => fetchAssignmentRecords({ workerId: '', phase: '', q: taskId, page, size: PAGE_SIZE }, signal),
    enabled: Boolean(taskId),
    staleTime: 5_000,
  })
  if (!task) return null
  const store = formatStore(task.source_name)
  const status = storeStateOf(task)
  const reviewable = status.label === '待检查'
  const records = (history.data?.records ?? []).filter((record) => record.task_id === taskId)
  if (assignment && assignment.task_id === taskId) {
    return <AssignmentDetailDrawer key={assignment.id} record={assignment} onOpenChange={(open) => { if (!open) setAssignment(null) }} />
  }
  return (
    <>
    <AppDrawer
      open
      onOpenChange={(open) => { if (!open) onClose() }}
      title={store.name || task.source_name || '门店详情'}
      description={[store.code, task.batch_name, task.version].filter(Boolean).join(' · ')}
      {...(onPrev || onNext
        ? {
            headerActions: (
              <div className="flex items-center gap-2">
                {position ? <span className="text-2xs text-tertiary">{position}</span> : null}
                <Button color="secondary" variant="outline" size="sm" disabled={!hasPrev} onClick={onPrev}>
                  上一家
                </Button>
                <Button color="secondary" variant="outline" size="sm" disabled={!hasNext} onClick={onNext}>
                  下一家
                </Button>
              </div>
            ),
          }
        : {})}
    >
      <div className="space-y-5">
        <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
        <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
          <div className="min-w-0">
            <dt className="text-xs text-tertiary">原始门店名称</dt>
            <dd className="mt-0.5"><CellText copyable copyLabel="门店名称">{task.source_name}</CellText></dd>
          </div>
          <div><dt className="text-xs text-tertiary">批次 / 版本</dt><dd>{[task.batch_name, task.version].filter(Boolean).join(' · ') || '未记录'}</dd></div>
          <div className="min-w-0">
            <dt className="text-xs text-tertiary">任务 ID</dt>
            <dd className="mt-0.5"><CellText copyable copyLabel="任务 ID" mono>{task.id}</CellText></dd>
          </div>
          <div className="min-w-0">
            <dt className="text-xs text-tertiary">采集说明</dt>
            <dd className="mt-0.5">
              <CellText>{task.reject_reason || task.reject_label || task.message || task.label || '未记录'}</CellText>
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="text-xs text-tertiary">同步状态</dt>
            <dd className="mt-0.5 text-sm">
              {history.isPending
                ? '正在核对…'
                : records.length > 0
                  ? `已收到采集电脑结果（${records.length} 条）`
                  : '尚未收到采集电脑同步结果'}
            </dd>
          </div>
        </dl>
        <div className="flex flex-wrap items-center gap-2">
          <Button color="secondary" variant="outline" size="sm" onClick={() => setPreview({ src: `/api/tasks/${encodeURIComponent(task.id)}/source`, alt: '任务二维码' })}>
            查看二维码
          </Button>
          <Button color="secondary" variant="outline" size="sm" onClick={() => setPreview({ src: `/api/tasks/${encodeURIComponent(task.id)}/last-frame`, alt: '最近采集画面' })}>
            查看最近画面
          </Button>
          {reviewable ? (
            <Button color="primary" size="sm" onClick={() => navigate(`/rejections?view=review&task=${encodeURIComponent(task.id)}`)}>
              前往质量审核处置
            </Button>
          ) : null}
        </div>
        <p className="text-2xs text-tertiary">Hub 只保存共享端已同步的产物；采集电脑尚未上传图片时，画面预览不可用。</p>
        <section className="space-y-3">
          <h3 className="heading-xs">当前任务分配与采集结果</h3>
          <p className="text-xs text-secondary">仅显示与当前任务 ID 匹配的记录。其他素材版本或未同步的结果不会在此冒充当前结果。</p>
          {history.isPending ? <StatePanel variant="loading" title="正在读取分配记录…" /> : history.isError ? (
            <StatePanel variant="error" title="分配记录读取失败" description={errorMessage(history.error)} action={<Button size="sm" color="secondary" variant="outline" onClick={() => void history.refetch()}>重试</Button>} />
          ) : records.length === 0 ? <StatePanel variant="empty" title="当前任务暂无匹配的分配记录" /> : (
            <ul className="divide-y divide-subtle rounded-lg border border-subtle">
              {records.map((record) => {
                const meta = assignmentStatusOf(record.status, record.disposition)
                return <li key={record.id} className="flex items-center justify-between gap-3 p-3">
                  <div className="min-w-0 space-y-1">
                    <div className="flex flex-wrap items-center gap-2"><span className="text-sm">{record.worker_name || record.worker_id}</span><StatusBadge tone={meta.tone}>{meta.label}</StatusBadge></div>
                    {record.result_message ? <p className="line-clamp-2 text-xs text-secondary">{record.result_message}</p> : null}
                  </div>
                  <Button color="secondary" variant="outline" size="sm" onClick={() => setAssignment(record)}>查看结果</Button>
                </li>
              })}
            </ul>
          )}
          {history.data && history.data.pages > 1 ? <AppPagination page={history.data.page} pageCount={history.data.pages} total={history.data.total} pageSize={PAGE_SIZE} pageSizeOptions={[PAGE_SIZE]} onPageChange={setPage} /> : null}
        </section>
      </div>
    </AppDrawer>
    <ImagePreview
      open={preview !== null}
      src={preview?.src ?? ''}
      alt={preview?.alt ?? ''}
      caption={preview?.alt ?? ''}
      onOpenChange={(open) => { if (!open) setPreview(null) }}
    />
    </>
  )
}
