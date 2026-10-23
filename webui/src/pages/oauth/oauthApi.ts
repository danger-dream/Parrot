import { reactive } from 'vue'
import { ApiError, api, enc } from '@/api/client'
import { isOperation, operationErrorText, waitOperation, type Operation } from '@/api/operations'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'

/** 409 IDENTITY_CONFLICT carries a one-shot replace plan next to the error body. */
export class IdentityConflict extends Error {
  constructor(
    readonly accountId: string,
    readonly replacePlanToken: string,
  ) {
    super('IDENTITY_CONFLICT')
  }
}

/** POST that turns a 409 IDENTITY_CONFLICT carrying a replace plan into an IdentityConflict. */
export async function postKeepingConflict<T>(path: string, body: unknown): Promise<T> {
  try {
    return (await api.post<T>(path, body)).data
  } catch (err) {
    const conflict = err instanceof ApiError ? (err.payload.conflict as { accountId?: string; replacePlanToken?: string } | undefined) : undefined
    if (err instanceof ApiError && err.status === 409 && conflict?.replacePlanToken) {
      throw new IdentityConflict(conflict.accountId || '', conflict.replacePlanToken)
    }
    throw err
  }
}

export function accountPath(id: string, suffix = ''): string {
  return `/oauth/accounts/${enc(id)}${suffix}`
}

/** Cross-component change signal: the list reloads, an open detail refreshes. */
export const oauthBus = reactive({ version: 0, changed: '' as string })

export function notifyChanged(accountId = ''): void {
  oauthBus.changed = accountId
  oauthBus.version++
}

const OP_ERRORS: Record<string, string> = {
  UPSTREAM_ERROR: '上游返回了错误（令牌无效、网络或服务异常）',
  UPSTREAM_TIMEOUT: '上游响应超时',
  INVALID_OPERATION_STATE: '当前状态下不能执行',
  RESOURCE_NOT_FOUND: '账号已不存在',
  REVISION_CONFLICT: '账号已在别处被修改，请刷新后重试',
  STATE_CONFLICT: '状态已变化，请刷新后重试',
  UNSUPPORTED_VALUE: '该平台不支持此操作',
  OPERATION_ALREADY_RUNNING: '同类任务正在进行中',
}

export function translateOpError(op: Operation): string {
  const e = op.error as { code?: string; message?: string } | null
  if (e?.code && OP_ERRORS[e.code]) return OP_ERRORS[e.code]
  if (e?.message && OP_ERRORS[e.message]) return OP_ERRORS[e.message]
  return operationErrorText(op)
}

/**
 * Start a 202 Operation, follow it to the end and toast the outcome.
 * Returns the finished operation, or null when it could not start / follow.
 */
export async function runOperation(
  start: () => Promise<{ data: unknown }>,
  opts: { success?: string; failure?: string; onProgress?: (op: Operation) => void } = {},
): Promise<Operation | null> {
  let op: Operation
  try {
    const res = await start()
    if (!isOperation(res.data)) {
      if (opts.success) toast(opts.success, 'success')
      return null
    }
    op = res.data
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    return null
  }
  try {
    const done = await waitOperation(op, { onProgress: opts.onProgress, intervalMs: 1200 })
    if (done.status === 'succeeded') {
      if (opts.success) toast(opts.success, 'success')
    } else if (done.status === 'failed') {
      toast(opts.failure || '操作失败', 'error', translateOpError(done), 8000)
    } else {
      toast('任务已取消', 'warning')
    }
    return done
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    return null
  }
}

/** Fetch every page of a paged list endpoint whose data is `{ items, revision }`. */
export async function fetchAllPages<T>(path: string, pageSize = 200): Promise<{ items: T[]; revision: string }> {
  const items: T[] = []
  let revision = ''
  for (let page = 1; page < 100; page++) {
    const res = await api.get<{ items: T[]; revision: string }>(path, { page, pageSize })
    items.push(...res.data.items)
    revision = res.data.revision
    if (!res.meta.hasNext) break
  }
  return { items, revision }
}

/** First error field code of an ApiError (the backend puts machine reasons there). */
export function fieldCode(err: unknown): string {
  if (err instanceof ApiError) return err.fields?.[0]?.code || ''
  return ''
}

/** Human text for an error, preferring known OAuth field reasons. */
const FIELD_REASONS: Record<string, string> = {
  REFRESH_DISABLED: '当前实例禁止刷新令牌（PARROT_NO_REFRESH=1），未发出请求',
  UNSUPPORTED_REGION: '该区域不支持此操作',
  ACCOUNT_PAUSED: '账号已停用或认证失效，活动暂停',
  STATUS_UNKNOWN: '今日状态未知（不是未签到），请先查询核对',
  ACTIVITY_INACTIVE: '上游确认当前活动未开放',
  FREE_TRIAL_TERMS_CONFIRMATION_REQUIRED: '需要先确认免费试用条件',
  DISABLED: '当前环境禁止上游网络请求',
  BUSINESS_DATE_CHANGED: '日期已变化，请重新确认',
  STALE_GENERATION: '登录凭据已变化，请重新确认',
  AUTO_NOT_ENABLED: '未开启自动签到',
  ACCOUNT_MISSING: '账号已不存在',
  PREPARE_FIRST: '请先确认设备档案，再提交 PAT',
  INVALID: '内容无效',
  REJECTED: '上游拒绝了这个凭据',
  UNSUPPORTED: '该平台的设备协议尚未适配，档案未更改',
  EXACT_IDENTITY: '已存在相同身份的账号',
  INVALID_TARGET: '代理不存在或已被删除，请重新选择',
}

export function explainError(err: unknown): string | undefined {
  const code = fieldCode(err)
  if (code && FIELD_REASONS[code]) return FIELD_REASONS[code]
  const detail = errorDetail(err)
  // Generic English server messages add nothing to the Chinese title.
  return detail && /[\u4e00-\u9fff]/.test(detail) ? detail : undefined
}

/** One-line "title：reason" text for inline error areas. */
export function errorLine(err: unknown): string {
  const d = explainError(err)
  return d ? `${errorTitle(err)}：${d}` : errorTitle(err)
}

export function errorToast(err: unknown): void {
  toast(errorTitle(err), 'error', explainError(err), 7000)
}
