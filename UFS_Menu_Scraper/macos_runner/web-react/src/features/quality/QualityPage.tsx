import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { SegmentedControl } from '@openai/apps-sdk-ui/components/SegmentedControl'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { AppPagination } from '../../components/AppPagination'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { PageHeader } from '../../components/PageHeader'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { useRole } from '../../app/RoleProvider'
import { AssignDialog } from '../hub/AssignDialog'
import { formatStore, formatTime } from '../hub/format'
import { useHubSharedStatus } from '../hub/hooks'
import type { HubSelectedTask } from '../hub/types'
import { useBatchOptions } from '../materials/hooks'
import { statusOf } from '../task-center/format'
import { REASON_OPTIONS, downloadRejections } from './api'
import {
  REVIEW_BUCKETS,
  REVIEW_STATE_OPTIONS,
  SOURCE_OPTIONS,
  bucketLabelOf,
  isRetryable,
  rejectionKey,
  reviewBucketOf,
  sourceLabelOf,
} from './format'
import {
  useHubRunning,
  usePendingReviews,
  useRejectionActions,
  useRejections,
  useReviewActions,
} from './hooks'
import { PendingReviewDrawer } from './PendingReviewDrawer'
import { RejectionDetailDrawer } from './RejectionDetailDrawer'
import type { PendingReviewRecord, RejectionRecord, ReviewBucket } from './types'

type QualityView = 'rejections' | 'review'
type SourceFilter = 'all' | 'materials' | 'capture'
type ReviewStateFilter = 'all' | 'reviewed' | 'pending'

const PAGE_SIZE = 50

