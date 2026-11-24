import { useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import type { Box } from './types'

interface ManualQrCanvasProps {
  imageUrl: string
  box: Box | null
  onChange: (box: Box | null) => void
  disabled?: boolean
}

interface DragState {
  startX: number
  startY: number
  currentX: number
  currentY: number
}

function clamp(value: number): number {
  return Math.min(1, Math.max(0, value))
}

function boxFromDrag(drag: DragState): Box {
  const x = Math.min(drag.startX, drag.currentX)
  const y = Math.min(drag.startY, drag.currentY)
  return {
    x,
    y,
    width: Math.abs(drag.currentX - drag.startX),
    height: Math.abs(drag.currentY - drag.startY),
  }
}

/**
 * 原图上的手选框。
 * 坐标按显示尺寸归一化（0–1），由后端 `_display_box_to_source` 换算回原图，
 * 因此这里不读取图片像素尺寸，也不做 EXIF 旋转处理。
 */
export function ManualQrCanvas({ imageUrl, box, onChange, disabled = false }: ManualQrCanvasProps) {
  const overlayRef = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<DragState | null>(null)

  const pointOf = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = overlayRef.current?.getBoundingClientRect()
    if (!rect || !rect.width || !rect.height) return { x: 0, y: 0 }
    return {
      x: clamp((event.clientX - rect.left) / rect.width),
      y: clamp((event.clientY - rect.top) / rect.height),
    }
  }

  const handleDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (disabled) return
    const point = pointOf(event)
    event.currentTarget.setPointerCapture(event.pointerId)
    setDrag({ startX: point.x, startY: point.y, currentX: point.x, currentY: point.y })
  }

  const handleMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!drag || disabled) return
    const point = pointOf(event)
    setDrag({ ...drag, currentX: point.x, currentY: point.y })
  }

  const handleUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!drag || disabled) return
    event.currentTarget.releasePointerCapture(event.pointerId)
    const next = boxFromDrag(drag)
    setDrag(null)
    // 小于 2% 的框视为误触，保留原有选择。
    if (next.width < 0.02 || next.height < 0.02) return
    onChange(next)
  }

  const display = drag ? boxFromDrag(drag) : box

  return (
    <div className="space-y-2">
      <div className="relative overflow-hidden rounded-lg border border-subtle bg-surface-secondary">
        <img src={imageUrl} alt="原始采集图片" className="block w-full" draggable={false} />
        <div
          ref={overlayRef}
          role="presentation"
          className={`absolute inset-0 touch-none ${disabled ? '' : 'cursor-crosshair'}`}
          onPointerDown={handleDown}
          onPointerMove={handleMove}
          onPointerUp={handleUp}
          onPointerCancel={() => setDrag(null)}
        >
          {display ? (
            <span
              className="pointer-events-none absolute border-2 border-primary-solid bg-secondary-soft/40"
              style={{
                left: `${display.x * 100}%`,
                top: `${display.y * 100}%`,
                width: `${display.width * 100}%`,
                height: `${display.height * 100}%`,
              }}
            />
          ) : null}
        </div>
      </div>
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-secondary">在原图上拖拽框选二维码；松开后立即生效。</p>
        {box ? (
          <button
            type="button"
            className="shrink-0 text-xs text-secondary underline-offset-2 hover:underline"
            onClick={() => onChange(null)}
            disabled={disabled}
          >
            清除框选
          </button>
        ) : null}
      </div>
    </div>
  )
}
