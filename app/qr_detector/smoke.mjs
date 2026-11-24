// 最小 smoke test：node smoke.mjs <图片1> [图片2 ...]
// 输出每张图片的 kind / 定位点数 / confidence，用于人工确认依赖可用。
import { detectMiniProgramCodeFromFile } from '@weapp-vite/qr'

const paths = process.argv.slice(2)
if (!paths.length) {
  console.error('usage: node smoke.mjs <image> [image ...]')
  process.exit(2)
}

for (const path of paths) {
  try {
    const result = await detectMiniProgramCodeFromFile(path)
    process.stdout.write(`${JSON.stringify({
      path,
      detected: Boolean(result),
      kind: result?.kind ?? null,
      locatorCount: Array.isArray(result?.locatorPoints) ? result.locatorPoints.length : 0,
      confidence: typeof result?.confidence === 'number' ? result.confidence : null
    })}\n`)
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ path, error: error?.name ?? 'Error' })}\n`)
  }
}
