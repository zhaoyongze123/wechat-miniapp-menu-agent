import { beforeEach, describe, expect, it, vi } from 'vitest'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

describe('API 会话与令牌', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.unstubAllGlobals()
  })

  it('GET 请求不换取操作令牌', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => jsonResponse({ records: [] }))
    vi.stubGlobal('fetch', fetchMock)
    const { apiFetch } = await import('./client')
    await apiFetch('/api/tasks/history')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/tasks/history')
  })

  it('写请求附带内存中的会话令牌', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      if (input === '/api/session') return jsonResponse({ token: 'local-token' })
      return jsonResponse({ ok: true })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { apiFetch } = await import('./client')
    await apiFetch('/api/tasks/history/capture/abc', { method: 'PATCH', json: { name: '新名字' } })
    const writeCall = fetchMock.mock.calls.find((call) => call[0] === '/api/tasks/history/capture/abc')
    const init = writeCall?.[1] as RequestInit | undefined
    expect((init?.headers as Record<string, string>)['X-Capture-Token']).toBe('local-token')
    expect(init?.body).toBe(JSON.stringify({ name: '新名字' }))
  })

  it('服务端明确拒绝令牌时只重试一次并换发新令牌', async () => {
    const sessions = ['old-token', 'new-token']
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input === '/api/session') {
        const token = sessions.shift() ?? 'extra-token'
        return jsonResponse({ token })
      }
      const token = (init?.headers as Record<string, string> | undefined)?.['X-Capture-Token']
      if (token === 'old-token') return jsonResponse({ detail: '操作令牌已失效，请刷新页面' }, 403)
      return jsonResponse({ ok: true })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { apiFetch } = await import('./client')
    await expect(apiFetch('/api/tasks/history/capture/abc', { method: 'PATCH', json: { name: 'x' } })).resolves.toEqual({
      ok: true,
    })
    const writes = fetchMock.mock.calls.filter((call) => call[0] !== '/api/session')
    expect(writes).toHaveLength(2)
  })
})
