import { useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import type { AnnotationBox } from './types'

interface AnnotationCanvasProps {
  url: string
  /** 画面切换标记：同一 URL 重新截图时也强制重绘。 */
  revision: string
  box: AnnotationBox | null
  suggestionBox?: AnnotationBox | null
  readOnly?: boolean
  onBoxChange?: (box: AnnotationBox | null) => void
}

interface Point {
  x: number
  y: number
}

// 标注叠加色属于画布业务语义：建议框=信息蓝、编辑框=警示琥珀、只读框=成功绿。
// 这些颜色需在任意门店截图上保持高对比，不能直接套用界面文本 Token。
const SUGGESTION_COLOR = '#2563eb'
const EDIT_BOX_COLOR = '#f59e0b'
const READONLY_BOX_COLOR = '#16a34a'
const EDIT_BOX_FILL = 'rgba(245, 158, 11, 0.18)'

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max)
}

/** 在截图原始像素坐标系内拖动框选；显示尺寸与坐标换算解耦。 */
export function AnnotationCanvas({ url, revision, box, suggestionBox, readOnly = false, onBoxChange }: AnnotationCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const imageRef = useRef<HTMLImageElement | null>(null)
  const paintRef = useRef<() => void>(() => {})
  const dragStart = useRef<Point | null>(null)
  const latestBox = useRef<AnnotationBox | null>(null)
  const [error, setError] = useState(false)
  const [loading, setLoading] = useState(true)

  const paint = () => {
    const canvas = canvasRef.current
    const image = imageRef.current
    if (!canvas || !image) return
    if (canvas.width !== image.naturalWidth || canvas.height !== image.naturalHeight) {
      canvas.width = image.naturalWidth
      canvas.height = image.naturalHeight
    }
    const context = canvas.getContext('2d')
    if (!context) return
    context.clearRect(0, 0, canvas.width, canvas.height)
    context.drawImage(image, 0, 0)
    if (suggestionBox) {
      context.save()
      context.setLineDash([10, 6])
      context.lineWidth = Math.max(2, canvas.width / 700)
      context.strokeStyle = SUGGESTION_COLOR
      context.strokeRect(suggestionBox.x, suggestionBox.y, suggestionBox.width, suggestionBox.height)
      context.restore()
    }
    if (box) {
      context.save()
      context.fillStyle = EDIT_BOX_FILL
      context.strokeStyle = readOnly ? READONLY_BOX_COLOR : EDIT_BOX_COLOR
      context.lineWidth = Math.max(3, canvas.width / 500)
      context.fillRect(box.x, box.y, box.width, box.height)
      context.strokeRect(box.x, box.y, box.width, box.height)
      context.restore()
    }
  }
  // 图片 onload 回调创建于首次渲染，通过 ref 取最新 props 计算绘制内容。
  paintRef.current = paint
  latestBox.current = box

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(false)
    imageRef.current = null
    const image = new Image()
    image.onload = () => {
      if (cancelled) return
      imageRef.current = image
      setLoading(false)
      paintRef.current()
    }
    image.onerror = () => {
      if (cancelled) return
      setError(true)
      setLoading(false)
    }
    image.src = `${url}${url.includes('?') ? '&' : '?'}rev=${encodeURIComponent(revision)}`
    return () => {
      cancelled = true
      image.onload = null
      image.onerror = null
    }
  }, [url, revision])

  useEffect(() => {
    paintRef.current()
  }, [box, suggestionBox, readOnly, loading])

  const pointOf = (event: ReactPointerEvent<HTMLCanvasElement>): Point | null => {
    const canvas = canvasRef.current
    const image = imageRef.current
    if (!canvas || !image) return null
    const rect = canvas.getBoundingClientRect()
    if (!rect.width || !rect.height) return null
    return {
      x: clamp(((event.clientX - rect.left) / rect.width) * canvas.width, 0, canvas.width),
      y: clamp(((event.clientY - rect.top) / rect.height) * canvas.height, 0, canvas.height),
    }
  }

  const onPointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (readOnly) return
    const point = pointOf(event)
    if (!point) return
    event.currentTarget.setPointerCapture(event.pointerId)
    dragStart.current = point
    onBoxChange?.({ x: point.x, y: point.y, width: 1, height: 1 })
  }

  const onPointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const start = dragStart.current
    if (!start || readOnly) return
    const point = pointOf(event)
    if (!point) return
    onBoxChange?.({
      x: Math.min(start.x, point.x),
      y: Math.min(start.y, point.y),
      width: Math.abs(point.x - start.x),
      height: Math.abs(point.y - start.y),
    })
  }

  const finish = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (!dragStart.current) return
    dragStart.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    const current = latestBox.current
    if (current && current.width < 2 && current.height < 2) onBoxChange?.(null)
  }

  return (
    <div className="relative min-h-72 overflow-hidden rounded-lg border border-subtle bg-surface-secondary">
      <canvas
        ref={canvasRef}
        className={`block w-full cursor-crosshair ${readOnly ? 'cursor-default' : ''}`}
        aria-label={readOnly ? '标注步骤截图，展示已记录的点击位置' : '拖拽框选需要点击的目标位置'}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={finish}
        onPointerCancel={finish}
        style={{ touchAction: 'none' }}
      />
      {loading ? <div className="absolute inset-0 grid place-items-center text-sm text-secondary">正在载入截图…</div> : null}
      {error ? (
        <div className="absolute inset-0 grid place-items-center px-4 text-center text-sm text-danger">
          截图加载失败，请刷新截图后重试
        </div>
      ) : null}
    </div>
  )
}
