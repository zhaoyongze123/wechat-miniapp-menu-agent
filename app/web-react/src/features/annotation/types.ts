/** 人工标注相关的服务端数据结构：字段与 menu_capture/annotation.py 的 state() 保持一致。 */

export interface AnnotationBox {
  x: number
  y: number
  width: number
  height: number
}

export interface AnnotationFrame {
  name: string
  url: string
  image_width: number
  image_height: number
  window_width: number
  window_height: number
  scale_x: number
  scale_y: number
  captured_at?: string
}

export interface AnnotationSuggestion {
  category?: string | null
  page_state?: string | null
  confidence?: number | null
  matched_text?: string | null
  note?: string | null
  error?: string | null
  target?: string | null
  box_px?: AnnotationBox | null
}

export interface AnnotationCandidate {
  id?: string
  text?: string
  kind?: string
  position?: string
}

export interface AnnotationDecision {
  at?: string
  source?: string | null
  source_detail?: string | null
  chain?: string[] | null
  hit_stage?: string | null
  action?: string | null
  target_label?: string | null
  reason?: string | null
  x?: number | null
  y?: number | null
  box_px?: AnnotationBox | null
  method?: string | null
  resolution_reason?: string | null
  candidates?: AnnotationCandidate[] | null
  error?: string | null
  image_size?: { width?: number; height?: number } | null
}

export interface AnnotationFile {
  role?: string
  name: string
  url: string
  width?: number
  height?: number
}

export interface AnnotationEffect {
  changed?: boolean | null
  diff_score?: number | null
  roi_localized?: boolean | null
  roi_ratio?: number | null
}

export interface AnnotationStep {
  index: number
  at?: string
  task_id?: string
  source_name?: string
  category?: string
  layout?: string
  page_state?: string
  target?: string
  hard_negative?: boolean
  note?: string
  box_px?: AnnotationBox | null
  center_points?: { x: number; y: number } | null
  image_size?: { width?: number; height?: number } | null
  before?: AnnotationFile | null
  after?: AnnotationFile | null
  effect?: AnnotationEffect | null
  suggestion?: AnnotationDecision | null
}

export interface AnnotationTaskRef {
  id: string
  source_name?: string
  status?: string
  stage?: string
}

export interface AnnotationCompleted {
  task_id: string
  source_name?: string
  steps?: number
}

export interface AnnotationSkipped {
  task_id: string
  source_name?: string
  reason?: string
}

export interface AnnotationRejected {
  task_id: string
  source_name?: string
  reason?: string
  detail?: string
}

export interface AnnotationState {
  session_id?: string | null
  active: boolean
  paused?: boolean
  status: string
  message?: string
  task_ids?: string[]
  index: number
  total: number
  task?: AnnotationTaskRef | null
  frame?: AnnotationFrame | null
  suggestion?: AnnotationSuggestion | null
  decision?: AnnotationDecision | null
  categories?: string[]
  steps?: AnnotationStep[]
  current_steps?: AnnotationStep[]
  last_click?: AnnotationStep | null
  completed?: AnnotationCompleted[]
  archived?: AnnotationCompleted[]
  skipped?: AnnotationSkipped[]
  rejected?: AnnotationRejected[]
  started_at?: string | null
  finished_at?: string | null
  samples_root?: string
}

export interface AnnotationHistory {
  task?: AnnotationTaskRef
  session_id?: string | null
  steps: AnnotationStep[]
  started_at?: string | null
  finished_at?: string | null
}

/** 仅取标注排队需要的字段，来源为 /api/status 的任务列表。 */
export interface AnnotationTask {
  id: string
  source_name?: string
  status?: string
  stage?: string
  reject_reason?: string
}

export interface AnnotationClickPayload {
  box: AnnotationBox
  category: string
  target?: string | undefined
  hard_negative?: boolean | undefined
  note?: string | undefined
}
