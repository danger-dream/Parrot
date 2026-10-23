import type { components } from './schema'

export type Schemas = components['schemas']

export const API_BASE = '/api/management/v1'

export interface PageMeta {
  requestId?: string
  page?: number
  pageSize?: number
  total?: number
  hasNext?: boolean
}

export interface Envelope<T> {
  data: T
  meta: PageMeta
}

export interface ApiErrorBody {
  code: string
  message: string
  fields?: { path: string; code: string; message: string }[]
  retryable?: boolean
  requestId?: string
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly fields: ApiErrorBody['fields']
  readonly retryable: boolean
  readonly requestId?: string
  /** The whole response body; some endpoints put extra blocks (e.g. `conflict`) next to `error`. */
  readonly payload: Record<string, unknown>

  constructor(status: number, body: Partial<ApiErrorBody>, payload: Record<string, unknown> = {}) {
    super(body.message || `HTTP ${status}`)
    this.status = status
    this.code = body.code || (status === 0 ? 'NETWORK_ERROR' : `HTTP_${status}`)
    this.fields = body.fields || []
    this.retryable = Boolean(body.retryable)
    this.requestId = body.requestId
    this.payload = payload
  }

  get isAuth(): boolean {
    return this.code === 'SESSION_REQUIRED' || this.code === 'SESSION_EXPIRED'
  }
}

type Scalar = string | number | boolean | null | undefined
type Query = Record<string, Scalar | Scalar[]>

export interface RequestOptions {
  query?: Query
  body?: unknown
  headers?: Record<string, string>
  ifMatch?: string
  signal?: AbortSignal
}

let unauthorizedHandler: (() => void) | null = null

export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler
}

function buildUrl(path: string, query?: Query): string {
  const url = API_BASE + path
  if (!query) return url
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item === undefined || item === null || item === '') continue
      params.append(key, String(item))
    }
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<Envelope<T>> {
  const headers: Record<string, string> = { Accept: 'application/json', ...opts.headers }
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json'
  if (opts.ifMatch) headers['If-Match'] = opts.ifMatch
  let response: Response
  try {
    response = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      credentials: 'same-origin',
      signal: opts.signal,
    })
  } catch (err) {
    if ((err as Error).name === 'AbortError') throw err
    throw new ApiError(0, { message: '无法连接到 Parrot 服务' })
  }
  if (response.status === 204) return { data: undefined as T, meta: {} }
  let payload: unknown = null
  const text = await response.text()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = null
    }
  }
  if (!response.ok) {
    const raw = payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {}
    const body = (raw.error as ApiErrorBody | undefined) || { message: text.slice(0, 200) }
    const error = new ApiError(response.status, body, raw)
    if (error.isAuth && unauthorizedHandler) unauthorizedHandler()
    throw error
  }
  return payload as Envelope<T>
}

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) => request<T>('GET', path, { query, signal }),
  post: <T>(path: string, body?: unknown, opts: RequestOptions = {}) => request<T>('POST', path, { ...opts, body }),
  patch: <T>(path: string, body?: unknown, opts: RequestOptions = {}) => request<T>('PATCH', path, { ...opts, body }),
  put: <T>(path: string, body?: unknown, opts: RequestOptions = {}) => request<T>('PUT', path, { ...opts, body }),
  del: <T>(path: string, opts: RequestOptions = {}) => request<T>('DELETE', path, opts),
}

export function enc(segment: string): string {
  return encodeURIComponent(segment)
}
