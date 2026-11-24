import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  cancelUpdateDownload,
  checkUpdates,
  fetchUpdateCleanup,
  fetchUpdateHistory,
  fetchUpdateInstall,
  fetchUpdateStatus,
  runUpdateCleanup,
  saveUpdatePreferences,
  startUpdateDownload,
  startUpdateInstall,
} from './api'

export const updateKeys = {
  status: ['updates', 'status'] as const,
  install: ['updates', 'install'] as const,
  history: ['updates', 'history'] as const,
  cleanup: ['updates', 'cleanup'] as const,
}

/** 服务端按小时缓存；下载进行中才加密轮询，其余情况不为每台机器引入高频请求。 */
export function useUpdateStatus(enabled = true) {
  return useQuery({
    queryKey: updateKeys.status,
    queryFn: ({ signal }) => fetchUpdateStatus(signal),
    enabled,
    staleTime: 30_000,
    refetchInterval: (query) => (query.state.data?.download.active ? 2_000 : 5 * 60_000),
  })
}

/** 安装状态：安装期间服务会重启，读取失败不代表失败，仍保持短间隔轮询直到终态。 */
export function useUpdateInstall(enabled = true) {
  return useQuery({
    queryKey: updateKeys.install,
    queryFn: ({ signal }) => fetchUpdateInstall(signal),
    enabled,
    staleTime: 15_000,
    refetchInterval: (query) => (query.state.data?.active ? 2_000 : 60_000),
    retry: 2,
  })
}

/** 存储治理预览只读扫描；不自动删除，只有用户确认后才提交清理。 */
export function useUpdateCleanup(enabled = true) {
  return useQuery({
    queryKey: updateKeys.cleanup,
    queryFn: ({ signal }) => fetchUpdateCleanup(signal),
    enabled,
    staleTime: 30_000,
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
    download: useMutation({ mutationFn: startUpdateDownload, onSuccess: refresh }),
    cancelDownload: useMutation({ mutationFn: cancelUpdateDownload, onSuccess: refresh }),
    install: useMutation({ mutationFn: startUpdateInstall, onSuccess: refresh }),
    cleanup: useMutation({ mutationFn: runUpdateCleanup, onSuccess: refresh }),
  }
}
