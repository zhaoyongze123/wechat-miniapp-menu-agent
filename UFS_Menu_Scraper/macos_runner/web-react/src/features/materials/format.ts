import type { StatusTone } from '../../components/StatusBadge'
import type { StoreDecision } from './types'

export interface LabelMeta {
  label: string
  tone: StatusTone
}

/**
 * 决策口径与后端一致：pending 待审核、confirmed 已确认、skipped 不合格。
 * 后端在旧数据里可能返回 rejected，统一归一化为 skipped。
 */
export function normalizeDecision(value: string | undefined): StoreDecision {
  if (value === 'confirmed' || value === 'skipped') return value
  if (value === 'rejected') return 'skipped'
  return 'pending'
}

export const DECISION_META: Record<StoreDecision, LabelMeta> = {
  pending: { label: '待审核', tone: 'warning' },
  confirmed: { label: '已确认', tone: 'success' },
  skipped: { label: '不合格', tone: 'danger' },
}

export function decisionMeta(value: string | undefined): LabelMeta {
  return DECISION_META[normalizeDecision(value)]
}

/** 批次状态与旧素材页保持一致，未知状态原样展示便于排查。 */
export const BATCH_STATUS: Record<string, LabelMeta> = {
  uploading: { label: '上传中', tone: 'info' },
  ready_to_analyze: { label: '待分析', tone: 'secondary' },
  queued: { label: '排队分析', tone: 'info' },
  analyzing: { label: '分析中', tone: 'info' },
  completed: { label: '分析完成', tone: 'success' },
  ready: { label: '待确认', tone: 'warning' },
  interrupted: { label: '已中断', tone: 'warning' },
  failed: { label: '分析失败', tone: 'danger' },
  error: { label: '分析失败', tone: 'danger' },
}

export function batchStatusMeta(value: string | undefined): LabelMeta {
  return (value && BATCH_STATUS[value]) || { label: value || '未知', tone: 'secondary' }
}

export const JOB_STATUS: Record<string, LabelMeta> = {
  queued: { label: '排队中', tone: 'info' },
  running: { label: '分析中', tone: 'info' },
  pause_requested: { label: '正在暂停', tone: 'warning' },
  paused: { label: '已暂停', tone: 'warning' },
  interrupted: { label: '已中断', tone: 'warning' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'secondary' },
}

export function jobStatusMeta(value: string | undefined): LabelMeta {
  return (value && JOB_STATUS[value]) || { label: value || '未知', tone: 'secondary' }
}

/** 任务未结束：包含暂停，避免重复发起分析。 */
export const OPEN_JOB_STATUSES = ['queued', 'running', 'pause_requested', 'paused', 'interrupted']
/** 任务占用分析进程：只有这些状态需要高频轮询。 */
export const ACTIVE_JOB_STATUSES = ['queued', 'running', 'pause_requested']

export function isJobOpen(status: string | undefined): boolean {
  return !!status && OPEN_JOB_STATUSES.includes(status)
}

export function isJobActive(status: string | undefined): boolean {
  return !!status && ACTIVE_JOB_STATUSES.includes(status)
}

export const PURPOSE_LABELS: Record<string, string> = {
  ordering: '点餐',
  not_ordering: '非点餐',
  enterprise_wechat: '企业微信',
  conflict: '证据冲突',
  other: '其他',
  unknown: '未知',
}

export const PURPOSE_SOURCE_LABELS: Record<string, string> = {
  payload_url: '二维码链接（URL）',
  payload_url_ocr: '餐饮域名 + 周边 OCR',
  payload_domain: '企业微信域名',
  local_ocr: '周边 OCR',
  vision_model: '视觉模型',
}

export const RECOMMENDATION_LEVEL_LABELS: Record<string, string> = {
  high: '高置信推荐',
  ocr: 'OCR 推荐',
  low: '低置信候选',
  none: '无推荐',
}

export const WECHAT_VALIDATION_LABELS: Record<string, string> = {
  verified: '小程序校验通过',
  passed: '小程序校验通过',
  failed: '小程序校验失败',
  unavailable: '小程序校验不可用',
  unverified: '未做小程序校验',
  skipped: '未做小程序校验',
  unknown: '小程序状态未知',
}

/** 单店分析状态：后端只使用 selected / needs_review，其余为兼容旧数据。 */
export const ANALYSIS_STATUS_LABELS: Record<string, string> = {
  selected: '已检测到候选',
  needs_review: '需人工确认',
  no_candidates: '未检测到二维码',
  failed: '分析失败',
  pending: '等待分析',
}

