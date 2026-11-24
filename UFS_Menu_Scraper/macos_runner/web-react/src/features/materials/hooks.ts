import { useCallback, useRef, useState } from 'react'
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

export const materialsKeys = {
  all: ['materials'] as const,
  batches: () => ['materials', 'batches'] as const,
  batchOptions: () => ['materials', 'batches', 'options'] as const,
  batch: (batchId: string) => ['materials', 'batch', batchId] as const,
  store: (batchId: string, storeId: string) => ['materials', 'store', batchId, storeId] as const,
  jobs: (batchId: string) => ['materials', 'jobs', batchId] as const,
  job: (jobId: string) => ['materials', 'job', jobId] as const,
  rework: (query = '') => ['materials', 'rework', query] as const,
  management: (deleted: boolean) => ['materials', 'management', deleted] as const,
}

/** 分析或上传进行中时轮询批次明细，其余时间不主动刷新。 */
function batchPollInterval(batch: MaterialBatchDetail | undefined): number | false {
  if (!batch) return false
  if (isJobActive(batch.job?.status)) return 3000
  if (['uploading', 'queued', 'analyzing'].includes(batch.status)) return 4000
  return false
}

export function useBatches() {
  return useQuery({
    queryKey: materialsKeys.batches(),
    queryFn: ({ signal }) => fetchBatches(signal),
    select: (data) => data.batches,
  })
}

export function useBatchOptions() {
  return useQuery({
    queryKey: materialsKeys.batchOptions(),
    queryFn: ({ signal }) => fetchBatchOptions(signal),
    select: (data) => data.batches,
  })
}

export function useBatch(batchId: string) {
  return useQuery({
    queryKey: materialsKeys.batch(batchId),
    queryFn: ({ signal }) => fetchBatch(batchId, signal),
    enabled: !!batchId,
    refetchInterval: (query) => batchPollInterval(query.state.data),
  })
}

export function useStore(batchId: string, storeId: string) {
  return useQuery({
    queryKey: materialsKeys.store(batchId, storeId),
    queryFn: ({ signal }) => fetchStore(batchId, storeId, signal),
    enabled: !!batchId && !!storeId,
  })
}

export function useJobs(batchId: string, enabled: boolean) {
  return useQuery({
    queryKey: materialsKeys.jobs(batchId),
    queryFn: ({ signal }) => fetchJobs(batchId, signal),
    enabled: enabled && !!batchId,
    refetchInterval: (query) => {
      const jobs = query.state.data?.jobs ?? []
      return jobs.some((job) => isJobActive(job.status)) ? 2000 : false
    },
  })
}

export function useJob(jobId: string, enabled: boolean) {
  return useQuery({
    queryKey: materialsKeys.job(jobId),
    queryFn: ({ signal }) => fetchJob(jobId, signal),
    enabled: enabled && !!jobId,
    refetchInterval: (query) => (isJobActive(query.state.data?.status) ? 1500 : false),
  })
}

export function useRework(query = '') {
  return useQuery({
    queryKey: materialsKeys.rework(query),
    queryFn: ({ signal }) => fetchRework(query, signal),
    placeholderData: keepPreviousData,
  })
}

export function useBatchManagement(deleted: boolean, enabled: boolean) {
  return useQuery({
    queryKey: materialsKeys.management(deleted),
    queryFn: ({ signal }) => fetchBatchManagement({ deleted }, signal),
    enabled,
    select: (data) => data.batches,
  })
}

