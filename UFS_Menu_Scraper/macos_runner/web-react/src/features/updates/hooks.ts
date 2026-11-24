import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { checkUpdates, fetchUpdateHistory, fetchUpdateStatus, saveUpdatePreferences } from './api'

export const updateKeys = {
  status: ['updates', 'status'] as const,
  history: ['updates', 'history'] as const,
}

/** 服务端按小时缓存；前端只做轻量刷新，不为每台机器引入高频轮询。 */
export function useUpdateStatus(enabled = true) {
  return useQuery({
    queryKey: updateKeys.status,
    queryFn: ({ signal }) => fetchUpdateStatus(signal),
    enabled,
    staleTime: 30_000,
    refetchInterval: 5 * 60_000,
  })
}

export function useUpdateHistory(enabled = true) {
  return useQuery({
    queryKey: updateKeys.history,
    queryFn: ({ signal }) => fetchUpdateHistory(50, signal),
    enabled,
    staleTime: 30_000,
  })
}

export function useUpdateActions() {
  const queryClient = useQueryClient()
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['updates'] })
  }
  return {
    check: useMutation({ mutationFn: checkUpdates, onSuccess: refresh }),
    savePreferences: useMutation({ mutationFn: saveUpdatePreferences, onSuccess: refresh }),
  }
}
