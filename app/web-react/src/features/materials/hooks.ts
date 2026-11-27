import { useCallback, useEffect, useRef, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  abortStoreUpload,
  abortUpload,
  acceptRecommendations,
  cancelJob,
  activateVersion,
  beginStoreUpload,
  beginUpload,
  clearActiveSelection,
  commitStoreUpload,
  createLocalBatch,
  decideStore,
  deleteBatch,
  fetchBatch,
  fetchBatchManagement,
  fetchBatchOptions,
  fetchBatches,
  fetchJob,
  fetchJobs,
  fetchRework,
  fetchStore,
  finishUpload,
  pauseJob,
  publishBatch,
  publishRework,
  purgeBatch,
  reanalyzeStore,
  restoreBatch,
  resumeJob,
  retryFailed,
  returnReworkStore,
  setBatchStage,
  startAnalysis,
  startReanalysis,
  updateBatch,
  uploadBatchFile,
  uploadStoreFile,
} from './api'
import {
  MAX_STORE_UPLOAD_BYTES,
  MAX_STORE_UPLOAD_FILES,
  isJobActive,
  relativePathOf,
} from './format'
import type {
  DecisionPayload,
  MaterialBatchDetail,
  UploadMode,
  UploadedStoreFile,
} from './types'
import { useProject } from '../projects/ProjectProvider'
import { projectQueryKey } from '../projects/scope'

const prefix = (projectId: string) => projectQueryKey(projectId, 'materials')
export const materialsKeys = {
  all: (projectId: string) => prefix(projectId),
  batches: (projectId: string) => [...prefix(projectId), 'batches'] as const,
  batchOptions: (projectId: string) => [...prefix(projectId), 'batches', 'options'] as const,
  batch: (projectId: string, batchId: string) => [...prefix(projectId), 'batch', batchId] as const,
  store: (projectId: string, batchId: string, storeId: string) => [...prefix(projectId), 'store', batchId, storeId] as const,
  jobs: (projectId: string, batchId: string) => [...prefix(projectId), 'jobs', batchId] as const,
  job: (projectId: string, jobId: string) => [...prefix(projectId), 'job', jobId] as const,
  rework: (projectId: string, query = '') => [...prefix(projectId), 'rework', query] as const,
  management: (projectId: string, deleted: boolean) => [...prefix(projectId), 'management', deleted] as const,
}

/** 分析或上传进行中时轮询批次明细，其余时间不主动刷新。 */
function batchPollInterval(batch: MaterialBatchDetail | undefined): number | false {
  if (!batch) return false
  if (isJobActive(batch.job?.status)) return 3000
  if (['uploading', 'queued', 'analyzing'].includes(batch.status)) return 4000
  return false
}

export function useBatches() {
  const { projectId } = useProject()
  return useQuery({
    queryKey: materialsKeys.batches(projectId),
    queryFn: ({ signal }) => fetchBatches(signal, projectId),
    enabled: Boolean(projectId),
    select: (data) => data.batches,
  })
}

export function useBatchOptions() {
  const { projectId } = useProject()
  return useQuery({
    queryKey: materialsKeys.batchOptions(projectId),
    queryFn: ({ signal }) => fetchBatchOptions(signal, projectId),
    enabled: Boolean(projectId),
    select: (data) => data.batches,
  })
}

export function useBatch(batchId: string) {
  const { projectId } = useProject()
  return useQuery({
    queryKey: materialsKeys.batch(projectId, batchId),
    queryFn: ({ signal }) => fetchBatch(batchId, signal, projectId),
    enabled: !!projectId && !!batchId,
    placeholderData: keepPreviousData,
    refetchInterval: (query) => batchPollInterval(query.state.data),
  })
}

export function useStore(batchId: string, storeId: string) {
  const { projectId } = useProject()
  return useQuery({
    queryKey: materialsKeys.store(projectId, batchId, storeId),
    queryFn: ({ signal }) => fetchStore(batchId, storeId, signal, projectId),
    enabled: !!projectId && !!batchId && !!storeId,
  })
}

export function useJobs(batchId: string, enabled: boolean) {
  const { projectId } = useProject()
  return useQuery({
    queryKey: materialsKeys.jobs(projectId, batchId),
    queryFn: ({ signal }) => fetchJobs(batchId, signal, projectId),
    enabled: enabled && !!projectId && !!batchId,
    refetchInterval: (query) => {
      const jobs = query.state.data?.jobs ?? []
      return jobs.some((job) => isJobActive(job.status)) ? 2000 : false
    },
  })
}

