import type { Box, MaterialStoreDetail, StoreImage } from './types'

export type ResizeHandle = 'nw' | 'ne' | 'sw' | 'se'

export interface ImageTransformParams {
  clientX: number
  clientY: number
  stageCenterX: number
  stageCenterY: number
  baseWidth: number
  baseHeight: number
  zoom: number
  panX: number
  panY: number
  rotationDeg: number
}

export const MIN_BOX_SIZE = 0.02

export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
}

/**
 * 将后端返回的候选框统一归一化到 [0, 1] 显示坐标。
 * 后端候选框通常为原图像素坐标，人工框选或部分场景可能已是 0..1 归一化坐标。
 */
export function normalizeCandidateBox(box: Box | null | undefined, naturalWidth: number, naturalHeight: number): Box | null {
  if (!box) return null
  if (!Number.isFinite(box.x) || !Number.isFinite(box.y) || !Number.isFinite(box.width) || !Number.isFinite(box.height)) {
    return null
  }
  if (box.width <= 0 || box.height <= 0) return null

  const alreadyNormalized = box.x <= 1 && box.y <= 1 && box.width <= 1 && box.height <= 1
  let x = box.x
  let y = box.y
  let width = box.width
  let height = box.height

  if (!alreadyNormalized) {
    if (naturalWidth <= 0 || naturalHeight <= 0) return null
    x = box.x / naturalWidth
    y = box.y / naturalHeight
    width = box.width / naturalWidth
    height = box.height / naturalHeight
  }

  const clampedX = clamp(x, 0, 1)
  const clampedY = clamp(y, 0, 1)
  const clampedW = clamp(width, 0, 1 - clampedX)
  const clampedH = clamp(height, 0, 1 - clampedY)
  if (clampedW <= 0 || clampedH <= 0) return null

  return {
    x: clampedX,
    y: clampedY,
    width: clampedW,
    height: clampedH,
  }
}

/**
 * 将视口指针坐标通过逆变换（平移 -> 缩放 -> 旋转）精确映射为原图 [0, 1] 归一化坐标。
 * 对应画布 CSS transform: translate(panX, panY) scale(zoom) rotate(rotationDeg)。
 */
export function clientPointToImageUV({
  clientX,
  clientY,
  stageCenterX,
  stageCenterY,
  baseWidth,
  baseHeight,
  zoom,
  panX,
  panY,
  rotationDeg,
}: ImageTransformParams): { u: number; v: number } {
  if (baseWidth <= 0 || baseHeight <= 0 || zoom <= 0) {
    return { u: 0, v: 0 }
  }
  const dx = (clientX - stageCenterX - panX) / zoom
  const dy = (clientY - stageCenterY - panY) / zoom
  const rad = (-rotationDeg * Math.PI) / 180
  const cos = Math.cos(rad)
  const sin = Math.sin(rad)
  const ix = dx * cos - dy * sin
  const iy = dx * sin + dy * cos
  return {
    u: clamp(ix / baseWidth + 0.5, 0, 1),
    v: clamp(iy / baseHeight + 0.5, 0, 1),
  }
}

export function boxFromPoints(start: { u: number; v: number }, end: { u: number; v: number }): Box {
  const x1 = clamp(Math.min(start.u, end.u), 0, 1)
  const y1 = clamp(Math.min(start.v, end.v), 0, 1)
  const x2 = clamp(Math.max(start.u, end.u), 0, 1)
  const y2 = clamp(Math.max(start.v, end.v), 0, 1)
  return {
    x: x1,
    y: y1,
    width: x2 - x1,
    height: y2 - y1,
  }
}

export function moveBox(box: Box, du: number, dv: number): Box {
  const x = clamp(box.x + du, 0, Math.max(0, 1 - box.width))
  const y = clamp(box.y + dv, 0, Math.max(0, 1 - box.height))
  return { x, y, width: box.width, height: box.height }
}

export function resizeBox(box: Box, handle: ResizeHandle, u: number, v: number, minSize = MIN_BOX_SIZE): Box {
  const right = box.x + box.width
  const bottom = box.y + box.height
  let left = box.x
  let top = box.y
  let nextRight = right
  let nextBottom = bottom

  if (handle === 'nw' || handle === 'sw') {
    left = clamp(u, 0, right - minSize)
  } else {
    nextRight = clamp(u, box.x + minSize, 1)
  }

  if (handle === 'nw' || handle === 'ne') {
    top = clamp(v, 0, bottom - minSize)
  } else {
    nextBottom = clamp(v, box.y + minSize, 1)
  }

  return {
    x: left,
    y: top,
    width: Math.max(minSize, nextRight - left),
    height: Math.max(minSize, nextBottom - top),
  }
}

export function isUsableBox(box: Box | null | undefined, minSize = MIN_BOX_SIZE): box is Box {
  if (!box) return false
  return box.width >= minSize && box.height >= minSize
}

