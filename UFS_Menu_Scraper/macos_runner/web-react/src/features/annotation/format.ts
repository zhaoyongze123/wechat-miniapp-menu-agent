import type { StatusTone } from '../../components/StatusBadge'
import type { AnnotationBox, AnnotationFrame, AnnotationStep, AnnotationTask } from './types'

/** 标注品类：与服务端 annotation.py 的 categories 保持一致，未知值回退原文。 */
export const CATEGORY_LABELS: Record<string, string> = {
  overview: '总览',
  category: '分类',
  dish: '菜品',
  modifier: '规格 / 选项',
}

const STATUS_TEXT: Record<string, string> = {
  idle: '未开始',
  starting: '正在准备',
  opening: '正在打开门店',
  ready: '等待标注',
  clicked: '已点击，等待效果',
  paused: '已暂停',
  closing: '正在结束',
  close_failed: '收尾异常',
  open_failed: '打开失败',
  error: '标注异常',
  finished: '已完成',
  stopped: '已停止',
  loading: '正在载入',
}

const STATUS_TONE: Record<string, StatusTone> = {
  idle: 'secondary',
  starting: 'info',
  opening: 'info',
  ready: 'info',
  clicked: 'warning',
  paused: 'warning',
  closing: 'info',
  close_failed: 'danger',
  open_failed: 'danger',
  error: 'danger',
  finished: 'success',
  stopped: 'secondary',
}

export function annotationStatusLabel(status?: string | null): string {
  if (!status) return '未知状态'
  return STATUS_TEXT[status] ?? status
}

export function annotationStatusTone(status?: string | null): StatusTone {
  if (!status) return 'secondary'
  return STATUS_TONE[status] ?? 'secondary'
}

export function categoryLabel(value?: string | null): string {
  if (!value) return '未分类'
  return CATEGORY_LABELS[value] ?? value
}

export function categoryOptions(categories?: string[] | null): { value: string; label: string }[] {
  const list = Array.isArray(categories) && categories.length ? categories : Object.keys(CATEGORY_LABELS)
  return list.map((value) => ({ value, label: categoryLabel(value) }))
}

/** 标注不合格原因：与服务端 materials.REJECTION_REASONS 完全相同，提交的是原因码。 */
export const REJECTION_REASONS: { value: string; label: string }[] = [
  { value: 'blur', label: '图片模糊 / 无法识别' },
  { value: 'invalid_qr', label: '二维码无效' },
  { value: 'store_mismatch', label: '二维码门店不一致' },
  { value: 'store_closed', label: '门店已关店 / 停业' },
  { value: 'miniapp_unavailable', label: '小程序到期或未绑定' },
  { value: 'scan_order_unsupported', label: '门店不支持扫码点餐' },
  { value: 'not_ordering', label: '非点餐码' },
  { value: 'qr_external_web', label: '二维码指向外部网页（非小程序）' },
  { value: 'menu_unavailable', label: '门店未配置菜单 / 无可用菜品' },
  { value: 'phone_only', label: '需在手机上打开（桌面无法采集）' },
  { value: 'authorization_unavailable', label: '需要验证码 / 扫码授权（自动化无法进入）' },
  { value: 'network_unavailable', label: '网络异常，重试后仍无法进入' },
  { value: 'duplicate', label: '重复素材' },
  { value: 'other', label: '其他原因' },
]

export function rejectionReasonLabel(code?: string | null): string {
  if (!code) return '未记录原因'
  return REJECTION_REASONS.find((item) => item.value === code)?.label ?? code
}

const TASK_STATUS_TEXT: Record<string, string> = {
  pending: '待处理',
  running: '进行中',
  done: '已完成',
  error: '异常',
  blocked: '阻塞',
  needs_review: '待复核',
  captured: '已采集',
  rejected: '不合格',
}

const TASK_STATUS_TONE: Record<string, StatusTone> = {
  pending: 'secondary',
  running: 'info',
  done: 'success',
  error: 'danger',
  blocked: 'warning',
  needs_review: 'warning',
  captured: 'info',
  rejected: 'danger',
}

