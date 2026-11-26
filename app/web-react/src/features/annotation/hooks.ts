import { useQuery } from '@tanstack/react-query'
import { fetchAnnotationState, fetchAnnotationTasks } from './api'
import { useProject } from '../projects/ProjectProvider'
import { projectQueryKey } from '../projects/scope'

export const ANNOTATION_STATE_KEY = (projectId: string) => projectQueryKey(projectId, 'annotation', 'state')
export const ANNOTATION_TASKS_KEY = (projectId: string) => projectQueryKey(projectId, 'annotation', 'tasks')

interface StateOptions {
  enabled: boolean
  /** 操作进行中时暂停轮询，避免服务端返回的中间态覆盖本地结果。 */
  paused: boolean
}

export function useAnnotationState({ enabled, paused }: StateOptions) {
  const { projectId } = useProject()
  return useQuery({
    queryKey: ANNOTATION_STATE_KEY(projectId || 'unselected'),
    queryFn: ({ signal }) => fetchAnnotationState(signal, projectId),
    enabled: enabled && Boolean(projectId),
    refetchInterval: paused ? false : 2000,
    refetchIntervalInBackground: false,
    staleTime: 0,
  })
}

/** 标注排队变化不频繁，只在进入页面、标记不合格或完成后刷新。 */
export function useAnnotationTasks(enabled: boolean) {
  const { projectId } = useProject()
  return useQuery({
    queryKey: ANNOTATION_TASKS_KEY(projectId || 'unselected'),
    queryFn: ({ signal }) => fetchAnnotationTasks(signal, projectId),
    enabled: enabled && Boolean(projectId),
    staleTime: 15_000,
  })
}