/** 计算图片在当前容器与旋转角度下的自适应基准尺寸（保证 90°/270° 旋转也不溢出）。 */
export function fitImageDimensions(
  containerWidth: number,
  containerHeight: number,
  naturalWidth: number,
  naturalHeight: number,
  rotationDeg: number,
): { width: number; height: number } {
  if (containerWidth <= 0 || containerHeight <= 0 || naturalWidth <= 0 || naturalHeight <= 0) {
    return { width: 0, height: 0 }
  }
  const normalizedRot = ((rotationDeg % 360) + 360) % 360
  const swapped = normalizedRot === 90 || normalizedRot === 270
  const effectiveW = swapped ? naturalHeight : naturalWidth
  const effectiveH = swapped ? naturalWidth : naturalHeight
  const scale = Math.min(containerWidth / effectiveW, containerHeight / effectiveH)
  return {
    width: Math.max(1, naturalWidth * scale),
    height: Math.max(1, naturalHeight * scale),
  }
}

/** 将人工框选区域实时裁剪绘制到预览 canvas（按当前视图旋转角度呈现）。 */
export function drawCroppedPreview(
  canvas: HTMLCanvasElement,
  img: HTMLImageElement,
  box: Box,
  rotationDeg: number,
): boolean {
  if (!img.complete || img.naturalWidth <= 0 || img.naturalHeight <= 0) return false
  const sx = clamp(Math.round(box.x * img.naturalWidth), 0, img.naturalWidth - 1)
  const sy = clamp(Math.round(box.y * img.naturalHeight), 0, img.naturalHeight - 1)
  const sw = clamp(Math.round(box.width * img.naturalWidth), 1, img.naturalWidth - sx)
  const sh = clamp(Math.round(box.height * img.naturalHeight), 1, img.naturalHeight - sy)

  const ctx = canvas.getContext('2d')
  if (!ctx) return false

  const targetSize = 160
  canvas.width = targetSize
  canvas.height = targetSize
  ctx.clearRect(0, 0, targetSize, targetSize)

  const normalizedRot = ((rotationDeg % 360) + 360) % 360
  const swapped = normalizedRot === 90 || normalizedRot === 270
  const drawW = swapped ? sh : sw
  const drawH = swapped ? sw : sh
  const scale = Math.min((targetSize - 8) / drawW, (targetSize - 8) / drawH)
  const rw = sw * scale
  const rh = sh * scale

  ctx.save()
  ctx.translate(targetSize / 2, targetSize / 2)
  ctx.rotate((normalizedRot * Math.PI) / 180)
  ctx.drawImage(img, sx, sy, sw, sh, -rw / 2, -rh / 2, rw, rh)
  ctx.restore()
  return true
}

const DETECTOR_ERROR_PATTERN =
  /检测失败|识别服务|环境异常|服务不可用|未安装|无法启动|超时|timeout|traceback|exception|error:|detector|worker.*失败|vision.*失败/i

/** 区分「检测系统异常」与「正常未检出二维码」，避免人工误判。 */
export function getDetectorErrorMessage(detail: MaterialStoreDetail | null | undefined, image: StoreImage | null | undefined): string | null {
  if (image?.error && image.error.trim()) {
    return image.error.trim()
  }
  if (!detail) return null
  if (detail.analysis_status === 'failed') {
    return detail.reason?.trim() || '门店分析执行失败，请检查检测环境后重试。'
  }
  if (detail.reason && DETECTOR_ERROR_PATTERN.test(detail.reason)) {
    return detail.reason.trim()
  }
  return null
}

/** 查找门店默认应展示的图片与推荐候选（仅作默认预选，绝不自动提交）。 */
export function findInitialImageAndRecommendation(detail: MaterialStoreDetail): {
  imageIndex: number
  recommendedCandidateIndex: number | null
} {
  if (detail.selected && detail.selected.image_index >= 0 && detail.selected.image_index < detail.images.length) {
    return {
      imageIndex: detail.selected.image_index,
      recommendedCandidateIndex: detail.selected.candidate_index,
    }
  }
  for (let i = 0; i < detail.images.length; i += 1) {
    const rec = detail.images[i]?.candidates.find((c) => c.recommendation && c.quality !== 'rejected')
    if (rec) return { imageIndex: i, recommendedCandidateIndex: rec.candidate_index }
  }
  const firstWithCandidates = detail.images.findIndex((img) => img.candidates.some((c) => c.quality !== 'rejected'))
  if (firstWithCandidates >= 0) {
    return { imageIndex: firstWithCandidates, recommendedCandidateIndex: null }
  }
  const anyWithCandidates = detail.images.findIndex((img) => img.candidates.length > 0)
  return { imageIndex: anyWithCandidates >= 0 ? anyWithCandidates : 0, recommendedCandidateIndex: null }
}
