import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent as ReactWheelEvent,
} from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { StatusBadge } from '../../components/StatusBadge'
import {
  MIN_BOX_SIZE,
  boxFromPoints,
  clamp,
  clientPointToImageUV,
  fitImageDimensions,
  isUsableBox,
  moveBox,
  normalizeCandidateBox,
  resizeBox,
  type ResizeHandle,
} from './reviewGeometry'
import type { Box, QrCandidate, StoreImage } from './types'

interface ReviewImageCanvasProps {
  images: StoreImage[]
  imageIndex: number
  onSelectImage: (index: number) => void
  getImageUrl: (index: number) => string
  storeName: string
  visibleCandidates: QrCandidate[]
  selectedCandidateIndex: number | null
  savedCandidateIndex: number | null
  canEdit: boolean
  onSelectCandidate: (candidate: QrCandidate) => void
  manualMode: boolean
  manualBox: Box | null
  onManualBoxChange: (box: Box | null) => void
  onToggleManualMode: () => void
  onOpenFullscreen: () => void
  onRotationChange?: (rotationDeg: number) => void
  onImageElementChange?: (img: HTMLImageElement | null) => void
}

type InteractionState =
  | { kind: 'pan'; pointerId: number; startX: number; startY: number; originPanX: number; originPanY: number }
  | { kind: 'draw'; pointerId: number; startUV: { u: number; v: number } }
  | { kind: 'move'; pointerId: number; startUV: { u: number; v: number }; originBox: Box }
  | { kind: 'resize'; pointerId: number; handle: ResizeHandle; originBox: Box }

const HANDLES: { id: ResizeHandle; className: string; label: string }[] = [
  { id: 'nw', className: '-top-1.5 -left-1.5 cursor-nwse-resize', label: '左上角缩放' },
  { id: 'ne', className: '-top-1.5 -right-1.5 cursor-nesw-resize', label: '右上角缩放' },
  { id: 'sw', className: '-bottom-1.5 -left-1.5 cursor-nesw-resize', label: '左下角缩放' },
  { id: 'se', className: '-bottom-1.5 -right-1.5 cursor-nwse-resize', label: '右下角缩放' },
]

