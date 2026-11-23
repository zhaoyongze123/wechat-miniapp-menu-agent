import { apiFetch, apiUpload } from '../../api/client'
import type {
  AcceptRecommendationsResponse,
  BatchListParams,
  BatchManagementResponse,
  BatchOptionsResponse,
  BatchesResponse,
  BeginStoreUploadResponse,
  BeginUploadResponse,
  CommitStoreUploadResponse,
  DecisionPayload,
  JobsResponse,
  MaterialBatchDetail,
  MaterialJob,
  MaterialStoreDetail,
  PublishResponse,
  ReworkPublishResponse,
  ReworkResponse,
  StartAnalysisResponse,
  UploadedStoreFile,
  UploadFileResponse,
  UploadMode,
} from './types'

/**
 * 素材清洗写入适配层。
 * 路径、参数名和错误语义都与 Python 路由保持一致；前端不复制决策与发布规则。
 */
const BASE = '/api/materials'

function batchPath(batchId: string): string {
  return `${BASE}/batches/${encodeURIComponent(batchId)}`
}

function storePath(batchId: string, storeId: string): string {
  return `${batchPath(batchId)}/stores/${encodeURIComponent(storeId)}`
}

export function fetchBatches(signal?: AbortSignal): Promise<BatchesResponse> {
  return apiFetch<BatchesResponse>(`${BASE}/batches`, { signal })
}

export function fetchBatchOptions(signal?: AbortSignal): Promise<BatchOptionsResponse> {
  return apiFetch<BatchOptionsResponse>(`${BASE}/batches?view=options`, { signal })
}

export function fetchBatchManagement(params: BatchListParams, signal?: AbortSignal): Promise<BatchManagementResponse> {
  const search = new URLSearchParams({ deleted: String(params.deleted) })
  return apiFetch<BatchManagementResponse>(`${BASE}/batch-management?${search.toString()}`, { signal })
}

export function fetchBatch(batchId: string, signal?: AbortSignal): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(batchPath(batchId), { signal })
}

export function fetchStore(batchId: string, storeId: string, signal?: AbortSignal): Promise<MaterialStoreDetail> {
  return apiFetch<MaterialStoreDetail>(storePath(batchId, storeId), { signal })
}

export function createLocalBatch(payload: { input_dir: string; name: string; description: string }): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(`${BASE}/batches`, { method: 'POST', json: payload })
}

export function beginUpload(payload: { name: string; description: string }): Promise<BeginUploadResponse> {
  return apiFetch<BeginUploadResponse>(`${BASE}/uploads`, { method: 'POST', json: payload })
}

export function uploadBatchFile(batchId: string, relativePath: string, file: Blob): Promise<UploadFileResponse> {
  return apiUpload<UploadFileResponse>(
    `${BASE}/uploads/${encodeURIComponent(batchId)}/file?path=${encodeURIComponent(relativePath)}`,
    file,
  )
}

export function finishUpload(batchId: string): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(`${BASE}/uploads/${encodeURIComponent(batchId)}/finish`, {
    method: 'POST',
    json: {},
  })
}

export function abortUpload(batchId: string): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(`${BASE}/uploads/${encodeURIComponent(batchId)}/abort`, {
    method: 'POST',
    json: {},
  })
}

export function updateBatch(batchId: string, payload: { name: string; description: string }): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(batchPath(batchId), { method: 'PATCH', json: payload })
}

export function deleteBatch(batchId: string): Promise<{ ok: boolean; batch: MaterialBatchDetail }> {
  return apiFetch<{ ok: boolean; batch: MaterialBatchDetail }>(batchPath(batchId), { method: 'DELETE' })
}

export function restoreBatch(batchId: string): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(`${batchPath(batchId)}/restore`, { method: 'POST', json: {} })
}

export function purgeBatch(batchId: string): Promise<{ ok: boolean; batch_id: string; purged: boolean }> {
  return apiFetch<{ ok: boolean; batch_id: string; purged: boolean }>(`${batchPath(batchId)}/permanent`, {
    method: 'DELETE',
  })
}

export function clearActiveSelection(batchId: string): Promise<{ ok: boolean; cleared: boolean; batch_id: string | null }> {
  return apiFetch<{ ok: boolean; cleared: boolean; batch_id: string | null }>(`${BASE}/selection/clear`, {
    method: 'POST',
    json: { batch_id: batchId },
  })
}

export function setBatchStage(batchId: string, stage: string): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(`${batchPath(batchId)}/stage`, { method: 'POST', json: { stage } })
}

export function startAnalysis(batchId: string): Promise<StartAnalysisResponse> {
  return apiFetch<StartAnalysisResponse>(`${batchPath(batchId)}/analyze`, { method: 'POST', json: {} })
}

export function startReanalysis(batchId: string, storeIds: string[]): Promise<StartAnalysisResponse> {
  return apiFetch<StartAnalysisResponse>(`${batchPath(batchId)}/reanalyze`, {
    method: 'POST',
    json: { store_ids: storeIds },
  })
}

export function retryFailed(batchId: string): Promise<StartAnalysisResponse> {
  return apiFetch<StartAnalysisResponse>(`${batchPath(batchId)}/retry-failed`, { method: 'POST', json: {} })
}

