/**
 * 浏览器与本地 Python 服务的唯一传输层。
 *
 * 约束：
 * - 操作令牌只在内存中持有，进入页面时通过同源 `GET /api/session` 换取；
 * - 非 GET 请求统一附带 `X-Capture-Token`，不在日志、DOM 或静态产物中回显；
 * - 只有服务端明确拒绝令牌且同源能换到新值时，才允许重试一次。
 */

export type ApiMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

export interface ApiRequest {
  method?: ApiMethod
  /** JSON 请求体；与 body 二选一。 */
  json?: unknown
  /** 原始请求体（上传等二进制场景）。 */
  body?: BodyInit | undefined
  signal?: AbortSignal | undefined
  headers?: Record<string, string>
}

export class ApiError extends Error {
  readonly status: number
  readonly conflicts: unknown[] | null

  constructor(message: string, status: number, conflicts: unknown[] | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.conflicts = conflicts
  }
}

interface ErrorBody {
  detail?: unknown
  conflicts?: unknown
}

let tokenPromise: Promise<string> | null = null

function sessionError(status: number): ApiError {
  return new ApiError('无法建立本机操作会话，请刷新页面重试', status)
}

/** 内存中的会话令牌；首次非 GET 请求时按需获取。 */
export function ensureSession(force = false): Promise<string> {
  if (!force && tokenPromise) return tokenPromise
  tokenPromise = fetch('/api/session', {
    method: 'GET',
    headers: { Accept: 'application/json' },
    cache: 'no-store',
  })
    .then(async (response) => {
      if (!response.ok) throw sessionError(response.status)
      const payload: unknown = await response.json()
      const token = typeof payload === 'object' && payload !== null ? (payload as { token?: unknown }).token : null
      if (typeof token !== 'string' || !token) throw sessionError(response.status)
      return token
    })
    .catch((error: unknown) => {
      tokenPromise = null
      throw error
    })
  return tokenPromise
}

async function readError(response: Response): Promise<{ detail: string; conflicts: unknown[] | null }> {
  let payload: ErrorBody | null = null
  try {
    payload = (await response.json()) as ErrorBody
  } catch {
    payload = null
  }
  const detail = typeof payload?.detail === 'string' && payload.detail ? payload.detail : `请求失败（${response.status}）`
  const conflicts = Array.isArray(payload?.conflicts) ? payload.conflicts : null
  return { detail, conflicts }
}

async function send(path: string, request: ApiRequest, freshSession: boolean): Promise<Response> {
  const method = request.method ?? 'GET'
  const headers: Record<string, string> = { Accept: 'application/json', ...request.headers }
  if (method !== 'GET') headers['X-Capture-Token'] = await ensureSession(freshSession)
  let body = request.body
  if (request.json !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(request.json)
  }
  return fetch(path, {
    method,
    headers,
    body: body ?? null,
    signal: request.signal ?? null,
    cache: 'no-store',
  })
}

/** 发起同源 API 请求并解析 JSON；错误按 HTTP 语义抛出 ApiError。 */
export async function apiFetch<T>(path: string, request: ApiRequest = {}): Promise<T> {
  const method = request.method ?? 'GET'
  let response = await send(path, request, false)
  if (response.status === 403 && method !== 'GET' && !request.headers?.['X-Capture-Token']) {
    const { detail } = await readError(response.clone())
    if (detail.includes('令牌')) {
      // 服务端重启后会换发令牌，用同源会话换取一次新值再重试。
      response = await send(path, request, true)
    }
  }
  if (!response.ok) {
    const { detail, conflicts } = await readError(response)
    throw new ApiError(detail, response.status, conflicts)
  }
  if (response.status === 204) return undefined as T
  const type = response.headers.get('content-type') ?? ''
  if (type.includes('application/json')) return (await response.json()) as T
  return (await response.text()) as T
}

/** 上传原始字节时使用：不设置 JSON 头，令牌与错误处理保持一致。 */
export function apiUpload<T>(path: string, body: BodyInit, signal?: AbortSignal): Promise<T> {
  return apiFetch<T>(path, { method: 'PUT', body, ...(signal ? { signal } : {}) })
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '请求失败'
}
