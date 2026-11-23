/** 质量审核查询与动作：列表按服务端筛选，变更后同时刷新素材域数据。 */

import { useCallback } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  approveBatches,
  fetchHubRunning,
  fetchPendingReviews,
  fetchRejections,
  markRejectionReviewed,
  retryCaptureTask,
  returnRejectionToMaterials,
  unapproveBatches,
  undoCaptureRejection,
  undoMaterialRejection,
} from './api'
import type { RejectionQuery } from './types'

export const qualityKeys = {
  rejections: (query: RejectionQuery) =>
    ['quality', 'rejections', query.batchId, query.source, query.reason, query.q] as const,
  reviews: (batchIds: string[], q: string) => ['quality', 'reviews', batchIds.join(','), q] as const,
  running: () => ['quality', 'hub-running'] as const,
}

export function useRejections(query: RejectionQuery, enabled = true) {
  return useQuery({
    queryKey: qualityKeys.rejections(query),
    queryFn: ({ signal }) => fetchRejections(query, signal),
    enabled,
    placeholderData: keepPreviousData,
  })
}

export function usePendingReviews(batchIds: string[], q: string, enabled = true) {
  return useQuery({
    queryKey: qualityKeys.reviews(batchIds, q),
    queryFn: ({ signal }) => fetchPendingReviews(batchIds, q, signal),
    enabled,
    placeholderData: keepPreviousData,
  })
}

/** 采集运行中，不合格撤销/打回/重采会被后端拒绝，这里提前禁用。 */
export function useHubRunning() {
  return useQuery({
    queryKey: qualityKeys.running(),
    queryFn: ({ signal }) => fetchHubRunning(signal),
    refetchInterval: 5000,
    staleTime: 4000,
  })
}

function useRefreshQuality() {
  const queryClient = useQueryClient()
  return useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['quality'] })
    void queryClient.invalidateQueries({ queryKey: ['materials'] })
  }, [queryClient])
}

export function useRejectionActions() {
  const refresh = useRefreshQuality()
  return {
    reviewed: useMutation({ mutationFn: markRejectionReviewed, onSuccess: refresh }),
    undoCapture: useMutation({ mutationFn: undoCaptureRejection, onSuccess: refresh }),
    undoMaterials: useMutation({
      mutationFn: (payload: { batchId: string; storeId: string }) =>
        undoMaterialRejection(payload.batchId, payload.storeId),
      onSuccess: refresh,
    }),
    returnToMaterials: useMutation({ mutationFn: returnRejectionToMaterials, onSuccess: refresh }),
    retry: useMutation({ mutationFn: retryCaptureTask, onSuccess: refresh }),
  }
}

export function useReviewActions() {
  const refresh = useRefreshQuality()
  return {
    approve: useMutation({ mutationFn: approveBatches, onSuccess: refresh }),
    unapprove: useMutation({ mutationFn: unapproveBatches, onSuccess: refresh }),
  }
}
