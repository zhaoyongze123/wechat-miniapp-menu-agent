import { describe, expect, it } from 'vitest'
import {
  boxFromPoints,
  clientPointToImageUV,
  fitImageDimensions,
  getDetectorErrorMessage,
  moveBox,
  normalizeCandidateBox,
  resizeBox,
} from './reviewGeometry'
import type { MaterialStoreDetail } from './types'

describe('reviewGeometry', () => {
  it('normalizes pixel boxes and preserves normalized boxes', () => {
    expect(normalizeCandidateBox({ x: 100, y: 200, width: 300, height: 400 }, 1000, 2000)).toEqual({
      x: 0.1,
      y: 0.1,
      width: 0.3,
      height: 0.2,
    })
    expect(normalizeCandidateBox({ x: 0.2, y: 0.3, width: 0.4, height: 0.5 }, 1000, 2000)).toEqual({
      x: 0.2,
      y: 0.3,
      width: 0.4,
      height: 0.5,
    })
    expect(normalizeCandidateBox({ x: 100, y: 200, width: 0, height: 10 }, 1000, 1000)).toBeNull()
  })

  it('maps client coordinates accurately across zoom, pan, and 90/180/270 rotations', () => {
    const base = {
      stageCenterX: 500,
      stageCenterY: 400,
      baseWidth: 400,
      baseHeight: 200,
      zoom: 2,
      panX: 40,
      panY: -20,
    }
    // Center point after pan maps to (0.5, 0.5) for every rotation.
    for (const rotationDeg of [0, 90, 180, 270]) {
      const center = clientPointToImageUV({ ...base, clientX: 540, clientY: 380, rotationDeg })
      expect(center.u).toBeCloseTo(0.5, 5)
      expect(center.v).toBeCloseTo(0.5, 5)
    }

    // At 0 deg, moving +200px in clientX (= +100px in unscaled image = +0.25 of width) => u = 0.75
    const p0 = clientPointToImageUV({ ...base, clientX: 740, clientY: 380, rotationDeg: 0 })
    expect(p0.u).toBeCloseTo(0.75, 5)
    expect(p0.v).toBeCloseTo(0.5, 5)

    // At 90 deg CW, screen right (+dx) corresponds to image top (-iy), so v decreases.
    // +100px clientX => +50px unscaled => -50px iy => -50/200 = -0.25 => v = 0.25
    const p90 = clientPointToImageUV({ ...base, clientX: 640, clientY: 380, rotationDeg: 90 })
    expect(p90.u).toBeCloseTo(0.5, 5)
    expect(p90.v).toBeCloseTo(0.25, 5)

    // At 180 deg, screen right (+dx) corresponds to image left (-ix), so u decreases.
    const p180 = clientPointToImageUV({ ...base, clientX: 740, clientY: 380, rotationDeg: 180 })
    expect(p180.u).toBeCloseTo(0.25, 5)
    expect(p180.v).toBeCloseTo(0.5, 5)

    // At 270 deg CW, screen right (+dx) corresponds to image bottom (+iy), so v increases.
    const p270 = clientPointToImageUV({ ...base, clientX: 640, clientY: 380, rotationDeg: 270 })
    expect(p270.u).toBeCloseTo(0.5, 5)
    expect(p270.v).toBeCloseTo(0.75, 5)
  })

  it('supports drawing, moving, and corner resizing within [0, 1] bounds', () => {
    const drawn = boxFromPoints({ u: 0.6, v: 0.5 }, { u: 0.2, v: 0.1 })
    expect(drawn.x).toBeCloseTo(0.2, 5)
    expect(drawn.y).toBeCloseTo(0.1, 5)
    expect(drawn.width).toBeCloseTo(0.4, 5)
    expect(drawn.height).toBeCloseTo(0.4, 5)

    const moved = moveBox(drawn, 0.9, -0.5)
    expect(moved.x).toBeCloseTo(0.6, 5)
    expect(moved.y).toBeCloseTo(0, 5)

    const resized = resizeBox(drawn, 'se', 0.9, 0.8)
    expect(resized.x).toBeCloseTo(0.2, 5)
    expect(resized.y).toBeCloseTo(0.1, 5)
    expect(resized.width).toBeCloseTo(0.7, 5)
    expect(resized.height).toBeCloseTo(0.7, 5)
  })

  it('fits rotated images inside container without overflow', () => {
    const fit0 = fitImageDimensions(800, 600, 1600, 800, 0)
    expect(fit0.width).toBeCloseTo(800, 3)
    expect(fit0.height).toBeCloseTo(400, 3)

    const fit90 = fitImageDimensions(800, 600, 1600, 800, 90)
    // Rotated 90 deg: effective width is 800, effective height is 1600 => scale = 600/1600 = 0.375
    expect(fit90.width).toBeCloseTo(600, 3)
    expect(fit90.height).toBeCloseTo(300, 3)
  })

  it('distinguishes detector errors from normal no-candidate stores', () => {
    const baseDetail = {
      batch_id: 'b1',
      store_id: 's1',
      source_name: '测试店',
      source_folder: '测试店',
      analysis_status: 'no_candidates',
      reason: '未检测到二维码',
      recommendation: false,
      decision: 'pending',
      recommendation_level: 'none',
      wechat_validation_status: 'unverified',
      rejection: null,
      selected: null,
      images: [],
    } as MaterialStoreDetail

    expect(getDetectorErrorMessage(baseDetail, null)).toBeNull()
    expect(getDetectorErrorMessage({ ...baseDetail, analysis_status: 'failed', reason: 'ZXing 运行超时' }, null)).toBe('ZXing 运行超时')
    expect(getDetectorErrorMessage(baseDetail, { path: 'a.jpg', sha256: '1', detection_count: 0, candidates: [], image_index: 0, error: '图片解码失败' })).toBe('图片解码失败')
  })
})