export function useJob(jobId: string, enabled: boolean) {
  const { projectId } = useProject()
  return useQuery({
    queryKey: materialsKeys.job(projectId, jobId),
    queryFn: ({ signal }) => fetchJob(jobId, signal, projectId),
    enabled: enabled && !!projectId && !!jobId,
    refetchInterval: (query) => (isJobActive(query.state.data?.status) ? 1500 : false),
  })
}

export function useRework(query = '') {
  const { projectId } = useProject()
  return useQuery({
    queryKey: materialsKeys.rework(projectId, query),
    queryFn: ({ signal }) => fetchRework(query, signal, projectId),
    enabled: Boolean(projectId),
  })
}

export function useBatchManagement(deleted: boolean, enabled: boolean) {
  const { projectId } = useProject()
  return useQuery({
    queryKey: materialsKeys.management(projectId, deleted),
    queryFn: ({ signal }) => fetchBatchManagement({ deleted }, signal, projectId),
    enabled: enabled && Boolean(projectId),
    select: (data) => data.batches,
  })
}

/** 批次级写操作统一失效批次列表、选项与当前明细。 */
export function useBatchActions(batchId: string) {
  const { projectId } = useProject()
  const queryClient = useQueryClient()
  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: materialsKeys.batches(projectId) })
    void queryClient.invalidateQueries({ queryKey: materialsKeys.batchOptions(projectId) })
    if (batchId) void queryClient.invalidateQueries({ queryKey: materialsKeys.batch(projectId, batchId) })
  }, [batchId, projectId, queryClient])

  const createLocal = useMutation({ mutationFn: (payload: Parameters<typeof createLocalBatch>[0]) => createLocalBatch(payload, projectId), onSuccess: invalidate })
  const analyze = useMutation({ mutationFn: () => startAnalysis(batchId, projectId), onSuccess: invalidate })
  const reanalyze = useMutation({
    mutationFn: (storeIds: string[]) => startReanalysis(batchId, storeIds, projectId),
    onSuccess: invalidate,
  })
  const retry = useMutation({ mutationFn: () => retryFailed(batchId, projectId), onSuccess: invalidate })
  const accept = useMutation({ mutationFn: () => acceptRecommendations(batchId, projectId), onSuccess: invalidate })
  const remove = useMutation({ mutationFn: () => deleteBatch(batchId, projectId), onSuccess: invalidate })
  const restore = useMutation({ mutationFn: () => restoreBatch(batchId, projectId), onSuccess: invalidate })
  const purge = useMutation({ mutationFn: () => purgeBatch(batchId, projectId), onSuccess: invalidate })
  const clearSelection = useMutation({ mutationFn: () => clearActiveSelection(batchId, projectId), onSuccess: invalidate })
  const setStage = useMutation({
    mutationFn: (stage: string) => setBatchStage(batchId, stage, projectId),
    onSuccess: invalidate,
  })
  const update = useMutation({
    mutationFn: (payload: { name: string; description: string }) => updateBatch(batchId, payload, projectId),
    onSuccess: invalidate,
  })
  const publish = useMutation({
    mutationFn: (payload: { name: string; version: string; description: string }) => publishBatch(batchId, payload, projectId),
    onSuccess: invalidate,
  })
  const activate = useMutation({
    mutationFn: (version: string) => activateVersion(batchId, version, projectId),
    onSuccess: invalidate,
  })
  const pause = useMutation({ mutationFn: (jobId: string) => pauseJob(jobId, projectId), onSuccess: invalidate })
  const resume = useMutation({ mutationFn: (jobId: string) => resumeJob(jobId, projectId), onSuccess: invalidate })
  const cancel = useMutation({ mutationFn: (jobId: string) => cancelJob(jobId, projectId), onSuccess: invalidate })

  return {
    createLocal,
    analyze,
    reanalyze,
    retry,
    accept,
    remove,
    restore,
    purge,
    clearSelection,
    setStage,
    update,
    publish,
    activate,
    pause,
    resume,
    cancel,
  }
}

export interface BatchUploadProgress {
  done: number
  total: number
  bytes: number
  totalBytes: number
  current: string
}

