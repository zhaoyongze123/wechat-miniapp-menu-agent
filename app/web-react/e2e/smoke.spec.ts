import { expect, test } from '@playwright/test'

/**
 * 真实 Chromium 防白屏冒烟 E2E：
 * 校验首屏及核心路由在真实浏览器中渲染正常，无未捕获异常、无 console.error、无 5xx。
 * 运行前由脚本或 CI 在隔离临时目录启动本地 FastAPI（默认 http://127.0.0.1:18765）。
 */
test('控制台首屏与核心路由真实渲染无白屏、无运行时报错', async ({ page }) => {
  const pageErrors: string[] = []
  const consoleErrors: string[] = []
  const serverErrors: string[] = []

  page.on('pageerror', (err) => pageErrors.push(err.message))
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text())
  })
  page.on('response', (res) => {
    if (res.status() >= 500) serverErrors.push(`${res.status()} ${res.url()}`)
  })

  for (const route of ['/', '/settings', '/diagnostics', '/task-center']) {
    await page.goto(route, { waitUntil: 'networkidle' })
    const rootHtml = await page.locator('#root').innerHTML()
    expect(rootHtml.trim().length, `路由 ${route} 渲染为空白页`).toBeGreaterThan(20)
  }

  expect(pageErrors, `未捕获页面异常: ${pageErrors.join(' | ')}`).toEqual([])
  expect(consoleErrors, `浏览器 console.error: ${consoleErrors.join(' | ')}`).toEqual([])
  expect(serverErrors, `服务端 5xx 错误: ${serverErrors.join(' | ')}`).toEqual([])
})