/** 批次级写操作统一失效批次列表、选项与当前明细。 */
export function useBatchActions(batchId: string) {
  const queryClient = useQueryClient()
  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: materialsKeys.batches() })
    void queryClient.invalidateQueries({ queryKey: materialsKeys.batchOptions() })
    if (batchId) void queryClient.invalidateQueries({ queryKey: materialsKeys.batch(batchId) })
  }, [batchId, queryClient])

  const createLocal = useMutation({ mutationFn: createLocalBatch, onSuccess: invalidate })
  const analyze = useMutation({ mutationFn: () => startAnalysis(batchId), onSuccess: invalidate })
  const reanalyze = useMutation({
    mutationFn: (storeIds: string[]) => startReanalysis(batchId, storeIds),
    onSuccess: invalidate,
  })
  const retry = useMutation({ mutationFn: () => retryFailed(batchId), onSuccess: invalidate })
  const accept = useMutation({ mutationFn: () => acceptRecommendations(batchId), onSuccess: invalidate })
  const remove = useMutation({ mutationFn: () => deleteBatch(batchId), onSuccess: invalidate })
  const restore = useMutation({ mutationFn: () => restoreBatch(batchId), onSuccess: invalidate })
  const purge = useMutation({ mutationFn: () => purgeBatch(batchId), onSuccess: invalidate })
  const clearSelection = useMutation({ mutationFn: () => clearActiveSelection(batchId), onSuccess: invalidate })
  const setStage = useMutation({
    mutationFn: (stage: string) => setBatchStage(batchId, stage),
    onSuccess: invalidate,
  })
  const update = useMutation({
    mutationFn: (payload: { name: string; description: string }) => updateBatch(batchId, payload),
    onSuccess: invalidate,
  })
  const publish = useMutation({
    mutationFn: (payload: { name: string; version: string; description: string }) => publishBatch(batchId, payload),
    onSuccess: invalidate,
  })
  const activate = useMutation({
    mutationFn: (version: string) => activateVersion(batchId, version),
    onSuccess: invalidate,
  })
  const pause = useMutation({ mutationFn: (jobId: string) => pauseJob(jobId), onSuccess: invalidate })
  const resume = useMutation({ mutationFn: (jobId: string) => resumeJob(jobId), onSuccess: invalidate })
  const cancel = useMutation({ mutationFn: (jobId: string) => cancelJob(jobId), onSuccess: invalidate })

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
  const queryClient = useQueryClient()
  const [progress, setProgress] = useState<BatchUploadProgress | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

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
        const started = await beginUpload(meta)
        batchId = started.batch_id
        let bytes = 0
        for (let index = 0; index < files.length; index += 1) {
          if (controller.signal.aborted) throw new Error('上传已取消')
          const file = files[index]!
          await uploadBatchFile(batchId, relativePathOf(file), file)
          bytes += file.size
          setProgress({
            done: index + 1,
            total: files.length,
            bytes,
            totalBytes,
            current: files[index + 1]?.name ?? '',
          })
        }
        const batch = await finishUpload(batchId)
        await queryClient.invalidateQueries({ queryKey: materialsKeys.batches() })
        await queryClient.invalidateQueries({ queryKey: materialsKeys.batchOptions() })
        setProgress(null)
        return batch
      } catch (cause) {
        if (batchId) await abortUpload(batchId).catch(() => undefined)
        const message = cause instanceof Error ? cause.message : '上传失败'
        setError(message)
        throw cause
      } finally {
        setRunning(false)
        abortRef.current = null
      }
    },
    [queryClient],
  )

  const cancel = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  return { run, cancel, progress, error, running }
}

/** 单店写操作：决策、单店重分析、补图。 */
export function useStoreActions(batchId: string, storeId: string) {
  const queryClient = useQueryClient()
  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: materialsKeys.store(batchId, storeId) })
    void queryClient.invalidateQueries({ queryKey: materialsKeys.batch(batchId) })
    void queryClient.invalidateQueries({ queryKey: materialsKeys.batches() })
    void queryClient.invalidateQueries({ queryKey: ['materials', 'rework'] })
  }, [batchId, queryClient, storeId])

  const decide = useMutation({
    mutationFn: (payload: DecisionPayload) => decideStore(batchId, storeId, payload),
    onSuccess: (store) => {
      queryClient.setQueryData(materialsKeys.store(batchId, storeId), store)
      invalidate()
    },
  })
  const reanalyze = useMutation({
    mutationFn: () => reanalyzeStore(batchId, storeId),
    onSuccess: (store) => {
      queryClient.setQueryData(materialsKeys.store(batchId, storeId), store)
      invalidate()
    },
  })

  const supplement = useCallback(
    async (files: File[], mode: UploadMode, onProgress?: (done: number, total: number) => void) => {
      const totalBytes = files.reduce((sum, file) => sum + file.size, 0)
      if (!files.length) throw new Error('请选择要补充的图片')
      if (files.length > MAX_STORE_UPLOAD_FILES) throw new Error(`单店最多补充 ${MAX_STORE_UPLOAD_FILES} 张图片`)
      if (totalBytes > MAX_STORE_UPLOAD_BYTES) throw new Error('单店补充图片总大小超过上限')
      const session = await beginStoreUpload(batchId, storeId)
      const uploaded: UploadedStoreFile[] = []
      try {
        for (let index = 0; index < files.length; index += 1) {
          const file = files[index]!
          uploaded.push(await uploadStoreFile(batchId, storeId, session.upload_id, file.name, file))
          onProgress?.(index + 1, files.length)
        }
        const result = await commitStoreUpload(batchId, storeId, session.upload_id, { mode, files: uploaded })
        queryClient.setQueryData(materialsKeys.store(batchId, storeId), result.store)
        invalidate()
        return result
      } catch (cause) {
        await abortStoreUpload(batchId, storeId, session.upload_id).catch(() => undefined)
        throw cause
      }
    },
    [batchId, invalidate, queryClient, storeId],
  )

  return { decide, reanalyze, supplement }
}

export function useReworkActions() {
  const queryClient = useQueryClient()
  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['materials', 'rework'] })
    void queryClient.invalidateQueries({ queryKey: materialsKeys.batches() })
  }, [queryClient])

  const returnStore = useMutation({
    mutationFn: (payload: { batchId: string; storeId: string }) =>
      returnReworkStore(payload.batchId, payload.storeId),
    onSuccess: invalidate,
  })
  const publish = useMutation({
    mutationFn: (storeKeys: string[]) => publishRework(storeKeys),
    onSuccess: invalidate,
  })
  return { returnStore, publish }
}
