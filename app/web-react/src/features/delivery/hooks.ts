/** 合格成品目录查询与动作：同步中短轮询，稳定后低频刷新。 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchDeliveryStatus, fetchDeliveryStores, revealDelivery, triggerDeliverySync } from './api'

export const deliveryKeys = {
  status: () => ['delivery', 'status'] as const,
  stores: (page: number, size: number, q: string, batchId: string) =>
    ['delivery', 'stores', page, size, q, batchId] as const,
}

export function useDeliveryStatus() {
  return useQuery({
    queryKey: deliveryKeys.status(),
    queryFn: ({ signal }) => fetchDeliveryStatus(signal),
    refetchInterval: (query) => {
      const state = query.state.data?.state
      return state === 'initializing' || state === 'syncing' ? 3000 : 30000
    },
  })
}

export function useDeliveryStores(
  params: { page: number; size: number; q: string; batchId: string },
  enabled = true,
) {
  return useQuery({
    queryKey: deliveryKeys.stores(params.page, params.size, params.q, params.batchId),
    queryFn: ({ signal }) => fetchDeliveryStores(params, signal),
    enabled,
    placeholderData: keepPreviousData,
  })
}

export function useDeliveryActions() {
  const queryClient = useQueryClient()
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['delivery'] })
  }
  return {
    sync: useMutation({ mutationFn: triggerDeliverySync, onSuccess: refresh }),
    reveal: useMutation({ mutationFn: revealDelivery }),
  }
}
