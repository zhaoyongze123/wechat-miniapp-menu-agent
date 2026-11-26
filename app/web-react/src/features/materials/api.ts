import { apiFetch, apiUpload } from '../../api/client'
import { projectApiPath } from '../projects/scope'
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

function scoped(path: string, projectId?: string): string {
  return projectId ? projectApiPath(path, projectId) : path
}

export function fetchBatches(signal?: AbortSignal, projectId?: string): Promise<BatchesResponse> {
  return apiFetch<BatchesResponse>(scoped(`${BASE}/batches`, projectId), { signal })
}

export function fetchBatchOptions(signal?: AbortSignal, projectId?: string): Promise<BatchOptionsResponse> {
  return apiFetch<BatchOptionsResponse>(scoped(`${BASE}/batches?view=options`, projectId), { signal })
}

export function fetchBatchManagement(params: BatchListParams, signal?: AbortSignal, projectId?: string): Promise<BatchManagementResponse> {
  const search = new URLSearchParams({ deleted: String(params.deleted) })
  return apiFetch<BatchManagementResponse>(scoped(`${BASE}/batch-management?${search.toString()}`, projectId), { signal })
}

export function fetchBatch(batchId: string, signal?: AbortSignal, projectId?: string): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(scoped(batchPath(batchId), projectId), { signal })
}

export function fetchStore(batchId: string, storeId: string, signal?: AbortSignal, projectId?: string): Promise<MaterialStoreDetail> {
  return apiFetch<MaterialStoreDetail>(scoped(storePath(batchId, storeId), projectId), { signal })
}

export function createLocalBatch(payload: { input_dir: string; name: string; description: string }, projectId?: string): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(scoped(`${BASE}/batches`, projectId), { method: 'POST', json: { ...payload, ...(projectId ? { project_id: projectId } : {}) } })
}

export function beginUpload(payload: { name: string; description: string }, projectId?: string): Promise<BeginUploadResponse> {
  return apiFetch<BeginUploadResponse>(scoped(`${BASE}/uploads`, projectId), { method: 'POST', json: { ...payload, ...(projectId ? { project_id: projectId } : {}) } })
}

export function uploadBatchFile(batchId: string, relativePath: string, file: Blob, projectId?: string, signal?: AbortSignal): Promise<UploadFileResponse> {
  return apiUpload<UploadFileResponse>(
    scoped(`${BASE}/uploads/${encodeURIComponent(batchId)}/file?path=${encodeURIComponent(relativePath)}`, projectId),
    file,
    signal,
  )
}

export function finishUpload(batchId: string, projectId?: string): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(scoped(`${BASE}/uploads/${encodeURIComponent(batchId)}/finish`, projectId), {
    method: 'POST',
    json: {},
  })
}

export function abortUpload(batchId: string, projectId?: string): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(scoped(`${BASE}/uploads/${encodeURIComponent(batchId)}/abort`, projectId), {
    method: 'POST',
    json: {},
  })
}

export function updateBatch(batchId: string, payload: { name: string; description: string }, projectId?: string): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(scoped(batchPath(batchId), projectId), { method: 'PATCH', json: payload })
}

export function deleteBatch(batchId: string, projectId?: string): Promise<{ ok: boolean; batch: MaterialBatchDetail }> {
  return apiFetch<{ ok: boolean; batch: MaterialBatchDetail }>(scoped(batchPath(batchId), projectId), { method: 'DELETE' })
}

export function restoreBatch(batchId: string, projectId?: string): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(scoped(`${batchPath(batchId)}/restore`, projectId), { method: 'POST', json: {} })
}

export function purgeBatch(batchId: string, projectId?: string): Promise<{ ok: boolean; batch_id: string; purged: boolean }> {
  return apiFetch<{ ok: boolean; batch_id: string; purged: boolean }>(scoped(`${batchPath(batchId)}/permanent`, projectId), {
    method: 'DELETE',
  })
}

export function clearActiveSelection(batchId: string, projectId?: string): Promise<{ ok: boolean; cleared: boolean; batch_id: string | null }> {
  return apiFetch<{ ok: boolean; cleared: boolean; batch_id: string | null }>(scoped(`${BASE}/selection/clear`, projectId), {
    method: 'POST',
    json: { batch_id: batchId, ...(projectId ? { project_id: projectId } : {}) },
  })
}

export function setBatchStage(batchId: string, stage: string, projectId?: string): Promise<MaterialBatchDetail> {
  return apiFetch<MaterialBatchDetail>(scoped(`${batchPath(batchId)}/stage`, projectId), { method: 'POST', json: { stage } })
}

export function startAnalysis(batchId: string, projectId?: string): Promise<StartAnalysisResponse> {
  return apiFetch<StartAnalysisResponse>(scoped(`${batchPath(batchId)}/analyze`, projectId), { method: 'POST', json: {} })
}

export function startReanalysis(batchId: string, storeIds: string[], projectId?: string): Promise<StartAnalysisResponse> {
  return apiFetch<StartAnalysisResponse>(scoped(`${batchPath(batchId)}/reanalyze`, projectId), {
    method: 'POST',
    json: { store_ids: storeIds },
  })
}

export function retryFailed(batchId: string, projectId?: string): Promise<StartAnalysisResponse> {
  return apiFetch<StartAnalysisResponse>(scoped(`${batchPath(batchId)}/retry-failed`, projectId), { method: 'POST', json: {} })
}

export function fetchJobs(batchId: string, signal?: AbortSignal, projectId?: string): Promise<JobsResponse> {
  const search = new URLSearchParams({ active: 'false', batch_id: batchId, limit: '20' })
  return apiFetch<JobsResponse>(scoped(`${BASE}/jobs?${search.toString()}`, projectId), { signal })
}

