/** 质量审核查询与动作：列表按服务端筛选，变更后同时刷新素材域数据。 */

import { useCallback } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  approveBatches,
  fetchHubRunning,
  fetchPendingReviews,
  fetchRejections,
  markRejectionReviewed,
  rejectPendingTask,
  retryCaptureTask,
  returnRejectionToMaterials,
  unapproveBatches,
  undoCaptureRejection,
  undoMaterialRejection,
  undoRejectionsBatch,
} from './api'
import { errorMessage } from '../../api/client'
import type { RejectionQuery } from './types'
import { useProject } from '../projects/ProjectProvider'
import { projectQueryKey } from '../projects/scope'

const prefix = (projectId: string) => projectQueryKey(projectId, 'quality')
export const qualityKeys = {
  rejections: (projectId: string, query: RejectionQuery) =>
    [...prefix(projectId), 'rejections', query.batchId, query.source, query.reason, query.q, query.reviewed] as const,
  reviews: (projectId: string, batchIds: string[], q: string) => [...prefix(projectId), 'reviews', batchIds.join(','), q] as const,
  running: (projectId: string) => [...prefix(projectId), 'hub-running'] as const,
}

export function useRejections(query: RejectionQuery, enabled = true) {
  const { projectId } = useProject()
  return useQuery({
    queryKey: qualityKeys.rejections(projectId, query),
    queryFn: ({ signal }) => fetchRejections(query, signal, projectId),
    enabled: enabled && Boolean(projectId),
    placeholderData: (previous, previousQuery) => previousQuery?.queryKey[1] === projectId ? previous : undefined,
  })
}

export function usePendingReviews(batchIds: string[], q: string, enabled = true) {
  const { projectId } = useProject()
  return useQuery({
    queryKey: qualityKeys.reviews(projectId, batchIds, q),
    queryFn: ({ signal }) => fetchPendingReviews(batchIds, q, signal, projectId),
    enabled: enabled && Boolean(projectId),
    placeholderData: (previous, previousQuery) => previousQuery?.queryKey[1] === projectId ? previous : undefined,
  })
}

/** 采集运行中，不合格撤销/打回/重采会被后端拒绝，这里提前禁用。 */
export function useHubRunning() {
  const { projectId } = useProject()
  return useQuery({
    queryKey: qualityKeys.running(projectId || 'unselected'),
    queryFn: ({ signal }) => fetchHubRunning(signal, projectId),
    enabled: Boolean(projectId),
    refetchInterval: 5000,
    staleTime: 4000,
  })
}

function useRefreshQuality() {
  const { projectId } = useProject()
  const queryClient = useQueryClient()
  return useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: prefix(projectId) })
    void queryClient.invalidateQueries({ queryKey: projectQueryKey(projectId, 'materials') })
    void queryClient.invalidateQueries({ queryKey: projectQueryKey(projectId, 'hub-status') })
    void queryClient.invalidateQueries({ queryKey: projectQueryKey(projectId, 'hub-shared-status') })
  }, [projectId, queryClient])
}

export function useRejectionActions() {
  const { projectId } = useProject()
  const refresh = useRefreshQuality()
  return {
    reviewed: useMutation({ mutationFn: (payload: Parameters<typeof markRejectionReviewed>[0]) => markRejectionReviewed(payload, projectId), onSuccess: refresh }),
    undoCapture: useMutation({ mutationFn: (taskId: string) => undoCaptureRejection(taskId, projectId), onSuccess: refresh }),
    undoMaterials: useMutation({
      mutationFn: (payload: { batchId: string; storeId: string }) =>
        undoMaterialRejection(payload.batchId, payload.storeId, projectId),
      onSuccess: refresh,
    }),
    undoBatch: useMutation({ mutationFn: (items: Parameters<typeof undoRejectionsBatch>[0]) => undoRejectionsBatch(items, projectId), onSuccess: refresh }),
    returnToMaterials: useMutation({ mutationFn: (taskId: string) => returnRejectionToMaterials(taskId, projectId), onSuccess: refresh }),
    retry: useMutation({ mutationFn: (taskId: string) => retryCaptureTask(taskId, projectId), onSuccess: refresh }),
  }
}

export interface RejectPendingPayload {
  taskIds: string[]
  reason: string
  detail: string
}

export function useReviewActions() {
  const { projectId } = useProject()
  const refresh = useRefreshQuality()
  return {
    approve: useMutation({ mutationFn: (batchIds: string[]) => approveBatches(batchIds, projectId), onSuccess: refresh }),
    unapprove: useMutation({ mutationFn: (batchIds: string[]) => unapproveBatches(batchIds, projectId), onSuccess: refresh }),
    // 后端按单任务校验并写入，这里顺序提交以便逐项返回失败，避免一次失败吞掉整批。
    reject: useMutation({
      mutationFn: async ({ taskIds, reason, detail }: RejectPendingPayload) => {
        const failed: string[] = []
        let firstError = ''
        for (const taskId of taskIds) {
          try {
            await rejectPendingTask(taskId, reason, detail, projectId)
          } catch (cause) {
            failed.push(taskId)
            if (!firstError) firstError = errorMessage(cause)
          }
        }
        return { rejected: taskIds.length - failed.length, failed, error: firstError }
      },
      onSuccess: refresh,
    }),
  }
}