export function fetchJobs(batchId: string, signal?: AbortSignal): Promise<JobsResponse> {
  const search = new URLSearchParams({ active: 'false', batch_id: batchId, limit: '20' })
  return apiFetch<JobsResponse>(`${BASE}/jobs?${search.toString()}`, { signal })
}

export function fetchJob(jobId: string, signal?: AbortSignal): Promise<MaterialJob> {
  return apiFetch<MaterialJob>(`${BASE}/jobs/${encodeURIComponent(jobId)}`, { signal })
}

export function pauseJob(jobId: string): Promise<MaterialJob> {
  return apiFetch<MaterialJob>(`${BASE}/jobs/${encodeURIComponent(jobId)}/pause`, { method: 'POST', json: {} })
}

export function resumeJob(jobId: string): Promise<MaterialJob> {
  return apiFetch<MaterialJob>(`${BASE}/jobs/${encodeURIComponent(jobId)}/resume`, { method: 'POST', json: {} })
}

export function cancelJob(jobId: string): Promise<MaterialJob> {
  return apiFetch<MaterialJob>(`${BASE}/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST', json: {} })
}

export function decideStore(batchId: string, storeId: string, payload: DecisionPayload): Promise<MaterialStoreDetail> {
  return apiFetch<MaterialStoreDetail>(`${storePath(batchId, storeId)}/decision`, { method: 'POST', json: payload })
}

export function acceptRecommendations(batchId: string): Promise<AcceptRecommendationsResponse> {
  return apiFetch<AcceptRecommendationsResponse>(`${batchPath(batchId)}/accept-recommendations`, {
    method: 'POST',
    json: {},
  })
}

export function publishBatch(
  batchId: string,
  payload: { name: string; version: string; description: string },
): Promise<PublishResponse> {
  return apiFetch<PublishResponse>(`${batchPath(batchId)}/publish`, { method: 'POST', json: payload })
}

export function activateVersion(batchId: string, version: string): Promise<{ snapshot_id: string }> {
  return apiFetch<{ snapshot_id: string }>(`${batchPath(batchId)}/activate`, { method: 'POST', json: { version } })
}

export function beginStoreUpload(batchId: string, storeId: string): Promise<BeginStoreUploadResponse> {
  return apiFetch<BeginStoreUploadResponse>(`${storePath(batchId, storeId)}/uploads`, {
    method: 'POST',
    json: {},
  })
}

export function uploadStoreFile(
  batchId: string,
  storeId: string,
  uploadId: string,
  filename: string,
  file: Blob,
): Promise<UploadedStoreFile> {
  const search = new URLSearchParams({ filename })
  return apiUpload<UploadedStoreFile>(
    `${storePath(batchId, storeId)}/uploads/${encodeURIComponent(uploadId)}/file?${search.toString()}`,
    file,
  )
}

export function commitStoreUpload(
  batchId: string,
  storeId: string,
  uploadId: string,
  payload: { mode: UploadMode; files: UploadedStoreFile[] },
): Promise<CommitStoreUploadResponse> {
  return apiFetch<CommitStoreUploadResponse>(
    `${storePath(batchId, storeId)}/uploads/${encodeURIComponent(uploadId)}/commit`,
    { method: 'POST', json: payload },
  )
}

export function abortStoreUpload(batchId: string, storeId: string, uploadId: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(`${storePath(batchId, storeId)}/uploads/${encodeURIComponent(uploadId)}`, {
    method: 'DELETE',
  })
}

export function reanalyzeStore(batchId: string, storeId: string): Promise<MaterialStoreDetail> {
  return apiFetch<MaterialStoreDetail>(`${storePath(batchId, storeId)}/reanalyze`, {
    method: 'POST',
    json: {},
  })
}

export function fetchRework(signal?: AbortSignal): Promise<ReworkResponse> {
  return apiFetch<ReworkResponse>(`${BASE}/rework?batch_ids=all`, { signal })
}

export function returnReworkStore(batchId: string, storeId: string): Promise<MaterialStoreDetail> {
  return apiFetch<MaterialStoreDetail>(`${BASE}/rework/return`, {
    method: 'POST',
    json: { batch_id: batchId, store_id: storeId },
  })
}

export function publishRework(storeKeys: string[], description = '跨批次返工发布'): Promise<ReworkPublishResponse> {
  return apiFetch<ReworkPublishResponse>(`${BASE}/rework/publish`, {
    method: 'POST',
    json: { store_ids: storeKeys, description },
  })
}

export function storeImageUrl(batchId: string, storeId: string, imageIndex: number): string {
  return `${storePath(batchId, storeId)}/images/${encodeURIComponent(imageIndex)}`
}

export function storeCandidateUrl(
  batchId: string,
  storeId: string,
  imageIndex: number,
  candidateIndex: number,
): string {
  return `${storeImageUrl(batchId, storeId, imageIndex)}/candidates/${encodeURIComponent(candidateIndex)}`
}

export function storeSelectedUrl(batchId: string, storeId: string): string {
  return `${storePath(batchId, storeId)}/selected`
}