export function fetchJob(jobId: string, signal?: AbortSignal, projectId?: string): Promise<MaterialJob> {
  return apiFetch<MaterialJob>(scoped(`${BASE}/jobs/${encodeURIComponent(jobId)}`, projectId), { signal })
}

export function pauseJob(jobId: string, projectId?: string): Promise<MaterialJob> {
  return apiFetch<MaterialJob>(scoped(`${BASE}/jobs/${encodeURIComponent(jobId)}/pause`, projectId), { method: 'POST', json: {} })
}

export function resumeJob(jobId: string, projectId?: string): Promise<MaterialJob> {
  return apiFetch<MaterialJob>(scoped(`${BASE}/jobs/${encodeURIComponent(jobId)}/resume`, projectId), { method: 'POST', json: {} })
}

export function cancelJob(jobId: string, projectId?: string): Promise<MaterialJob> {
  return apiFetch<MaterialJob>(scoped(`${BASE}/jobs/${encodeURIComponent(jobId)}/cancel`, projectId), { method: 'POST', json: {} })
}

export function decideStore(batchId: string, storeId: string, payload: DecisionPayload, projectId?: string): Promise<MaterialStoreDetail> {
  return apiFetch<MaterialStoreDetail>(scoped(`${storePath(batchId, storeId)}/decision`, projectId), { method: 'POST', json: payload })
}

export function acceptRecommendations(batchId: string, projectId?: string): Promise<AcceptRecommendationsResponse> {
  return apiFetch<AcceptRecommendationsResponse>(scoped(`${batchPath(batchId)}/accept-recommendations`, projectId), {
    method: 'POST',
    json: {},
  })
}

export function publishBatch(
  batchId: string,
  payload: { name: string; version: string; description: string },
  projectId?: string,
): Promise<PublishResponse> {
  return apiFetch<PublishResponse>(scoped(`${batchPath(batchId)}/publish`, projectId), { method: 'POST', json: payload })
}

export function activateVersion(batchId: string, version: string, projectId?: string): Promise<{ snapshot_id: string }> {
  return apiFetch<{ snapshot_id: string }>(scoped(`${batchPath(batchId)}/activate`, projectId), { method: 'POST', json: { version } })
}

export function beginStoreUpload(batchId: string, storeId: string, projectId?: string): Promise<BeginStoreUploadResponse> {
  return apiFetch<BeginStoreUploadResponse>(scoped(`${storePath(batchId, storeId)}/uploads`, projectId), {
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
  projectId?: string,
): Promise<UploadedStoreFile> {
  const search = new URLSearchParams({ filename })
  return apiUpload<UploadedStoreFile>(
    scoped(`${storePath(batchId, storeId)}/uploads/${encodeURIComponent(uploadId)}/file?${search.toString()}`, projectId),
    file,
  )
}

export function commitStoreUpload(
  batchId: string,
  storeId: string,
  uploadId: string,
  payload: { mode: UploadMode; files: UploadedStoreFile[] },
  projectId?: string,
): Promise<CommitStoreUploadResponse> {
  return apiFetch<CommitStoreUploadResponse>(
    scoped(`${storePath(batchId, storeId)}/uploads/${encodeURIComponent(uploadId)}/commit`, projectId),
    { method: 'POST', json: payload },
  )
}

export function abortStoreUpload(batchId: string, storeId: string, uploadId: string, projectId?: string): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(scoped(`${storePath(batchId, storeId)}/uploads/${encodeURIComponent(uploadId)}`, projectId), {
    method: 'DELETE',
  })
}

export function reanalyzeStore(batchId: string, storeId: string, projectId?: string): Promise<MaterialStoreDetail> {
  return apiFetch<MaterialStoreDetail>(scoped(`${storePath(batchId, storeId)}/reanalyze`, projectId), {
    method: 'POST',
    json: {},
  })
}

export function fetchRework(query = '', signal?: AbortSignal, projectId?: string): Promise<ReworkResponse> {
  const search = new URLSearchParams({ batch_ids: 'all' })
  if (query.trim()) search.set('q', query.trim())
  return apiFetch<ReworkResponse>(scoped(`${BASE}/rework?${search.toString()}`, projectId), { signal })
}

export function returnReworkStore(batchId: string, storeId: string, projectId?: string): Promise<MaterialStoreDetail> {
  return apiFetch<MaterialStoreDetail>(scoped(`${BASE}/rework/return`, projectId), {
    method: 'POST',
    json: { batch_id: batchId, store_id: storeId, ...(projectId ? { project_id: projectId } : {}) },
  })
}

export function publishRework(storeKeys: string[], description = '跨批次返工发布', projectId?: string): Promise<ReworkPublishResponse> {
  return apiFetch<ReworkPublishResponse>(scoped(`${BASE}/rework/publish`, projectId), {
    method: 'POST',
    json: { store_ids: storeKeys, description, ...(projectId ? { project_id: projectId } : {}) },
  })
}

export function storeImageUrl(batchId: string, storeId: string, imageIndex: number, projectId?: string): string {
  return scoped(`${storePath(batchId, storeId)}/images/${encodeURIComponent(imageIndex)}`, projectId)
}

export function storeCandidateUrl(
  batchId: string,
  storeId: string,
  imageIndex: number,
  candidateIndex: number,
  projectId?: string,
): string {
  return scoped(`${storeImageUrl(batchId, storeId, imageIndex)}/candidates/${encodeURIComponent(candidateIndex)}`, projectId)
}

export function storeSelectedUrl(batchId: string, storeId: string, projectId?: string): string {
  return scoped(`${storePath(batchId, storeId)}/selected`, projectId)
}