/** 质量审核：不合格清单与待检查清单共享同一页面，数据和操作全部走后端接口。 */
export function QualityPage() {
  const role = useRole()
  const toast = useToast()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [view, setView] = useState<QualityView>(() =>
    searchParams.get('view') === 'review' ? 'review' : 'rejections',
  )

  // 不合格清单
  const [batchId, setBatchId] = useState('all')
  const [source, setSource] = useState<SourceFilter>('all')
  const [reason, setReason] = useState('all')
  const [reviewState, setReviewState] = useState<ReviewStateFilter>('all')
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(PAGE_SIZE)
  const [drawerKey, setDrawerKey] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [selectedRejections, setSelectedRejections] = useState<Set<string>>(new Set())
  const [undoBatchOpen, setUndoBatchOpen] = useState(false)
  const [undoBatchResult, setUndoBatchResult] = useState<{ succeeded: number; failures: string[] } | null>(null)

  // 待检查清单
  const [reviewBatchIds, setReviewBatchIds] = useState<string[]>([])
  const [reviewSearch, setReviewSearch] = useState('')
  const [reviewQuery, setReviewQuery] = useState('')
  const [bucket, setBucket] = useState<'all' | ReviewBucket>('all')
  const [reviewPage, setReviewPage] = useState(1)
  const [selectedTasks, setSelectedTasks] = useState<Set<string>>(new Set())
  const [reviewDrawerId, setReviewDrawerId] = useState<string | null>(null)

  // 弹窗
  const [assignTasks, setAssignTasks] = useState<HubSelectedTask[] | null>(null)
  const [returnRecord, setReturnRecord] = useState<RejectionRecord | null>(null)
  const [approveOpen, setApproveOpen] = useState(false)
  const [unapproveOpen, setUnapproveOpen] = useState(false)
  const [dialogError, setDialogError] = useState('')

  const rejectionsQuery = useRejections({ batchId, source, reason, q: query, reviewed: reviewState }, view === 'rejections')
  const reviewsQuery = usePendingReviews(reviewBatchIds, reviewQuery, view === 'review')
  const runningQuery = useHubRunning()
  const batchOptionsQuery = useBatchOptions()
  const hubShared = useHubSharedStatus()
  const rejectionActions = useRejectionActions()
  const reviewActions = useReviewActions()

  const running = runningQuery.data === true
  const actionBusy =
    rejectionActions.reviewed.isPending ||
    rejectionActions.undoCapture.isPending ||
    rejectionActions.undoMaterials.isPending ||
    rejectionActions.undoBatch.isPending ||
    rejectionActions.returnToMaterials.isPending ||
    rejectionActions.retry.isPending

  // 输入防抖：列表筛选走服务端，避免每次按键都请求。
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 300)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    const timer = window.setTimeout(() => setReviewQuery(reviewSearch.trim()), 300)
    return () => window.clearTimeout(timer)
  }, [reviewSearch])

  useEffect(() => {
    setPage(1)
    setSelectedRejections(new Set())
    setUndoBatchResult(null)
  }, [batchId, source, reason, reviewState, query, pageSize])

  useEffect(() => {
    setSelectedRejections(new Set())
  }, [page, pageSize])

  const reviewBatchKey = reviewBatchIds.join(',')
  useEffect(() => {
    setReviewPage(1)
  }, [reviewBatchKey, reviewQuery, bucket])

  const workflowByBatch = useMemo(
    () => new Map((batchOptionsQuery.data ?? []).map((batch) => [batch.batch_id, batch.workflow])),
    [batchOptionsQuery.data],
  )

  const rejectionRecords = rejectionsQuery.data?.records ?? []
  const visibleRejections = rejectionRecords

  const rejectionPageCount = Math.max(1, Math.ceil(visibleRejections.length / pageSize))
  const currentPage = Math.min(page, rejectionPageCount)
  const pageRejections = visibleRejections.slice((currentPage - 1) * pageSize, currentPage * pageSize)
  const eligiblePageRejections = pageRejections.filter((record) =>
    record.source_type === 'capture' ? Boolean(record.task_id) : Boolean(record.batch_id && record.store_id),
  )
  const eligiblePageKeys = eligiblePageRejections.map(rejectionKey)
  const allRejectionPageSelected = eligiblePageKeys.length > 0 && eligiblePageKeys.every((key) => selectedRejections.has(key))
  const someRejectionPageSelected = eligiblePageKeys.some((key) => selectedRejections.has(key))
  const selectedPageRecords = eligiblePageRejections.filter((record) => selectedRejections.has(rejectionKey(record)))
  const drawerIndex = drawerKey
    ? visibleRejections.findIndex((record) => rejectionKey(record) === drawerKey)
    : -1
  const drawerRecord = drawerIndex >= 0 ? visibleRejections[drawerIndex] ?? null : null
  const drawerWorkflowLabel = drawerRecord
    ? workflowByBatch.get(drawerRecord.batch_id)?.label
    : undefined

  const reviewAll = useMemo(
    () => (reviewsQuery.data?.records ?? []).filter((record) => !record.reject_reason),
    [reviewsQuery.data],
  )
  const bucketCounts = useMemo(() => {
    const counts: Record<string, number> = { all: reviewAll.length }
    for (const record of reviewAll) {
      const key = reviewBucketOf(record)
      counts[key] = (counts[key] ?? 0) + 1
    }
    return counts
  }, [reviewAll])
  const filteredReviews = useMemo(
    () => (bucket === 'all' ? reviewAll : reviewAll.filter((record) => reviewBucketOf(record) === bucket)),
    [reviewAll, bucket],
  )

  // 数据刷新后剔除已不存在的勾选，避免对不可见门店执行批量操作。
  useEffect(() => {
    setSelectedTasks((current) => {
      const visible = new Set(filteredReviews.map((record) => record.task_id))
      const next = new Set([...current].filter((id) => visible.has(id)))
      return next.size === current.size ? current : next
    })
  }, [filteredReviews])

  const reviewPageCount = Math.max(1, Math.ceil(filteredReviews.length / PAGE_SIZE))
  const currentReviewPage = Math.min(reviewPage, reviewPageCount)
  const pageReviews = filteredReviews.slice(
    (currentReviewPage - 1) * PAGE_SIZE,
    currentReviewPage * PAGE_SIZE,
  )
  const reviewDrawerIndex = reviewDrawerId
    ? filteredReviews.findIndex((record) => record.task_id === reviewDrawerId)
    : -1
  const reviewDrawerRecord =
    reviewDrawerIndex >= 0 ? filteredReviews[reviewDrawerIndex] ?? null : null

  const reviewBatches = reviewsQuery.data?.batches ?? []
  const approveBatchIds = reviewBatchIds.length
    ? reviewBatchIds
    : reviewBatches.map((batch) => batch.id)
  const pageTaskIds = pageReviews.map((record) => record.task_id)
  const allPageSelected = pageTaskIds.length > 0 && pageTaskIds.every((id) => selectedTasks.has(id))
  const somePageSelected = pageTaskIds.some((id) => selectedTasks.has(id))

  const changeView = (next: QualityView) => {
    setView(next)
    setSearchParams(
      (current) => {
        const params = new URLSearchParams(current)
        if (next === 'review') params.set('view', 'review')
        else params.delete('view')
        return params
      },
      { replace: true },
    )
  }

  const toggleReviewed = (record: RejectionRecord) => {
    const reviewed = !record.reviewed_at
    const payload =
      record.source_type === 'materials'
        ? {
            source_type: 'materials' as const,
            reviewed,
            batch_id: record.batch_id,
            store_id: record.store_id ?? '',
          }
        : { source_type: 'capture' as const, reviewed, task_id: record.task_id }
    rejectionActions.reviewed.mutate(payload, {
      onSuccess: () => toast.push(reviewed ? '已标记人工确认' : '已取消人工确认', 'success'),
      onError: (error) => toast.push(errorMessage(error), 'danger'),
    })
  }

  const undoRejection = (record: RejectionRecord) => {
    const onSuccess = () => {
      toast.push(record.source_type === 'capture'
        ? '已撤销不合格标记，采集任务状态保持原记录'
        : '已撤销不合格，素材门店已恢复为待处理', 'success')
      setDrawerKey(null)
    }
    const onError = (error: Error) => toast.push(errorMessage(error), 'danger')
    if (record.source_type === 'materials') {
      rejectionActions.undoMaterials.mutate(
        { batchId: record.batch_id, storeId: record.store_id ?? '' },
        { onSuccess, onError },
      )
    } else {
      rejectionActions.undoCapture.mutate(record.task_id, { onSuccess, onError })
    }
  }

  const toggleRejection = (record: RejectionRecord) => {
    const key = rejectionKey(record)
    setSelectedRejections((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const toggleRejectionPage = () => {
    setSelectedRejections((current) => {
      const next = new Set(current)
      if (allRejectionPageSelected) eligiblePageKeys.forEach((key) => next.delete(key))
      else eligiblePageKeys.forEach((key) => next.add(key))
      return next
    })
  }

  const confirmUndoBatch = () => {
    const items = selectedPageRecords.map((record) => record.source_type === 'capture'
      ? { source_type: 'capture' as const, task_id: record.task_id }
      : { source_type: 'materials' as const, batch_id: record.batch_id, store_id: record.store_id ?? '' })
    if (!items.length) return
    rejectionActions.undoBatch.mutate(items, {
      onSuccess: (response) => {
        const failed = response.results.filter((result) => !result.ok)
        const failures = failed.map((result) => {
          const id = result.source_type === 'capture' ? result.task_id : result.store_id
          return `${result.store_name || id}: ${result.error || '撤销失败'}`
        })
        const failedKeys = failed.map((result) => result.source_type === 'capture'
          ? `capture:${result.task_id}::`
          : `materials::${result.batch_id}:${result.store_id}`)
        setSelectedRejections(new Set(failedKeys))
        setUndoBatchResult({ succeeded: response.results.length - failed.length, failures })
        setUndoBatchOpen(false)
        toast.push(`批量撤销完成：成功 ${response.results.length - failed.length} 条，失败 ${failed.length} 条`, failed.length ? 'danger' : 'success')
      },
      onError: (error) => toast.push(errorMessage(error), 'danger'),
    })
  }

  const confirmReturn = () => {
    if (!returnRecord) return
    rejectionActions.returnToMaterials.mutate(returnRecord.task_id, {
      onSuccess: () => {
        setReturnRecord(null)
        setDialogError('')
        setDrawerKey(null)
        toast.push('已打回素材清洗，可在素材清洗重新确认二维码', 'success')
      },
      onError: (error) => setDialogError(errorMessage(error)),
    })
  }

  const retryCapture = (record: RejectionRecord) => {
    rejectionActions.retry.mutate(record.task_id, {
      onSuccess: () => {
        setDrawerKey(null)
        toast.push('已重新入队，正在打开采集工作台', 'success')
        navigate(`/workbench?selected=${encodeURIComponent(record.task_id)}`)
      },
      onError: (error) => toast.push(errorMessage(error), 'danger'),
    })
  }

  const continueMaterials = (record: RejectionRecord) => {
    navigate(
      `/materials?batch=${encodeURIComponent(record.batch_id)}&store=${encodeURIComponent(record.store_id ?? '')}`,
    )
  }

  const exportExcel = async () => {
    setExporting(true)
    try {
      await downloadRejections({ batchId, source, reason, q: query, reviewed: reviewState })
      toast.push('已导出当前筛选的不合格门店', 'success')
    } catch (error) {
      toast.push(errorMessage(error), 'danger')
    } finally {
      setExporting(false)
    }
  }

  const toggleBatch = (id: string) => {
    setReviewBatchIds((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    )
  }

  const toggleTask = (id: string) => {
    setSelectedTasks((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const togglePage = () => {
    setSelectedTasks((current) => {
      const next = new Set(current)
      if (allPageSelected) {
        for (const id of pageTaskIds) next.delete(id)
      } else {
        for (const id of pageTaskIds) next.add(id)
      }
      return next
    })
  }

  const openAssign = (records: PendingReviewRecord[]) => {
    setAssignTasks(
      records.map((record) => ({
        id: record.task_id,
        ...(record.store_name ? { source_name: record.store_name } : {}),
        ...(record.status ? { status: record.status } : {}),
        ...(record.batch_name ? { batch_name: record.batch_name } : {}),
      })),
    )
  }

  const assignSelected = () => {
    openAssign(filteredReviews.filter((record) => selectedTasks.has(record.task_id)))
  }

  const confirmApprove = () => {
    reviewActions.approve.mutate(approveBatchIds, {
      onSuccess: () => {
        setApproveOpen(false)
        setDialogError('')
        setSelectedTasks(new Set())
        toast.push(`已标记 ${approveBatchIds.length} 个原批次为人工审核通过`, 'success')
      },
      onError: (error) => setDialogError(errorMessage(error)),
    })
  }

  const confirmUnapprove = () => {
    reviewActions.unapprove.mutate(approveBatchIds, {
      onSuccess: () => {
        setUnapproveOpen(false)
        setDialogError('')
        toast.push(`已撤销 ${approveBatchIds.length} 个批次的审核通过`, 'success')
      },
      onError: (error) => setDialogError(errorMessage(error)),
    })
  }

  const rejectionBatches = rejectionsQuery.data?.batches ?? []
  const summary = rejectionsQuery.data?.summary
  const reviewSummary = reviewsQuery.data?.summary

  const rejectionColumns: DataColumn<RejectionRecord>[] = [
    {
      id: 'select',
      header: (
        <Checkbox
          label="全选本页可撤销项"
          checked={allRejectionPageSelected ? true : someRejectionPageSelected ? 'indeterminate' : false}
          disabled={running || eligiblePageKeys.length === 0}
          onCheckedChange={toggleRejectionPage}
        />
      ),
      className: 'w-36',
      cell: (record) => {
        const eligible = record.source_type === 'capture' ? Boolean(record.task_id) : Boolean(record.batch_id && record.store_id)
        return (
          <div onClick={(event) => event.stopPropagation()}>
            <Checkbox
              label="选择"
              checked={selectedRejections.has(rejectionKey(record))}
              disabled={running || actionBusy || !eligible}
              onCheckedChange={() => toggleRejection(record)}
            />
          </div>
        )
      },
    },
    {
      id: 'store',
      header: '门店',
      cell: (record) => (
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-default">{formatStore(record.store_name).name}</span>
            <StatusBadge tone={record.source_type === 'materials' ? 'info' : 'secondary'}>
              {sourceLabelOf(record)}
            </StatusBadge>
          </div>
          <span className="text-2xs text-tertiary">{record.source_folder || record.source_item || '—'}</span>
        </div>
      ),
    },
    {
      id: 'batch',
      header: '批次 / 阶段',
      cell: (record) => (
        <div className="min-w-0">
          <div className="truncate text-default">{record.batch_name || '—'}</div>
          <div className="text-2xs text-tertiary">
            {workflowByBatch.get(record.batch_id)?.label || record.stage || '—'}
          </div>
        </div>
      ),
    },
    {
      id: 'reason',
      header: '原因',
      className: 'max-w-72',
      cell: (record) => (
        <div>
          <div className="text-default">{record.reason_label || record.reason_code || '—'}</div>
          {record.detail ? <div className="line-clamp-2 text-2xs text-tertiary">{record.detail}</div> : null}
        </div>
      ),
    },
    {
      id: 'marked',
      header: '标记时间',
      className: 'whitespace-nowrap',
      cell: (record) => (
        <span className="text-secondary">{record.marked_at ? formatTime(record.marked_at) : '—'}</span>
      ),
    },
    {
      id: 'reviewed',
      header: '人工确认',
      className: 'whitespace-nowrap',
      cell: (record) => (
        <div onClick={(event) => event.stopPropagation()}>
          <Checkbox
            label="确认"
            checked={Boolean(record.reviewed_at)}
            disabled={actionBusy}
            onCheckedChange={() => toggleReviewed(record)}
          />
        </div>
      ),
    },
    {
      id: 'actions',
      header: '',
      className: 'w-20 whitespace-nowrap text-right',
      cell: (record) => (
        <div onClick={(event) => event.stopPropagation()}>
          <Button
            color="secondary"
            variant="outline"
            size="sm"
            onClick={() => setDrawerKey(rejectionKey(record))}
          >
            详情
          </Button>
        </div>
      ),
    },
  ]

  const reviewColumns: DataColumn<PendingReviewRecord>[] = [
    {
      id: 'select',
      header: (
        <Checkbox
          label="全选"
          checked={allPageSelected ? true : somePageSelected ? 'indeterminate' : false}
          disabled={pageReviews.length === 0}
          onCheckedChange={togglePage}
        />
      ),
      className: 'w-20',
      cell: (record) => (
        <div onClick={(event) => event.stopPropagation()}>
          <Checkbox
            label="选择"
            checked={selectedTasks.has(record.task_id)}
            onCheckedChange={() => toggleTask(record.task_id)}
          />
        </div>
      ),
    },
    {
      id: 'store',
      header: '门店',
      cell: (record) => {
        const status = statusOf(record.status)
        return (
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate text-default">{formatStore(record.store_name).name}</span>
              <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
              <StatusBadge tone="secondary">{bucketLabelOf(record)}</StatusBadge>
            </div>
            <span className="text-2xs text-tertiary">{record.source_path || record.task_id}</span>
          </div>
        )
      },
    },
    {
      id: 'batch',
      header: '批次 / 阶段',
      cell: (record) => (
        <div className="min-w-0">
          <div className="truncate text-default">{record.batch_name || '—'}</div>
          <div className="text-2xs text-tertiary">{record.workflow?.label || record.stage || '—'}</div>
        </div>
      ),
    },
    {
      id: 'reason',
      header: '原因',
      className: 'max-w-72',
      cell: (record) => (
        <div>
          <div className="text-default">{record.label || record.group || '—'}</div>
          <div className="line-clamp-2 text-2xs text-tertiary">
            {record.reject_detail || record.message || record.reject_reason || '—'}
          </div>
        </div>
      ),
    },
    {
      id: 'updated',
      header: '更新时间',
      className: 'whitespace-nowrap',
      cell: (record) => (
        <span className="text-secondary">{record.updated_at ? formatTime(record.updated_at) : '—'}</span>
      ),
    },
    {
      id: 'actions',
      header: '',
      className: 'w-32 whitespace-nowrap text-right',
      cell: (record) => (
        <div className="flex justify-end gap-2" onClick={(event) => event.stopPropagation()}>
          <Button
            color="secondary"
            variant="ghost"
            size="sm"
            onClick={() => setReviewDrawerId(record.task_id)}
          >
            详情
          </Button>
          <Button
            color="secondary"
            variant="outline"
            size="sm"
            disabled={!isRetryable(record.status)}
            onClick={() => openAssign([record])}
          >
            重跑
          </Button>
        </div>
      ),
    },
  ]

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
  if (role.role !== 'hub') {
    return (
      <StatePanel
        variant="info"
        title="仅共享后台可做质量审核"
        description="Windows 采集端只负责采集与上传，不合格处置与通过审核在共享后台完成。"
      />
    )
  }

  return (
    <section className="space-y-6" aria-labelledby="quality-heading">
      <PageHeader title="质量审核" headingId="quality-heading" />

      <SegmentedControl<QualityView> value={view} onChange={changeView} aria-label="质量审核视图">
        <SegmentedControl.Option value="rejections">不合格门店</SegmentedControl.Option>
        <SegmentedControl.Option value="review">待检查门店</SegmentedControl.Option>
      </SegmentedControl>

      {view === 'rejections' ? (
        <>
          <section className="grid grid-cols-2 gap-3 sm:grid-cols-4" aria-label="不合格统计">
            <Metric label="不合格门店" value={summary?.stores ?? 0} />
            <Metric label="素材 / 采集" value={`${summary?.materials ?? 0} / ${summary?.capture ?? 0}`} />
            <Metric label="已人工确认" value={summary?.reviewed ?? 0} />
          </section>

          <section className="space-y-3 rounded-xl border border-subtle p-4" aria-labelledby="rejections-list-heading">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 id="rejections-list-heading" className="heading-xs">
                不合格清单
              </h2>
              <div className="flex flex-wrap items-center gap-2">
                <Button color="secondary" variant="outline" size="sm" onClick={() => void rejectionsQuery.refetch()}>
                  刷新
                </Button>
                <Button color="secondary" variant="outline" size="sm" disabled={exporting} onClick={() => void exportExcel()}>
                  {exporting ? '导出中…' : '导出 Excel'}
                </Button>
              </div>
            </div>

            <div className="flex flex-wrap gap-2">
              <div className="w-56">
                <Select
                  aria-label="批次筛选"
                  value={batchId}
                  options={[
                    { value: 'all', label: '全部批次' },
                    ...rejectionBatches.map((batch) => ({ value: batch.id, label: batch.name })),
                  ]}
                  onChange={(option) => setBatchId(option.value)}
                />
              </div>
              <div className="w-40">
                <Select
                  aria-label="来源筛选"
                  value={source}
                  options={SOURCE_OPTIONS}
                  onChange={(option) => setSource(option.value as SourceFilter)}
                />
              </div>
              <div className="w-56">
                <Select
                  aria-label="原因筛选"
                  value={reason}
                  options={REASON_OPTIONS}
                  onChange={(option) => setReason(option.value)}
                />
              </div>
              <div className="w-44">
                <Select
                  aria-label="确认状态筛选"
                  value={reviewState}
                  options={REVIEW_STATE_OPTIONS}
                  onChange={(option) => setReviewState(option.value as ReviewStateFilter)}
                />
              </div>
              <div className="w-64">
                <Input
                  aria-label="搜索不合格门店"
                  value={search}
                  placeholder="搜索门店、批次或说明"
                  onChange={(event) => setSearch(event.target.value)}
                />
              </div>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-subtle bg-surface-secondary px-3 py-2">
              <span className="text-xs text-secondary">本页已选 {selectedPageRecords.length} 条可撤销记录；翻页后需在当前页重新选择。</span>
              <Button
                color="danger"
                variant="outline"
                size="sm"
                disabled={running || actionBusy || selectedPageRecords.length === 0}
                onClick={() => setUndoBatchOpen(true)}
              >
                撤销所选不合格（{selectedPageRecords.length}）
              </Button>
            </div>
            {undoBatchResult ? (
              <Alert
                color={undoBatchResult.failures.length ? 'warning' : 'success'}
                variant="soft"
                title={`批量撤销结果：成功 ${undoBatchResult.succeeded} 条，失败 ${undoBatchResult.failures.length} 条`}
                {...(undoBatchResult.failures.length ? { description: undoBatchResult.failures.join('；') } : {})}
              />
            ) : null}

            {rejectionsQuery.isError ? (
              <StatePanel
                variant="error"
                title="不合格清单读取失败"
                description={errorMessage(rejectionsQuery.error)}
                action={
                  <Button color="secondary" variant="outline" size="sm" onClick={() => void rejectionsQuery.refetch()}>
                    重试
                  </Button>
                }
              />
            ) : (
              <>
                <AppDataTable
                  columns={rejectionColumns}
                  rows={pageRejections}
                  rowKey={rejectionKey}
                  caption="不合格门店清单"
                  loading={rejectionsQuery.isLoading && !rejectionsQuery.data}
                  onRowActivate={(record) => setDrawerKey(rejectionKey(record))}
                  emptyState={
                    <StatePanel
                      variant="empty"
                      title="没有符合条件的不合格记录"
                      description="调整来源、原因或确认状态筛选后再试。"
                    />
                  }
                />
                <AppPagination
                  page={currentPage}
                  pageCount={rejectionPageCount}
                  total={visibleRejections.length}
                  pageSize={pageSize}
                  pageSizeOptions={[20, 50, 100]}
                  onPageChange={setPage}
                  onPageSizeChange={setPageSize}
                />
              </>
            )}
          </section>
        </>
      ) : (
        <>
          <section className="grid grid-cols-2 gap-3 sm:grid-cols-4" aria-label="待检查统计">
            <Metric label="待检查门店" value={reviewSummary?.stores ?? 0} />
            <Metric label="涉及批次" value={reviewSummary?.batches ?? 0} />
            <Metric label="需人工确认" value={reviewSummary?.needs_review ?? 0} />
            <Metric label="可重跑" value={reviewSummary?.retryable ?? 0} />
          </section>

          <section className="space-y-3 rounded-xl border border-subtle p-4" aria-labelledby="review-list-heading">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 id="review-list-heading" className="heading-xs">
                待检查门店
              </h2>
              <div className="flex flex-wrap items-center gap-2">
                <div className="w-64">
                  <Input
                    aria-label="搜索待检查门店"
                    value={reviewSearch}
                    placeholder="搜索门店或批次"
                    onChange={(event) => setReviewSearch(event.target.value)}
                  />
                </div>
                <Button color="secondary" variant="outline" size="sm" onClick={() => void reviewsQuery.refetch()}>
                  刷新
                </Button>
              </div>
            </div>

            {reviewBatches.length ? (
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs text-secondary">批次范围（不选表示全部批次）</span>
                  <Button
                    color="secondary"
                    variant="ghost"
                    size="sm"
                    disabled={reviewBatchIds.length === 0}
                    onClick={() => setReviewBatchIds([])}
                  >
                    全部批次
                  </Button>
                </div>
                <div className="flex max-h-24 flex-wrap gap-x-4 gap-y-2 overflow-y-auto rounded-lg border border-subtle p-3">
                  {reviewBatches.map((batch) => (
                    <Checkbox
                      key={batch.id}
                      label={batch.name}
                      checked={reviewBatchIds.includes(batch.id)}
                      onCheckedChange={() => toggleBatch(batch.id)}
                    />
                  ))}
                </div>
              </div>
            ) : null}

            <div className="flex flex-wrap gap-2" role="group" aria-label="按失败分类筛选">
              {REVIEW_BUCKETS.map((item) => (
                <Button
                  key={item.value}
                  color={bucket === item.value ? 'primary' : 'secondary'}
                  variant={bucket === item.value ? 'solid' : 'outline'}
                  size="sm"
                  aria-pressed={bucket === item.value}
                  onClick={() => setBucket(item.value)}
                >
                  {item.label}（{bucketCounts[item.value] ?? 0}）
                </Button>
              ))}
            </div>

            <div className="flex flex-wrap items-center justify-between gap-3">
              <span className="text-xs text-secondary">已选 {selectedTasks.size} 家门店</span>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  color="secondary"
                  variant="outline"
                  size="sm"
                  disabled={selectedTasks.size === 0}
                  onClick={assignSelected}
                >
                  重跑选中（{selectedTasks.size}）
                </Button>
                <Button
                  color="secondary"
                  variant="outline"
                  size="sm"
                  disabled={approveBatchIds.length === 0}
                  onClick={() => {
                    setDialogError('')
                    setApproveOpen(true)
                  }}
                >
                  标记批次通过
                </Button>
                <Button
                  color="secondary"
                  variant="ghost"
                  size="sm"
                  disabled={approveBatchIds.length === 0}
                  onClick={() => {
                    setDialogError('')
                    setUnapproveOpen(true)
                  }}
                >
                  撤销通过
                </Button>
              </div>
            </div>

            {reviewsQuery.isError ? (
              <StatePanel
                variant="error"
                title="待检查清单读取失败"
                description={errorMessage(reviewsQuery.error)}
                action={
                  <Button color="secondary" variant="outline" size="sm" onClick={() => void reviewsQuery.refetch()}>
                    重试
                  </Button>
                }
              />
            ) : (
              <>
                <AppDataTable
                  columns={reviewColumns}
                  rows={pageReviews}
                  rowKey={(record) => record.task_id}
                  caption="待检查门店清单"
                  loading={reviewsQuery.isLoading && !reviewsQuery.data}
                  onRowActivate={(record) => setReviewDrawerId(record.task_id)}
                  emptyState={
                    <StatePanel
                      variant="empty"
                      title="没有待检查门店"
                      description="当前范围内没有需要人工复核的采集结果。"
                    />
                  }
                />
                <AppPagination
                  page={currentReviewPage}
                  pageCount={reviewPageCount}
                  total={filteredReviews.length}
                  pageSize={PAGE_SIZE}
                  pageSizeOptions={[50]}
                  onPageChange={setReviewPage}
                />
              </>
            )}
          </section>
        </>
      )}

      <RejectionDetailDrawer
        record={drawerRecord}
        {...(drawerWorkflowLabel ? { workflowLabel: drawerWorkflowLabel } : {})}
        running={running}
        busy={actionBusy}
        hasPrev={drawerIndex > 0}
        hasNext={drawerIndex >= 0 && drawerIndex < visibleRejections.length - 1}
        onOpenChange={(open) => {
          if (!open) setDrawerKey(null)
        }}
        onPrev={() => {
          const previous = visibleRejections[drawerIndex - 1]
          if (previous) setDrawerKey(rejectionKey(previous))
        }}
        onNext={() => {
          const next = visibleRejections[drawerIndex + 1]
          if (next) setDrawerKey(rejectionKey(next))
        }}
        onToggleReviewed={toggleReviewed}
        onUndo={undoRejection}
        onReturn={(record) => {
          setDialogError('')
          setReturnRecord(record)
        }}
        onRetry={retryCapture}
        onContinue={continueMaterials}
      />

      <PendingReviewDrawer
        record={reviewDrawerRecord}
        hasPrev={reviewDrawerIndex > 0}
        hasNext={reviewDrawerIndex >= 0 && reviewDrawerIndex < filteredReviews.length - 1}
        onOpenChange={(open) => {
          if (!open) setReviewDrawerId(null)
        }}
        onPrev={() => {
          const previous = filteredReviews[reviewDrawerIndex - 1]
          if (previous) setReviewDrawerId(previous.task_id)
        }}
        onNext={() => {
          const next = filteredReviews[reviewDrawerIndex + 1]
          if (next) setReviewDrawerId(next.task_id)
        }}
        onRerun={(record) => openAssign([record])}
      />

      <ConfirmActionDialog
        open={undoBatchOpen}
        title={`批量撤销 ${selectedPageRecords.length} 条不合格记录`}
        description={
          <div className="space-y-2">
            <p>将逐条按原有撤销规则处理。采集任务只移除不合格标记并保留当前任务状态；素材门店恢复为待处理。历史采集结果不会删除。</p>
            <ul className="max-h-40 list-disc space-y-1 overflow-y-auto pl-5">
              {selectedPageRecords.map((record) => <li key={rejectionKey(record)}>{formatStore(record.store_name).name} · {sourceLabelOf(record)}</li>)}
            </ul>
          </div>
        }
        confirmLabel="确认批量撤销"
        tone="danger"
        busy={rejectionActions.undoBatch.isPending}
        onConfirm={confirmUndoBatch}
        onOpenChange={(open) => setUndoBatchOpen(open)}
      />

      <ConfirmActionDialog
        open={returnRecord !== null}
        title="打回素材清洗"
        description={
          returnRecord
            ? `将把「${formatStore(returnRecord.store_name).name}」（批次：${returnRecord.batch_name || '未关联'}）打回素材清洗，清除不合格标记并把门店恢复为待处理，采集任务置为已阻塞。`
            : ''
        }
        confirmLabel="确认打回"
        busy={rejectionActions.returnToMaterials.isPending}
        {...(dialogError ? { error: dialogError } : {})}
        onConfirm={confirmReturn}
        onOpenChange={(open) => {
          if (!open) {
            setReturnRecord(null)
            setDialogError('')
          }
        }}
      />

      <ConfirmActionDialog
        open={approveOpen}
        title="标记批次通过"
        description={`将把 ${approveBatchIds.length} 个批次${reviewBatchIds.length ? '（已选批次）' : '（当前列表涉及的全部批次）'}标记为人工审核通过。该操作只记录审核结论，不改变门店任务状态，可在批次管理中撤销。`}
        confirmLabel="标记通过"
        busy={reviewActions.approve.isPending}
        {...(dialogError ? { error: dialogError } : {})}
        onConfirm={confirmApprove}
        onOpenChange={(open) => {
          if (!open) {
            setApproveOpen(false)
            setDialogError('')
          }
        }}
      />

      <ConfirmActionDialog
        open={unapproveOpen}
        title="撤销批次通过"
        description={`将撤销 ${approveBatchIds.length} 个批次${reviewBatchIds.length ? '（已选批次）' : '（当前列表涉及的全部批次）'}的人工审核通过标记，批次回到自动阶段判断。`}
        confirmLabel="撤销通过"
        busy={reviewActions.unapprove.isPending}
        {...(dialogError ? { error: dialogError } : {})}
        onConfirm={confirmUnapprove}
        onOpenChange={(open) => {
          if (!open) {
            setUnapproveOpen(false)
            setDialogError('')
          }
        }}
      />

      <AssignDialog
        open={assignTasks !== null}
        onOpenChange={(open) => {
          if (!open) setAssignTasks(null)
        }}
        tasks={assignTasks ?? []}
        workers={hubShared.data?.workers ?? []}
        onExclude={(ids) => {
          setSelectedTasks((current) => {
            const next = new Set(current)
            for (const id of ids) next.delete(id)
            return next
          })
        }}
        onAssigned={(count, workerName) => {
          setAssignTasks(null)
          setSelectedTasks(new Set())
          toast.push(`已向 ${workerName} 分配 ${count} 家门店`, 'success')
          void queryClient.invalidateQueries({ queryKey: ['quality'] })
          void queryClient.invalidateQueries({ queryKey: ['hub-status'] })
          void queryClient.invalidateQueries({ queryKey: ['hub-shared-status'] })
          void queryClient.invalidateQueries({ queryKey: ['hub-assignments'] })
        }}
      />
    </section>
  )
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-xl border border-subtle bg-surface px-4 py-3">
      <span className="block text-xs text-secondary">{label}</span>
      <span className="mt-1 block heading-lg tabular-nums text-default">{value}</span>
    </div>
  )
}
