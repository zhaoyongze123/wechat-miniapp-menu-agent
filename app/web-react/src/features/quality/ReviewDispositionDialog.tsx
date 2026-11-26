import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { AppDialog } from '../../components/AppDialog'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { StatePanel } from '../../components/StatePanel'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { formatStore } from '../hub/format'
import { fetchPendingReviews } from './api'
import { useRejectionActions, useReviewActions } from './hooks'
import { PendingReviewActionButtons, PendingReviewContent } from './PendingReviewDrawer'
import { RejectPendingDialog } from './RejectPendingDialog'
import { useProject } from '../projects/ProjectProvider'
import { projectQueryKey } from '../projects/scope'
import type { PendingReviewRecord } from './types'

interface ReviewDispositionDialogProps {
  open: boolean
  taskId: string | null
  storeName?: string
  /** 处置成功或用户关闭时回调；调用方保持原任务详情不清空。 */
  onOpenChange: (open: boolean) => void
}

/**
 * 任务详情内的就地处置。
 *
 * 从任务中心直接打开，不跳转页面；关闭后仍停留在原任务详情与原有门店位置，
 * 避免“看任务 → 跳质量审核 → 丢失任务上下文”的往返。
 */
export function ReviewDispositionDialog({ open, taskId, storeName, onOpenChange }: ReviewDispositionDialogProps) {
  const queryClient = useQueryClient()
  const { projectId } = useProject()
  const toast = useToast()
  const reviewActions = useReviewActions()
  const rejectionActions = useRejectionActions()
  const [rejectOpen, setRejectOpen] = useState(false)
  const [rejectError, setRejectError] = useState('')
  const [returnOpen, setReturnOpen] = useState(false)
  const [returnError, setReturnError] = useState('')

  // 处置对象来自待检查队列本身，前端不根据任务字段推断是否可处置。
  const pendingQuery = useQuery({
    queryKey: projectQueryKey(projectId || 'unselected', 'pending-review', taskId ?? ''),
    queryFn: ({ signal }) => fetchPendingReviews([], '', signal, projectId),
    enabled: open && Boolean(taskId && projectId),
    retry: false,
  })
  const record: PendingReviewRecord | null =
    pendingQuery.data?.records.find((item) => item.task_id === taskId) ?? null

  const refresh = () => {
    if (!projectId) return
    void queryClient.invalidateQueries({ queryKey: projectQueryKey(projectId, 'task-detail') })
    void queryClient.invalidateQueries({ queryKey: projectQueryKey(projectId, 'quality') })
    void queryClient.invalidateQueries({ queryKey: projectQueryKey(projectId, 'pending-review') })
  }

  const confirmReject = (reason: string, detail: string) => {
    if (!taskId) return
    reviewActions.reject.mutate(
      { taskIds: [taskId], reason, detail },
      {
        onSuccess: ({ failed, error }) => {
          if (failed.length) {
            setRejectError(error || '标记失败，请重试')
            return
          }
          setRejectOpen(false)
          setRejectError('')
          refresh()
          toast.push('已标记为不合格', 'success')
          onOpenChange(false)
        },
        onError: (error) => setRejectError(errorMessage(error)),
      },
    )
  }

  const confirmReturn = () => {
    if (!taskId) return
    rejectionActions.returnToMaterials.mutate(taskId, {
      onSuccess: () => {
        setReturnOpen(false)
        setReturnError('')
        refresh()
        toast.push('已打回素材清洗', 'success')
        onOpenChange(false)
      },
      onError: (error) => setReturnError(errorMessage(error)),
    })
  }

  return (
    <>
      <AppDialog
        open={open}
        onOpenChange={onOpenChange}
        title={record ? formatStore(record.store_name).name : (storeName ? formatStore(storeName).name : '待检查门店')}
        description="在当前任务内直接处置，关闭后回到原任务详情。"
        widthClass="w-[min(820px,94vw)]"
        footer={
          record ? (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <span className="text-xs text-secondary">处置结果立即生效，并刷新当前任务详情。</span>
              <div className="flex flex-wrap items-center gap-2">
                <PendingReviewActionButtons
                  record={record}
                  onReject={() => {
                    setRejectError('')
                    setRejectOpen(true)
                  }}
                  onReturn={() => {
                    setReturnError('')
                    setReturnOpen(true)
                  }}
                  onRerun={() => {
                    toast.push('重跑需要在质量审核中选择采集电脑，请从质量审核列表操作。', 'info')
                  }}
                />
              </div>
            </div>
          ) : (
            <div className="flex justify-end gap-2">
              <Button color="secondary" variant="outline" size="sm" onClick={() => onOpenChange(false)}>
                关闭
              </Button>
            </div>
          )
        }
      >
        {!taskId ? null : pendingQuery.isError ? (
          <Alert color="danger" variant="soft" title="待检查记录读取失败" description={errorMessage(pendingQuery.error)} />
        ) : pendingQuery.isPending ? (
          <StatePanel variant="loading" title="正在读取待检查记录…" />
        ) : !record ? (
          <StatePanel
            variant="empty"
            title="该门店已不在待检查队列"
            description="可能已在其他页面处置，或任务状态已推进。关闭后可刷新任务详情查看最新结果。"
          />
        ) : (
          <PendingReviewContent record={record} />
        )}
      </AppDialog>

      <RejectPendingDialog
        open={rejectOpen}
        count={1}
        busy={reviewActions.reject.isPending}
        error={rejectError}
        onConfirm={confirmReject}
        onOpenChange={(next) => {
          if (next || reviewActions.reject.isPending) return
          setRejectOpen(false)
          setRejectError('')
        }}
      />

      <ConfirmActionDialog
        open={returnOpen}
        title="打回素材清洗"
        description={
          <div className="space-y-2">
            <p>将解除本次采集结论，门店回到素材清洗重新确认二维码；已产生的采集结果保留为历史记录。</p>
            {returnError ? <p className="text-xs text-danger">{returnError}</p> : null}
          </div>
        }
        confirmLabel="确认打回"
        busy={rejectionActions.returnToMaterials.isPending}
        onConfirm={confirmReturn}
        onOpenChange={(next) => {
          if (next || rejectionActions.returnToMaterials.isPending) return
          setReturnOpen(false)
          setReturnError('')
        }}
      />
    </>
  )
}
