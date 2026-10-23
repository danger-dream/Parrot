import type { Schemas } from '@/api/client'

export type UpdateCheck = Schemas['UpdateCheckData']

/** `getUpdateState` (R2-11). Typed locally until api/schema.d.ts is regenerated. */
export interface UpdateState {
  stage: string
  mode: string
  fromVersion: string | null
  toVersion: string | null
  targetVersion: string | null
  message: string | null
  repository: string
  currentVersion: string
  busy: boolean
  activationAvailable: boolean
  revision: string
}

/** `createUpdateActivationPlan` (R2-11). */
export interface ActivationPlan {
  planToken: string
  targetVersion: string
  revision: string
  expiresAt: string | null
}

export const MODE_LABEL: Record<string, string> = {
  docker: 'Docker 容器',
  systemd: 'systemd 服务',
  bare: '源码运行',
  src: '源码目录',
  git: 'Git 源码',
}

export const STAGE_LABEL: Record<string, { label: string; tone: 'neutral' | 'blue' | 'green' | 'orange' | 'red' }> = {
  idle: { label: '空闲', tone: 'neutral' },
  backing_up: { label: '正在备份', tone: 'blue' },
  pulling: { label: '正在下载新版本', tone: 'blue' },
  staged: { label: '已暂存，等待确认重启', tone: 'orange' },
  restarting: { label: '正在重启', tone: 'blue' },
  verifying: { label: '正在健康检查', tone: 'blue' },
  success: { label: '上次更新成功', tone: 'green' },
  failed: { label: '上次更新失败', tone: 'red' },
  rolled_back: { label: '上次更新已回滚', tone: 'orange' },
}

/** Stages in which the updater is still working and the state should be polled. */
export const TRANSIENT_STAGES = new Set(['backing_up', 'pulling', 'restarting', 'verifying'])

export function normVersion(v: string | null | undefined): string {
  return String(v || '').replace(/^v/i, '')
}

export function showVersion(v: string | null | undefined): string {
  const n = normVersion(v)
  return n ? `v${n}` : '—'
}
