import { ApiError } from '@/api/client'

const MESSAGES: Record<string, string> = {
  INVALID_REQUEST: '请求内容不正确',
  CONFIRMATION_REQUIRED: '这个操作需要先确认',
  INVALID_OPERATION_STATE: '当前状态下不能执行这个操作',
  SESSION_REQUIRED: '登录已失效，请重新登录',
  SESSION_EXPIRED: '登录已过期，请重新登录',
  AUTHENTICATION_FAILED: '认证失败',
  CAPABILITY_DENIED: '没有权限执行这个操作',
  ORIGIN_DENIED: '当前访问地址未被允许',
  RESOURCE_NOT_FOUND: '对象不存在，可能已被删除',
  OPERATION_NOT_FOUND: '任务不存在或已过期',
  RESOURCE_CONFLICT: '与现有对象冲突',
  IDENTITY_CONFLICT: '已存在相同身份的账号',
  REVISION_CONFLICT: '数据已在别处被修改，请刷新后重试',
  STATE_CONFLICT: '状态已变化，请刷新后重试',
  VALIDATION_FAILED: '填写的内容不符合要求',
  UNSUPPORTED_VALUE: '不支持这个值',
  RATE_LIMITED: '操作太频繁，请稍后再试',
  OPERATION_ALREADY_RUNNING: '同类任务正在进行中',
  UPSTREAM_ERROR: '上游返回了错误',
  SERVICE_NOT_READY: '服务尚未就绪，请稍后再试',
  DEPENDENCY_UNAVAILABLE: '依赖的服务暂时不可用',
  UPSTREAM_TIMEOUT: '上游响应超时',
  NETWORK_ERROR: '无法连接到 Parrot',
}

function statusTitle(status: number): string | undefined {
  if (status === 500) return '服务器内部错误（多为上游请求失败）'
  if (status === 502 || status === 503 || status === 504) return '服务暂时不可用，请稍后再试'
  if (status === 404) return '接口不存在，可能需要升级 Parrot'
  return undefined
}

export function errorTitle(err: unknown): string {
  if (err instanceof ApiError) {
    if (MESSAGES[err.code]) return MESSAGES[err.code]
    if (err.code.startsWith('HTTP_')) return statusTitle(err.status) || `请求失败（HTTP ${err.status}）`
    return err.message || '操作失败'
  }
  if (err instanceof Error) return err.message
  return '操作失败'
}

/** Field-level messages ("path: message") for VALIDATION_FAILED, otherwise the server message. */
export function errorDetail(err: unknown): string | undefined {
  if (!(err instanceof ApiError)) return undefined
  if (err.fields?.length) return err.fields.map((f) => (f.path ? `${f.path}：${f.message}` : f.message)).join('\n')
  if (err.code.startsWith('HTTP_')) return undefined
  const fallback = MESSAGES[err.code]
  return err.message && err.message !== fallback ? err.message : undefined
}

export function fieldErrors(err: unknown): Record<string, string> {
  const out: Record<string, string> = {}
  if (err instanceof ApiError) for (const f of err.fields || []) out[f.path] = f.message
  return out
}
