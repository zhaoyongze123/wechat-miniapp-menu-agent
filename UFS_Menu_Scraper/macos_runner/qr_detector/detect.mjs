import { createInterface } from 'node:readline'
import { detectMiniProgramCodeFromFile } from '@weapp-vite/qr'

const DETECTOR_KIND = 'wechat-mini-program-code'

function finiteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function point(value) {
  if (!value || typeof value !== 'object') return null
  const x = finiteNumber(value.x)
  const y = finiteNumber(value.y)
  return x === null || y === null ? null : { x, y }
}

function bounds(value) {
  if (!value || typeof value !== 'object') return null
  // 包内部返回 left/top，这里统一成 x/y/width/height。
  const x = finiteNumber(value.x ?? value.left)
  const y = finiteNumber(value.y ?? value.top)
  const width = finiteNumber(value.width)
  const height = finiteNumber(value.height)
  if (x === null || y === null || width === null || height === null) return null
  return { x, y, width, height }
}

function sanitize(result) {
  if (!result || typeof result !== 'object') return null
  if (result.kind !== DETECTOR_KIND) return null
  const locatorPoints = Array.isArray(result.locatorPoints)
    ? result.locatorPoints.map(point).filter(Boolean)
    : []
  return {
    kind: DETECTOR_KIND,
    confidence: finiteNumber(result.confidence),
    locatorPoints,
    badgeBounds: bounds(result.badgeBounds),
    logoBounds: bounds(result.logoBounds)
  }
}

function errorCode(error) {
  const name = error && typeof error.name === 'string' ? error.name : ''
  if (name === 'TypeError' || name === 'SyntaxError') return 'invalid_request'
  return 'detector_error'
}

function write(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`)
}

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity })

lines.on('line', async (line) => {
  if (!line.trim()) return
  let id = null
  try {
    const request = JSON.parse(line)
    id = typeof request.id === 'string' ? request.id : null
    if (!id || typeof request.path !== 'string' || !request.path) {
      write({ id, ok: false, error_code: 'invalid_request' })
      return
    }
    const result = await detectMiniProgramCodeFromFile(request.path)
    write({ id, ok: true, result: sanitize(result) })
  } catch (error) {
    // 不回传异常文本与路径，避免把图片信息写进日志。
    write({ id, ok: false, error_code: errorCode(error) })
  }
})

lines.on('close', () => process.exit(0))