export type TaskBucket = 'pending' | 'review' | 'captured' | 'annotated' | 'rejected' | 'other'

/** 排队分组沿用旧界面：不合格、已标注优先，其次按任务状态归类。 */
export function taskBucket(task: AnnotationTask): TaskBucket {
  if (task.reject_reason) return 'rejected'
  if (task.stage === 'annotation_complete') return 'annotated'
  if (task.status === 'pending') return 'pending'
  if (task.status === 'needs_review' || task.status === 'error' || task.status === 'blocked') return 'review'
  if (task.status === 'captured') return 'captured'
  return 'other'
}

export function taskClaimable(task: AnnotationTask): boolean {
  if (!task.id) return false
  const bucket = taskBucket(task)
  return bucket !== 'annotated' && bucket !== 'rejected'
}

export function taskStatusLabel(task: AnnotationTask): string {
  if (task.reject_reason) return '不合格'
  if (task.stage === 'annotation_complete') return '已标注'
  return TASK_STATUS_TEXT[task.status ?? ''] ?? task.status ?? '未知'
}

export function taskStatusTone(task: AnnotationTask): StatusTone {
  if (task.reject_reason) return 'danger'
  if (task.stage === 'annotation_complete') return 'success'
  return TASK_STATUS_TONE[task.status ?? ''] ?? 'secondary'
}

export interface TaskBuckets {
  all: number
  pending: number
  review: number
  captured: number
  annotated: number
  rejected: number
  other: number
}

export function countTaskBuckets(tasks: AnnotationTask[]): TaskBuckets {
  const result: TaskBuckets = { all: tasks.length, pending: 0, review: 0, captured: 0, annotated: 0, rejected: 0, other: 0 }
  for (const task of tasks) result[taskBucket(task)] += 1
  return result
}

function round(value: number, digits = 1): string {
  return Number(value.toFixed(digits)).toString()
}

/** 画布像素坐标与服务端要求的截图坐标一致，直接按图片原始尺寸提交。 */
export function formatMapping(box: AnnotationBox | null | undefined, frame: AnnotationFrame | null | undefined): string {
  if (!box) return '在截图上拖动框选目标'
  if (!frame) return `框选 ${Math.round(box.width)} × ${Math.round(box.height)} px`
  const centerX = box.x + box.width / 2
  const centerY = box.y + box.height / 2
  return (
    `中心 (${round(centerX)}, ${round(centerY)}) · 框 ${Math.round(box.width)} × ${Math.round(box.height)} px` +
    ` · 截图 ${frame.image_width} × ${frame.image_height}`
  )
}

/** 回看历史步骤时使用服务端记录的中心点，避免缩放差异导致重复换算。 */
export function formatStepMapping(step: AnnotationStep | null | undefined): string {
  if (!step) return ''
  const center = step.center_points
  const size = step.image_size
  const box = step.box_px
  const parts: string[] = []
  if (center) parts.push(`中心 (${round(center.x)}, ${round(center.y)})`)
  if (box) parts.push(`框 ${Math.round(box.width)} × ${Math.round(box.height)} px`)
  if (size?.width && size?.height) parts.push(`窗口 ${size.width} × ${size.height}`)
  return parts.join(' · ')
}

export function effectText(step: AnnotationStep | null | undefined): string {
  const changed = step?.effect?.changed
  if (changed === true) {
    const score = step?.effect?.diff_score
    return typeof score === 'number' ? `画面已变化（差异 ${round(score, 3)}）` : '画面已变化'
  }
  if (changed === false) return '画面基本未变'
  return '未判定变化'
}

export function effectTone(step: AnnotationStep | null | undefined): StatusTone {
  const changed = step?.effect?.changed
  if (changed === true) return 'success'
  if (changed === false) return 'warning'
  return 'secondary'
}

export function decisionSourceLabel(decision: { source?: string | null; source_detail?: string | null } | null | undefined): string {
  if (!decision) return '无判断'
  const source = decision.source_detail || decision.source || '未记录'
  return source
}

export function taskDisplayName(task: AnnotationTask | { id: string; source_name?: string } | null | undefined): string {
  if (!task) return '未选择门店'
  return task.source_name || task.id
}