export function ReviewImageCanvas({
  images,
  imageIndex,
  onSelectImage,
  getImageUrl,
  storeName,
  visibleCandidates,
  selectedCandidateIndex,
  savedCandidateIndex,
  canEdit,
  onSelectCandidate,
  manualMode,
  manualBox,
  onManualBoxChange,
  onToggleManualMode,
  onOpenFullscreen,
  onRotationChange,
  onImageElementChange,
}: ReviewImageCanvasProps) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const interactionRef = useRef<InteractionState | null>(null)

  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 })
  const [naturalSize, setNaturalSize] = useState({ width: 0, height: 0 })
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [rotation, setRotation] = useState(0)
  const [showBoxes, setShowBoxes] = useState(true)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const [retryCount, setRetryCount] = useState(0)

  const currentImage = images[imageIndex] ?? null
  const baseImageUrl = currentImage ? getImageUrl(imageIndex) : ''
  const imageUrl = retryCount > 0 && baseImageUrl
    ? `${baseImageUrl}${baseImageUrl.includes('?') ? '&' : '?'}retry=${retryCount}`
    : baseImageUrl

  // 监听画布容器尺寸变化（含左侧队列折叠与窗口缩放），自动适应图片尺寸。
  useEffect(() => {
    const el = viewportRef.current
    if (!el) return
    const update = () => {
      const rect = el.getBoundingClientRect()
      setViewportSize({ width: Math.max(0, rect.width - 24), height: Math.max(0, rect.height - 24) })
    }
    update()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(update)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // 切换图片时重置视图状态。
  useEffect(() => {
    setZoom(1)
    setPan({ x: 0, y: 0 })
    setRotation(0)
    setNaturalSize({ width: 0, height: 0 })
    setLoading(Boolean(baseImageUrl))
    setLoadError(false)
    setRetryCount(0)
    onRotationChange?.(0)
    onImageElementChange?.(null)
  }, [baseImageUrl, onImageElementChange, onRotationChange])

  const fitSize = useMemo(
    () => fitImageDimensions(viewportSize.width, viewportSize.height, naturalSize.width, naturalSize.height, rotation),
    [naturalSize.height, naturalSize.width, rotation, viewportSize.height, viewportSize.width],
  )

  const zoomTo1To1 = useCallback(() => {
    if (fitSize.width <= 0 || naturalSize.width <= 0) return
    const ratio = clamp(naturalSize.width / fitSize.width, 0.25, 6)
    setZoom(ratio)
    setPan({ x: 0, y: 0 })
  }, [fitSize.width, naturalSize.width])

  const resetView = useCallback(() => {
    setZoom(1)
    setPan({ x: 0, y: 0 })
    setRotation(0)
    onRotationChange?.(0)
  }, [onRotationChange])

  const rotateBy = useCallback(
    (delta: number) => {
      setRotation((prev) => {
        const next = ((prev + delta) % 360 + 360) % 360
        onRotationChange?.(next)
        return next
      })
      setPan({ x: 0, y: 0 })
    },
    [onRotationChange],
  )

  const zoomBy = useCallback((factor: number) => {
    setZoom((prev) => clamp(prev * factor, 0.25, 6))
  }, [])

  const getStageCenter = useCallback(() => {
    const rect = viewportRef.current?.getBoundingClientRect()
    if (!rect) return null
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
  }, [])

  const toUV = useCallback(
    (clientX: number, clientY: number) => {
      const center = getStageCenter()
      if (!center || fitSize.width <= 0 || fitSize.height <= 0) return null
      return clientPointToImageUV({
        clientX,
        clientY,
        stageCenterX: center.x,
        stageCenterY: center.y,
        baseWidth: fitSize.width,
        baseHeight: fitSize.height,
        zoom,
        panX: pan.x,
        panY: pan.y,
        rotationDeg: rotation,
      })
    },
    [fitSize.height, fitSize.width, getStageCenter, pan.x, pan.y, rotation, zoom],
  )

  const handleWheel = (event: ReactWheelEvent<HTMLDivElement>) => {
    if (!currentImage) return
    event.preventDefault()
    const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15
    setZoom((prev) => clamp(prev * factor, 0.25, 6))
  }

  const handleViewportPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!currentImage || loadError || loading) return
    // 中键或按住 Shift 随时允许平移；非手动框选模式左键也可直接平移。
    const shouldPan = event.button === 1 || (event.button === 0 && (!manualMode || event.shiftKey))
    if (shouldPan) {
      event.preventDefault()
      event.currentTarget.setPointerCapture(event.pointerId)
      interactionRef.current = {
        kind: 'pan',
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        originPanX: pan.x,
        originPanY: pan.y,
      }
      return
    }
    if (manualMode && canEdit && event.button === 0) {
      const uv = toUV(event.clientX, event.clientY)
      if (!uv) return
      event.preventDefault()
      event.currentTarget.setPointerCapture(event.pointerId)
      interactionRef.current = { kind: 'draw', pointerId: event.pointerId, startUV: uv }
      onManualBoxChange({ x: uv.u, y: uv.v, width: 0, height: 0 })
    }
  }

  const handleBoxMovePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!manualMode || !canEdit || !manualBox || event.button !== 0 || event.shiftKey) return
    event.stopPropagation()
    const uv = toUV(event.clientX, event.clientY)
    if (!uv) return
    viewportRef.current?.setPointerCapture(event.pointerId)
    interactionRef.current = {
      kind: 'move',
      pointerId: event.pointerId,
      startUV: uv,
      originBox: manualBox,
    }
  }

  const handleResizePointerDown = (handle: ResizeHandle, event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!manualMode || !canEdit || !manualBox || event.button !== 0) return
    event.stopPropagation()
    viewportRef.current?.setPointerCapture(event.pointerId)
    interactionRef.current = {
      kind: 'resize',
      pointerId: event.pointerId,
      handle,
      originBox: manualBox,
    }
  }

  const handleViewportPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const state = interactionRef.current
    if (!state || state.pointerId !== event.pointerId) return

    if (state.kind === 'pan') {
      setPan({
        x: state.originPanX + (event.clientX - state.startX),
        y: state.originPanY + (event.clientY - state.startY),
      })
      return
    }
    const uv = toUV(event.clientX, event.clientY)
    if (!uv) return

    if (state.kind === 'draw') {
      onManualBoxChange(boxFromPoints(state.startUV, uv))
    } else if (state.kind === 'move') {
      onManualBoxChange(moveBox(state.originBox, uv.u - state.startUV.u, uv.v - state.startUV.v))
    } else if (state.kind === 'resize') {
      onManualBoxChange(resizeBox(state.originBox, state.handle, uv.u, uv.v, MIN_BOX_SIZE))
    }
  }

  const handleViewportPointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const state = interactionRef.current
    if (!state || state.pointerId !== event.pointerId) return
    if (state.kind === 'draw') {
      const uv = toUV(event.clientX, event.clientY)
      const next = uv ? boxFromPoints(state.startUV, uv) : null
      onManualBoxChange(isUsableBox(next) ? next : null)
    }
    interactionRef.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }

  const normalizedCandidates = useMemo(() => {
    return visibleCandidates
      .map((candidate) => ({
        candidate,
        box: normalizeCandidateBox(candidate.box, naturalSize.width, naturalSize.height),
      }))
      .filter((item): item is { candidate: QrCandidate; box: Box } => item.box !== null)
  }, [naturalSize.height, naturalSize.width, visibleCandidates])

  const zoomPercent = Math.round(zoom * 100)

  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col bg-surface-secondary" aria-label="原图审核画布">
      {/* 画布工具栏 */}
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-subtle bg-surface px-3 py-2">
        <div className="flex flex-wrap items-center gap-1">
          <Button color="secondary" variant="outline" size="sm" onClick={() => zoomBy(1 / 1.25)} aria-label="缩小" disabled={!currentImage}>
            −
          </Button>
          <span className="min-w-12 text-center text-xs tabular-nums text-secondary">{zoomPercent}%</span>
          <Button color="secondary" variant="outline" size="sm" onClick={() => zoomBy(1.25)} aria-label="放大" disabled={!currentImage}>
            +
          </Button>
          <Button
            color="secondary"
            variant="ghost"
            size="sm"
            onClick={() => {
              setZoom(1)
              setPan({ x: 0, y: 0 })
            }}
            disabled={!currentImage}
          >
            适应
          </Button>
          <Button color="secondary" variant="ghost" size="sm" onClick={zoomTo1To1} disabled={!currentImage || !naturalSize.width}>
            1:1
          </Button>
          <Button color="secondary" variant="ghost" size="sm" onClick={() => rotateBy(-90)} aria-label="向左旋转" disabled={!currentImage}>
            左转
          </Button>
          <Button color="secondary" variant="ghost" size="sm" onClick={() => rotateBy(90)} aria-label="向右旋转" disabled={!currentImage}>
            右转
          </Button>
          <Button color="secondary" variant="ghost" size="sm" onClick={resetView} disabled={!currentImage}>
            重置
          </Button>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {currentImage && visibleCandidates.length > 0 ? (
            <Button
              color={showBoxes ? 'secondary' : 'secondary'}
              variant={showBoxes ? 'soft' : 'ghost'}
              size="sm"
              aria-pressed={showBoxes}
              onClick={() => setShowBoxes((prev) => !prev)}
            >
              候选框（{visibleCandidates.length}）
            </Button>
          ) : null}
          {currentImage ? (
            <Button
              color={manualMode ? 'primary' : 'secondary'}
              variant={manualMode ? 'solid' : 'outline'}
              size="sm"
              disabled={!canEdit}
              onClick={onToggleManualMode}
            >
              {manualMode ? '退出框选 (M)' : '手动框选 (M)'}
            </Button>
          ) : null}
          {manualMode && manualBox ? (
            <Button color="secondary" variant="ghost" size="sm" onClick={() => onManualBoxChange(null)}>
              清除框选
            </Button>
          ) : null}
          {currentImage ? (
            <Button color="secondary" variant="outline" size="sm" onClick={onOpenFullscreen}>
              全屏
            </Button>
          ) : null}
        </div>
      </div>

      {/* 主图像视口 */}
      <div
        ref={viewportRef}
        onWheel={handleWheel}
        onPointerDown={handleViewportPointerDown}
        onPointerMove={handleViewportPointerMove}
        onPointerUp={handleViewportPointerUp}
        onPointerCancel={handleViewportPointerUp}
        className={[
          'relative flex min-h-0 flex-1 items-center justify-center overflow-hidden p-3 select-none',
          manualMode && canEdit ? 'cursor-crosshair' : 'cursor-grab active:cursor-grabbing',
        ].join(' ')}
        aria-label={manualMode ? '手动框选画布：拖拽画框，按住 Shift 可平移，滚轮可缩放' : '原图审核画布：滚轮缩放，拖拽平移'}
      >
        {!currentImage ? (
          <div className="text-center text-sm text-secondary">该门店暂无原图，请在右侧折叠面板中补图后重新分析。</div>
        ) : loadError ? (
          <div role="alert" className="flex flex-col items-center gap-2 text-sm text-secondary">
            <p>原图加载失败，请检查网络连接。</p>
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              onClick={() => {
                setLoadError(false)
                setLoading(true)
                setRetryCount((prev) => prev + 1)
              }}
            >
              重新加载
            </Button>
          </div>
        ) : (
          <>
            {loading ? (
              <div role="status" className="absolute inset-0 z-10 flex items-center justify-center text-sm text-secondary">
                正在加载原图…
              </div>
            ) : null}

            {/* 变换舞台：原图、候选框、手动框选框共享同一坐标系，确保缩放/平移/旋转完全同步 */}
            <div
              className="relative shrink-0"
              style={{
                width: fitSize.width > 0 ? `${fitSize.width}px` : undefined,
                height: fitSize.height > 0 ? `${fitSize.height}px` : undefined,
                transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom}) rotate(${rotation}deg)`,
                transformOrigin: 'center center',
              }}
            >
              <img
                ref={(node) => {
                  imgRef.current = node
                }}
                key={imageUrl}
                src={imageUrl}
                alt={`${storeName} 原图 ${imageIndex + 1}`}
                draggable={false}
                onLoad={(event) => {
                  const target = event.currentTarget
                  setLoading(false)
                  setLoadError(false)
                  setNaturalSize({ width: target.naturalWidth, height: target.naturalHeight })
                  onImageElementChange?.(target)
                }}
                onError={() => {
                  setLoading(false)
                  setLoadError(true)
                  onImageElementChange?.(null)
                }}
                className={[
                  'block h-full w-full rounded-md object-contain shadow-sm pointer-events-none',
                  loading ? 'invisible' : '',
                ].join(' ')}
              />

              {/* 候选框叠加层 */}
              {!loading && showBoxes && normalizedCandidates.length > 0 ? (
                <div className="pointer-events-none absolute inset-0">
                  {normalizedCandidates.map(({ candidate, box }) => {
                    const isSelected = candidate.candidate_index === selectedCandidateIndex
                    const isSaved = candidate.candidate_index === savedCandidateIndex
                    const isRec = candidate.recommendation
                    return (
                      <button
                        key={candidate.candidate_index}
                        type="button"
                        onPointerDown={(event) => event.stopPropagation()}
                        onClick={(event) => {
                          event.stopPropagation()
                          if (manualMode) return
                          onSelectCandidate(candidate)
                        }}
                        title={`候选 ${candidate.candidate_index + 1}${isRec ? '（推荐）' : ''}`}
                        aria-label={`选择候选 ${candidate.candidate_index + 1}`}
                        className={[
                          'pointer-events-auto absolute rounded-sm transition-shadow focus:outline-none',
                          manualMode ? 'opacity-40 pointer-events-none' : 'cursor-pointer',
                          isSelected || isSaved
                            ? 'border-2 border-primary-solid bg-primary-soft/20 ring-2 ring-primary-solid/40'
                            : isRec
                              ? 'border-2 border-info-solid bg-info-soft/15 hover:ring-2 hover:ring-info-solid/40'
                              : 'border-2 border-warning-solid bg-warning-soft/10 hover:border-primary-solid',
                        ].join(' ')}
                        style={{
                          left: `${box.x * 100}%`,
                          top: `${box.y * 100}%`,
                          width: `${box.width * 100}%`,
                          height: `${box.height * 100}%`,
                        }}
                      >
                        <span
                          className={[
                            'absolute -top-5 left-0 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs font-medium whitespace-nowrap shadow-sm',
                            isSelected || isSaved
                              ? 'bg-primary-solid text-inverse'
                              : isRec
                                ? 'bg-info-solid text-inverse'
                                : 'bg-surface text-default border border-subtle',
                          ].join(' ')}
                        >
                          #{candidate.candidate_index + 1}
                          {isRec ? ' 推荐' : ''}
                        </span>
                      </button>
                    )
                  })}
                </div>
              ) : null}

              {/* 手动框选叠加层 */}
              {!loading && manualMode && manualBox && (manualBox.width > 0 || manualBox.height > 0) ? (
                <div
                  role="region"
                  aria-label="手动框选区域"
                  onPointerDown={handleBoxMovePointerDown}
                  className="absolute cursor-move rounded-sm border-2 border-primary-solid bg-primary-soft/20 ring-2 ring-primary-solid/40"
                  style={{
                    left: `${manualBox.x * 100}%`,
                    top: `${manualBox.y * 100}%`,
                    width: `${manualBox.width * 100}%`,
                    height: `${manualBox.height * 100}%`,
                  }}
                >
                  <span className="pointer-events-none absolute -top-5 left-0 rounded bg-primary-solid px-1.5 py-0.5 text-2xs font-medium whitespace-nowrap text-inverse shadow-sm">
                    人工框选
                  </span>
                  {canEdit
                    ? HANDLES.map((handle) => (
                        <button
                          key={handle.id}
                          type="button"
                          aria-label={handle.label}
                          onPointerDown={(event) => handleResizePointerDown(handle.id, event)}
                          className={`absolute h-3 w-3 rounded-full border-2 border-primary-solid bg-surface shadow-sm ${handle.className}`}
                        />
                      ))
                    : null}
                </div>
              ) : null}
            </div>

            {/* 底部悬浮状态提示 */}
            <div className="pointer-events-none absolute right-3 bottom-2 flex items-center gap-2 rounded-md border border-subtle bg-surface/90 px-2.5 py-1 text-2xs text-secondary shadow-sm">
              <span>
                第 {imageIndex + 1} / {images.length} 张
              </span>
              {naturalSize.width > 0 ? (
                <span>
                  · {naturalSize.width}×{naturalSize.height}
                </span>
              ) : null}
              {rotation !== 0 ? <span>· {rotation}°</span> : null}
              {manualMode ? <span className="text-default">· 拖拽画框 / Shift+拖动平移</span> : null}
            </div>
          </>
        )}
      </div>

      {/* 底部多图缩略图切换条 */}
      {images.length > 0 ? (
        <div
          className="flex shrink-0 items-center gap-2 overflow-x-auto border-t border-subtle bg-surface px-3 py-2"
          aria-label="原图缩略图列表"
        >
          {images.map((item, idx) => {
            const active = idx === imageIndex
            const okCount = item.candidates.filter((c) => c.quality !== 'rejected').length
            const hasRec = item.candidates.some((c) => c.recommendation && c.quality !== 'rejected')
            const hasIssue = Boolean(item.error || item.possible_missed_code)
            return (
              <button
                key={item.sha256 || idx}
                type="button"
                onClick={() => onSelectImage(idx)}
                aria-current={active ? 'true' : undefined}
                className={[
                  'group relative flex shrink-0 items-center gap-2 rounded-lg border p-1.5 text-left transition-colors',
                  active
                    ? 'border-primary-solid bg-secondary-soft ring-1 ring-primary-solid/40'
                    : 'border-subtle bg-surface hover:bg-secondary-ghost-hover',
                ].join(' ')}
              >
                <div className="relative h-12 w-12 shrink-0 overflow-hidden rounded border border-subtle bg-surface-secondary">
                  <img
                    src={getImageUrl(idx)}
                    alt={`缩略图 ${idx + 1}`}
                    loading="lazy"
                    className="h-full w-full object-cover"
                  />
                  <span className="absolute top-0.5 left-0.5 rounded bg-surface/90 px-1 text-2xs font-medium text-default">
                    {idx + 1}
                  </span>
                </div>
                <div className="min-w-0 pr-1 text-2xs">
                  <div className="flex items-center gap-1">
                    <span className="font-medium text-default">{okCount} 候选</span>
                    {hasRec ? <StatusBadge tone="info">推荐</StatusBadge> : null}
                  </div>
                  <div className="mt-0.5 truncate text-tertiary">
                    {item.error ? (
                      <span className="text-danger">检测异常</span>
                    ) : item.possible_missed_code ? (
                      <span className="text-warning">可能漏码</span>
                    ) : item.ordering_text_seen ? (
                      '含点餐字样'
                    ) : (
                      `共 ${item.candidates.length} 框`
                    )}
                  </div>
                  {hasIssue ? <span className="sr-only">存在异常</span> : null}
                </div>
              </button>
            )
          })}
        </div>
      ) : null}
    </section>
  )
}
