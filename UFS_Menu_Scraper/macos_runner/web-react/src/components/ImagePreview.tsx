import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type WheelEvent } from 'react'
import * as RadixDialog from '@radix-ui/react-dialog'
import { Button } from '@openai/apps-sdk-ui/components/Button'

export interface ImagePreviewItem {
  src: string
  alt: string
  caption?: string
}

interface ImagePreviewProps {
  open: boolean
  src: string
  alt: string
  caption?: string
  onOpenChange: (open: boolean) => void
  /** 可选图片序列；不传时维持原有单图调用方式。 */
  items?: ImagePreviewItem[]
  initialIndex?: number
}

/** 只读证据图片查看器。旋转、缩放和平移仅作用于预览变换，不会写回素材。 */
export function ImagePreview({
  open,
  src,
  alt,
  caption,
  onOpenChange,
  items,
  initialIndex = 0,
}: ImagePreviewProps) {
  const sequence = items?.length ? items : [{ src, alt, caption }]
  const sequenceKey = sequence.map((item) => item.src).join('\u0000')
  const [index, setIndex] = useState(initialIndex)
  const [scale, setScale] = useState(1)
  const [rotation, setRotation] = useState(0)
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const [naturalSize, setNaturalSize] = useState(false)
  const [imageError, setImageError] = useState(false)
  const [imageLoading, setImageLoading] = useState(true)
  const [retry, setRetry] = useState(0)
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 })
  const drag = useRef<{ pointerId: number; x: number; y: number; originX: number; originY: number } | null>(null)
  const viewport = useRef<HTMLDivElement>(null)
  const activeIndex = Math.min(index, sequence.length - 1)
  const active = sequence[activeIndex] ?? { src, alt, caption }

  const resetView = useCallback(() => {
    setScale(1)
    setRotation(0)
    setOffset({ x: 0, y: 0 })
    setNaturalSize(false)
  }, [])

  useEffect(() => {
    if (!open) return
    setIndex(Math.max(0, Math.min(initialIndex, sequence.length - 1)))
    resetView()
    setImageError(false)
    setImageLoading(true)
    setRetry(0)
    setDimensions({ width: 0, height: 0 })
  // Reset only when the dialog opens or the requested image sequence changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, src, initialIndex, sequenceKey, resetView])

  const showImage = (next: number) => {
    setIndex((next + sequence.length) % sequence.length)
    setImageError(false)
    setImageLoading(true)
    setRetry(0)
    setDimensions({ width: 0, height: 0 })
    resetView()
  }

  const zoomBy = (factor: number) => {
    setScale((current) => Math.max(0.2, Math.min(8, current * factor)))
    setNaturalSize(false)
  }

  const onWheel = (event: WheelEvent<HTMLDivElement>) => {
    // At fit size keep the drawer/page's normal wheel behavior; once enlarged,
    // wheel over the image becomes a predictable zoom gesture.
    if (scale <= 1 && !event.ctrlKey && !event.metaKey) return
    event.preventDefault()
    zoomBy(event.deltaY < 0 ? 1.12 : 1 / 1.12)
  }

  const onPointerDown = (event: PointerEvent<HTMLImageElement>) => {
    if ((scale <= 1 && !naturalSize) || event.button !== 0) return
    event.currentTarget.setPointerCapture(event.pointerId)
    drag.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, originX: offset.x, originY: offset.y }
  }

  const onPointerMove = (event: PointerEvent<HTMLImageElement>) => {
    if (!drag.current || drag.current.pointerId !== event.pointerId) return
    setOffset({ x: drag.current.originX + event.clientX - drag.current.x, y: drag.current.originY + event.clientY - drag.current.y })
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target instanceof HTMLElement && /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return
    if (event.key === 'ArrowLeft' && sequence.length > 1) { event.preventDefault(); showImage(activeIndex - 1) }
    else if (event.key === 'ArrowRight' && sequence.length > 1) { event.preventDefault(); showImage(activeIndex + 1) }
    else if (event.key === '+' || event.key === '=') { event.preventDefault(); zoomBy(1.2) }
    else if (event.key === '-') { event.preventDefault(); zoomBy(1 / 1.2) }
    else if (event.key.toLowerCase() === 'r') { event.preventDefault(); setRotation((value) => (value + 90) % 360) }
    else if (event.key === '0') { event.preventDefault(); resetView() }
  }

  const fitClass = naturalSize ? 'max-h-none max-w-none' : 'max-h-full max-w-full object-contain'
  const actualSrc = retry ? `${active.src}${active.src.includes('?') ? '&' : '?'}preview_retry=${retry}` : active.src

  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-[var(--modal-backdrop-background)]" />
        <RadixDialog.Content
          className="fixed inset-3 z-[60] flex flex-col overflow-hidden rounded-xl border border-subtle bg-surface shadow-[var(--shadow-400),var(--shadow-hairline)] sm:inset-6"
          aria-describedby={undefined}
          onKeyDown={onKeyDown}
        >
          <header className="flex flex-wrap items-center justify-between gap-3 border-b border-subtle px-4 py-2.5">
            <div className="min-w-0">
              <RadixDialog.Title className="truncate text-sm text-default">{active.caption ?? caption ?? active.alt}</RadixDialog.Title>
              <p className="text-2xs text-tertiary" aria-live="polite">{activeIndex + 1} / {sequence.length} · {Math.round(scale * 100)}%</p>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              {sequence.length > 1 ? <>
                <Button color="secondary" variant="outline" size="sm" aria-label="上一张" onClick={() => showImage(activeIndex - 1)}>上一张</Button>
                <Button color="secondary" variant="outline" size="sm" aria-label="下一张" onClick={() => showImage(activeIndex + 1)}>下一张</Button>
              </> : null}
              <Button color="secondary" variant="outline" size="sm" onClick={() => zoomBy(1 / 1.2)} aria-label="缩小">−</Button>
              <Button color="secondary" variant="outline" size="sm" onClick={() => zoomBy(1.2)} aria-label="放大">+</Button>
              <Button color="secondary" variant="outline" size="sm" onClick={() => { setNaturalSize(false); setScale(1); setOffset({ x: 0, y: 0 }) }} aria-pressed={!naturalSize && scale === 1}>适应窗口</Button>
              <Button color="secondary" variant="outline" size="sm" onClick={() => { setNaturalSize(true); setScale(1); setOffset({ x: 0, y: 0 }) }} aria-pressed={naturalSize}>原始尺寸</Button>
              <Button color="secondary" variant="outline" size="sm" onClick={() => setRotation((value) => (value + 90) % 360)} aria-label="向右旋转">旋转</Button>
              <Button color="secondary" variant="outline" size="sm" onClick={resetView}>重置</Button>
              <RadixDialog.Close asChild>
                <Button color="secondary" variant="outline" size="sm">关闭</Button>
              </RadixDialog.Close>
            </div>
          </header>
          <div
            ref={viewport}
            className="relative flex min-h-0 flex-1 items-center justify-center overflow-auto bg-surface-secondary p-3"
            onWheel={onWheel}
            aria-label="图片查看区域；快捷键：左右方向键切换，+/- 缩放，R 旋转，0 重置"
          >
            {imageError ? (
              <div role="alert" className="flex flex-col items-center gap-3 text-sm text-secondary">
                <p>图片读取失败，请检查连接后重试。</p>
                <Button color="secondary" variant="outline" size="sm" onClick={() => { setImageError(false); setRetry((value) => value + 1) }}>重试</Button>
              </div>
            ) : (
              <>
                {imageLoading ? <p className="absolute text-sm text-secondary" role="status">正在加载图片…</p> : null}
                <img
                  key={`${active.src}:${retry}`}
                  src={actualSrc}
                  alt={active.alt}
                  onLoad={(event) => {
                    setImageLoading(false)
                    setDimensions({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })
                  }}
                  onError={() => { setImageLoading(false); setImageError(true) }}
                  draggable={false}
                  onPointerDown={onPointerDown}
                  onPointerMove={onPointerMove}
                  onPointerUp={() => { drag.current = null }}
                  className={`${fitClass} select-none ${imageLoading ? 'invisible' : ''} ${scale > 1 || naturalSize ? 'cursor-grab active:cursor-grabbing' : ''}`}
                  style={{
                    transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale}) rotate(${rotation}deg)`,
                    transformOrigin: 'center center',
                    width: naturalSize && dimensions.width ? dimensions.width : undefined,
                    height: naturalSize && dimensions.height ? dimensions.height : undefined,
                  }}
                />
              </>
            )}
          </div>
          <footer className="border-t border-subtle px-4 py-2 text-2xs text-tertiary">左右方向键切换 · + / − 缩放 · R 旋转 · 0 重置 · 放大后可拖动</footer>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  )
}
