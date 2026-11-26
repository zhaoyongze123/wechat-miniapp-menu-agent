/** 合格成品目录查询与动作：同步中短轮询，稳定后低频刷新。 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useProject } from '../projects/ProjectProvider'
import { projectQueryKey } from '../projects/scope'
import { fetchDeliveryStatus, fetchDeliveryStores, revealDelivery, triggerDeliverySync } from './api'

export const deliveryKeys = (projectId: string) => ({
  status: projectQueryKey(projectId, 'delivery', 'status'),
  stores: (page: number, size: number, q: string, batchId: string) =>
    projectQueryKey(projectId, 'delivery', 'stores', page, size, q, batchId),
})

export function useDeliveryStatus() {
  const { projectId } = useProject()
  return useQuery({
    queryKey: projectId ? deliveryKeys(projectId).status : ['project', '', 'delivery', 'status'],
    queryFn: ({ signal }) => fetchDeliveryStatus(signal, projectId),
    enabled: Boolean(projectId),
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
  const { projectId } = useProject()
  return useQuery({
    queryKey: projectId ? deliveryKeys(projectId).stores(params.page, params.size, params.q, params.batchId) : ['project', '', 'delivery', 'stores'],
    queryFn: ({ signal }) => fetchDeliveryStores(params, signal, projectId),
    enabled: enabled && Boolean(projectId),
    placeholderData: (previous, previousQuery) => previousQuery?.queryKey[0] === 'project' && previousQuery.queryKey[1] === projectId ? previous : undefined,
  })
}

export function useDeliveryActions() {
  const queryClient = useQueryClient()
  const { projectId } = useProject()
  const refresh = () => {
    if (projectId) void queryClient.invalidateQueries({ queryKey: projectQueryKey(projectId, 'delivery') })
  }
  return {
    sync: useMutation({ mutationFn: () => triggerDeliverySync(projectId), onSuccess: refresh }),
    reveal: useMutation({ mutationFn: () => revealDelivery(projectId) }),
  }
}
