import { QueryClient } from '@tanstack/react-query'

/**
 * 查询策略：
 * - 读取失败允许一次重试（网络抖动），写操作永不自动重试；
 * - 页面隐藏时 TanStack 默认停止 interval 轮询，重新可见时立即补一次。
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      refetchIntervalInBackground: false,
      staleTime: 15_000,
      gcTime: 10 * 60_000,
    },
    mutations: {
      retry: false,
    },
  },
})
