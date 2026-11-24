import { useQuery } from '@tanstack/react-query'
import { fetchAnnotationState, fetchAnnotationTasks } from './api'

export const ANNOTATION_STATE_KEY = ['annotation', 'state'] as const
export const ANNOTATION_TASKS_KEY = ['annotation', 'tasks'] as const

interface StateOptions {
  enabled: boolean
  /** 操作进行中时暂停轮询，避免服务端返回的中间态覆盖本地结果。 */
  paused: boolean
}

export function useAnnotationState({ enabled, paused }: StateOptions) {
  return useQuery({
    queryKey: ANNOTATION_STATE_KEY,
    queryFn: ({ signal }) => fetchAnnotationState(signal),
    enabled,
    refetchInterval: paused ? false : 2000,
    refetchIntervalInBackground: false,
    staleTime: 0,
  })
}

/** 标注排队变化不频繁，只在进入页面、标记不合格或完成后刷新。 */
export function useAnnotationTasks(enabled: boolean) {
  return useQuery({
    queryKey: ANNOTATION_TASKS_KEY,
    queryFn: ({ signal }) => fetchAnnotationTasks(signal),
    enabled,
    staleTime: 15_000,
  })
}