export function analysisStatusLabel(value: string | undefined): string {
  if (!value) return '未知'
  return ANALYSIS_STATUS_LABELS[value] ?? value
}

export const REJECTION_REASONS: { code: string; label: string }[] = [
  { code: 'blur', label: '图片模糊 / 无法识别' },
  { code: 'invalid_qr', label: '二维码无效' },
  { code: 'store_mismatch', label: '二维码门店不一致' },
  { code: 'store_closed', label: '门店已关店 / 停业' },
  { code: 'miniapp_unavailable', label: '小程序到期或未绑定' },
  { code: 'scan_order_unsupported', label: '门店不支持扫码点餐' },
  { code: 'not_ordering', label: '非点餐码' },
  { code: 'qr_external_web', label: '二维码指向外部网页（非小程序）' },
  { code: 'menu_unavailable', label: '门店未配置菜单 / 无可用菜品' },
  { code: 'phone_only', label: '需在手机上打开（桌面无法采集）' },
  { code: 'authorization_unavailable', label: '需要验证码/扫码授权（自动化无法进入）' },
  { code: 'network_unavailable', label: '网络异常，重试后仍无法进入' },
  { code: 'duplicate', label: '重复素材' },
  { code: 'other', label: '其他原因' },
]

/** 与后端 `_WORKFLOW_LABELS` 一致；空字符串表示跟随采集轮次自动判断。 */
export const WORKFLOW_STAGES: { value: string; label: string }[] = [
  { value: '', label: '自动判断' },
  { value: 'not_started', label: '未采集' },
  { value: 'first_run', label: '首轮已跑' },
  { value: 'rerun', label: '已重跑' },
  { value: 'approved', label: '人工审核通过（可建档）' },
]

export const MAX_BATCH_FILES = 10_000
export const MAX_FILE_BYTES = 50 * 1024 * 1024
export const MAX_BATCH_BYTES = 5 * 1024 * 1024 * 1024
export const MAX_STORE_UPLOAD_FILES = 100
export const MAX_STORE_UPLOAD_BYTES = 500 * 1024 * 1024
export const IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.webp']

export function formatBytes(value: number | undefined): string {
  const size = Number(value || 0)
  if (!Number.isFinite(size) || size <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1)
  const scaled = size / 1024 ** index
  return `${scaled >= 10 || index === 0 ? Math.round(scaled) : scaled.toFixed(1)} ${units[index]}`
}

export function formatDuration(seconds: number | undefined): string {
  if (seconds === undefined || seconds === null || !Number.isFinite(seconds)) return '—'
  const total = Math.max(0, Math.round(seconds))
  if (total < 60) return `${total} 秒`
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时 ${minutes % 60} 分`
}

/** 上传前本地预检，只做与后端一致的硬性限制，不做内容判断。 */
export function validateBatchFiles(files: File[]): string | null {
  if (!files.length) return '请选择图片文件或素材文件夹'
  if (files.length > MAX_BATCH_FILES) return `单批次最多 ${MAX_BATCH_FILES} 个文件，当前 ${files.length} 个`
  const total = files.reduce((sum, file) => sum + file.size, 0)
  if (total > MAX_BATCH_BYTES) return `批次总大小不能超过 ${formatBytes(MAX_BATCH_BYTES)}`
  const oversized = files.find((file) => file.size > MAX_FILE_BYTES)
  if (oversized) return `单文件不能超过 ${formatBytes(MAX_FILE_BYTES)}：${oversized.name}`
  const unsupported = files.find((file) => !IMAGE_EXTENSIONS.some((ext) => file.name.toLowerCase().endsWith(ext)))
  if (unsupported) return `仅支持 JPG / PNG / WEBP 图片：${unsupported.name}`
  return null
}

/** 上传相对路径优先取目录选择保留的 webkitRelativePath。 */
export function relativePathOf(file: File): string {
  const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath
  return relative && relative.trim() ? relative : file.name
}

/** 返工发布接口要求 `batch_id:store_id` 形式的门店标识。 */
export function storeKey(batchId: string, storeId: string): string {
  return `${batchId}:${storeId}`
}

export function parseStoreKey(key: string): { batchId: string; storeId: string } {
  const index = key.indexOf(':')
  return { batchId: key.slice(0, index), storeId: key.slice(index + 1) }
}