/** 上传整批素材：先创建上传批次，再逐个 PUT，最后 finish 触发分析准备。 */
export function useBatchUpload() {
  const { projectId } = useProject()
  const queryClient = useQueryClient()
  const [progress, setProgress] = useState<BatchUploadProgress | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => () => abortRef.current?.abort(), [])

  const run = useCallback(
    async (files: File[], meta: { name: string; description: string }): Promise<MaterialBatchDetail> => {
      setRunning(true)
      setError(null)
      const controller = new AbortController()
      abortRef.current = controller
      const totalBytes = files.reduce((sum, file) => sum + file.size, 0)
      setProgress({ done: 0, total: files.length, bytes: 0, totalBytes, current: files[0]?.name ?? '' })
      let batchId = ''
      try {
        const started = await beginUpload(meta, projectId)
        batchId = started.batch_id
        let bytes = 0
        for (let index = 0; index < files.length; index += 1) {
          if (controller.signal.aborted) throw new Error('上传已取消')
          const file = files[index]!
          await uploadBatchFile(batchId, relativePathOf(file), file, projectId, controller.signal)
          bytes += file.size
          setProgress({
            done: index + 1,
            total: files.length,
            bytes,
            totalBytes,
            current: files[index + 1]?.name ?? '',
          })
        }
        if (controller.signal.aborted) throw new Error('上传已取消')
        const batch = await finishUpload(batchId, projectId)
        await queryClient.invalidateQueries({ queryKey: materialsKeys.batches(projectId) })
        await queryClient.invalidateQueries({ queryKey: materialsKeys.batchOptions(projectId) })
        setProgress(null)
        return batch
      } catch (cause) {
        if (batchId) await abortUpload(batchId, projectId).catch(() => undefined)
        const message = cause instanceof Error ? cause.message : '上传失败'
        setError(message)
        throw cause
      } finally {
        setRunning(false)
        abortRef.current = null
      }
    },
    [projectId, queryClient],
  )

  const cancel = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  return { run, cancel, progress, error, running }
}

/** 单店写操作：决策、单店重分析、补图。 */
export function useStoreActions(batchId: string, storeId: string) {
  const { projectId } = useProject()
  const queryClient = useQueryClient()
  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: materialsKeys.store(projectId, batchId, storeId) })
    void queryClient.invalidateQueries({ queryKey: materialsKeys.batch(projectId, batchId) })
    void queryClient.invalidateQueries({ queryKey: materialsKeys.batches(projectId) })
    void queryClient.invalidateQueries({ queryKey: [...prefix(projectId), 'rework'] })
  }, [batchId, projectId, queryClient, storeId])

  const decide = useMutation({
    mutationFn: (payload: DecisionPayload) => decideStore(batchId, storeId, payload, projectId),
    onSuccess: (store) => {
      queryClient.setQueryData(materialsKeys.store(projectId, batchId, storeId), store)
      invalidate()
    },
  })
  const reanalyze = useMutation({
    mutationFn: () => reanalyzeStore(batchId, storeId, projectId),
    onSuccess: (store) => {
      queryClient.setQueryData(materialsKeys.store(projectId, batchId, storeId), store)
      invalidate()
    },
  })

  const supplement = useCallback(
    async (files: File[], mode: UploadMode, onProgress?: (done: number, total: number) => void) => {
      const totalBytes = files.reduce((sum, file) => sum + file.size, 0)
      if (!files.length) throw new Error('请选择要补充的图片')
      if (files.length > MAX_STORE_UPLOAD_FILES) throw new Error(`单店最多补充 ${MAX_STORE_UPLOAD_FILES} 张图片`)
      if (totalBytes > MAX_STORE_UPLOAD_BYTES) throw new Error('单店补充图片总大小超过上限')
      const session = await beginStoreUpload(batchId, storeId, projectId)
      const uploaded: UploadedStoreFile[] = []
      try {
        for (let index = 0; index < files.length; index += 1) {
          const file = files[index]!
          uploaded.push(await uploadStoreFile(batchId, storeId, session.upload_id, file.name, file, projectId))
          onProgress?.(index + 1, files.length)
        }
        const result = await commitStoreUpload(batchId, storeId, session.upload_id, { mode, files: uploaded }, projectId)
        queryClient.setQueryData(materialsKeys.store(projectId, batchId, storeId), result.store)
        invalidate()
        return result
      } catch (cause) {
        await abortStoreUpload(batchId, storeId, session.upload_id, projectId).catch(() => undefined)
        throw cause
      }
    },
    [batchId, invalidate, projectId, queryClient, storeId],
  )

  return { decide, reanalyze, supplement }
}

export function useReworkActions() {
  const { projectId } = useProject()
  const queryClient = useQueryClient()
  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: [...prefix(projectId), 'rework'] })
    void queryClient.invalidateQueries({ queryKey: materialsKeys.batches(projectId) })
  }, [projectId, queryClient])

  const returnStore = useMutation({
    mutationFn: (payload: { batchId: string; storeId: string }) =>
      returnReworkStore(payload.batchId, payload.storeId, projectId),
    onSuccess: invalidate,
  })
  const publish = useMutation({
    mutationFn: (storeKeys: string[]) => publishRework(storeKeys, '跨批次返工发布', projectId),
    onSuccess: invalidate,
  })
  return { returnStore, publish }
}
