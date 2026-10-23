/**
 * What TG shows per account (oauth_menu._format_account_block / _detail_text_and_kb and the
 * WorkBuddy / 智谱 / Qoder menus), read from the server-built `card` (list and detail) plus the
 * detail-only fields. Nothing here derives business facts the server already computes.
 */
import { dateTime } from '@/utils/format'
import {
  amount,
  num,
  pick,
  planLabel,
  type AccountCardData,
  type AccountDetail,
  type AccountSummary,
  type PlanFacts,
  type RuntimeErr,
  type UsageItem,
  type UsageSection,
} from './meta'

type Rec = Record<string, unknown>
export type FactTone = 'green' | 'orange' | 'red' | 'blue' | 'neutral'

export interface Fact {
  label: string
  value: string
  tone?: FactTone
  mono?: boolean
}
export interface Alert {
  text: string
  tone: 'red' | 'orange' | 'blue'
  kind?: 'restriction' | 'relogin'
}

function rec(v: unknown): Rec {
  return v && typeof v === 'object' ? (v as Rec) : {}
}

const WB_TYPE: Record<string, string> = { personal: '个人', pro: '个人 Pro', ultimate: '企业版', exclusive: '专享版', premise: '私有部署' }
const ZHIPU_ENTITLEMENT: Record<string, string> = { available: '可用', unknown: '未知', unassigned: '未分配席位', expired: '已过期', unavailable: '无有效套餐' }
const CLAUDE_SUB: Record<string, string> = { active: '有效', trialing: '试用中', past_due: '已逾期', canceled: '已取消', cancelled: '已取消', unpaid: '未付款' }
const BILLING: Record<string, string> = { stripe_subscription: 'Stripe 订阅', apple_subscription: 'Apple 订阅', google_play_subscription: 'Google Play 订阅' }

/** The card of a list item / of a detail's account. */
export function cardOf(a: AccountSummary | undefined | null): AccountCardData | null {
  return a?.card || null
}

function qoderPlanItem(sections: UsageSection[] | undefined): UsageItem | undefined {
  return sections?.find((s) => s.id === 'qoderCredits')?.items.find((i) => i.label === '套餐')
}

/** Short plan text for the subtitle (TG 🏷️ 套餐). */
export function planText(a: AccountSummary, plan: PlanFacts | null | undefined, sections?: UsageSection[], planType?: string | null): string {
  const p = plan || {}
  if (a.provider === 'xai' || a.provider === 'antigravity') return p.tier || p.label || ''
  if (a.provider === 'qoder') return qoderPlanItem(sections)?.note || p.label || ''
  if (a.provider === 'workbuddy') return p.label || WB_TYPE[p.accountType || ''] || ''
  return p.label || planLabel(planType)
}

function sectionItem(sections: UsageSection[] | undefined, id: string, label: string): UsageItem | undefined {
  return sections?.find((s) => s.id === id)?.items.find((i) => i.label === label)
}

/** Key facts TG prints on the plan line(s); `detail` adds the detail-page-only lines. */
export function planFacts(
  a: AccountSummary,
  plan: PlanFacts | null | undefined,
  sections: UsageSection[] | undefined,
  opts: { detail?: AccountDetail | null } = {},
): Fact[] {
  const out: Fact[] = []
  const p = plan || {}
  const d = opts.detail
  const enabled = a.modelCount - a.disabledModelCount
  switch (a.provider) {
    case 'openai': {
      if (p.workspaceLabel || (d && d.workspaceName)) out.push({ label: '工作区', value: p.workspaceLabel || d?.workspaceName || '' })
      const count = p.resetCreditCount ?? num(sectionItem(sections, 'credits', '官方重置卡')?.remaining)
      if (count !== null && count !== undefined) out.push({ label: '官方重置', value: `${count} 次`, tone: count > 0 ? 'green' : 'neutral' })
      const credits = sectionItem(sections, 'credits', 'Credits')
      if (credits) {
        const r = num(credits.remaining)
        let value = r !== null ? amount(r) : credits.note || '—'
        if (r !== null && credits.note) value += ` · ${credits.note}`
        if (p.credits?.stale) value += ' · 刷新失败，旧数据'
        out.push({ label: '积分', value, tone: p.credits?.stale ? 'orange' : 'blue' })
        if (d && p.credits?.observedAt) {
          out.push({ label: '积分观测于', value: `${dateTime(p.credits.observedAt)} · ${p.credits.source === 'official_usage' ? '官方用量接口' : '上游响应采样'}` })
        }
      }
      if (p.expiresAt) out.push({ label: '套餐到期', value: dateTime(p.expiresAt) })
      break
    }
    case 'claude':
      if (p.status || p.billingType) out.push({ label: '订阅', value: [CLAUDE_SUB[p.status || ''] || p.status, BILLING[p.billingType || ''] || p.billingType].filter(Boolean).join(' · '), tone: p.status === 'active' ? 'green' : p.status ? 'orange' : undefined })
      if (d && p.startedAt) out.push({ label: '订阅开始', value: dateTime(p.startedAt) })
      break
    case 'xai':
      if (p.access?.state) out.push({ label: '访问', value: p.access.state === 'ok' ? '正常' : p.access.state === 'limited' ? '访问受限' : '账号受限', tone: p.access.state === 'ok' ? 'green' : 'red' })
      if (p.access?.grokCode !== undefined && p.access?.grokCode !== null) out.push({ label: 'Grok Code', value: p.access.grokCode ? '可用' : '不可用', tone: p.access.grokCode ? 'green' : 'orange' })
      break
    case 'cursor':
      out.push({ label: '模型', value: `${d ? `${enabled} 已启用` : a.modelCount}${p.variantCount ? ` · 变体 ${p.variantCount}` : ''}` })
      if (d?.workspaceName) out.push({ label: '姓名', value: d.workspaceName })
      break
    case 'antigravity': {
      const text = p.textModelCount ?? a.modelCount
      out.push({ label: '模型', value: `文本 ${text}${p.imageModelCount ? ` · 出图 ${p.imageModelCount}` : ''}` })
      if (d?.workspaceId) out.push({ label: 'Project', value: d.workspaceId, mono: true })
      break
    }
    case 'zhipu': {
      if (p.site) out.push({ label: '站点', value: p.site === 'zai' ? 'Z.ai 国际站' : 'BigModel 中国站' })
      const oauth = p.credentialMode === 'oauth'
      if (p.credentialMode) out.push({ label: '凭据', value: oauth ? 'OAuth' : 'API Key' })
      if (oauth && p.entitlement && (d || p.entitlement !== 'available')) out.push({ label: '订阅', value: ZHIPU_ENTITLEMENT[p.entitlement] || '未知', tone: p.entitlement === 'available' ? 'green' : 'orange' })
      const cards = d ? zhipuCards(rec(d.zhipu)) : null
      if (oauth && cards) out.push({ label: '重置卡', value: `5 小时 ${cards.five} 张 · 周 ${cards.week} 张`, tone: cards.five + cards.week ? 'green' : 'neutral' })
      if (d && (p.organizationId || p.projectId)) out.push({ label: '组织 / 项目', value: `${p.organizationId || '—'} / ${p.projectId || '—'}`, mono: true })
      if (d) {
        const g = p.globalDisabledModelCount ?? 0
        out.push({ label: '模型目录', value: `${a.modelCount} 个 · 启用 ${Math.max(0, a.modelCount - a.disabledModelCount - g)} · 禁用 ${a.disabledModelCount + g}（全局 ${g} · 账号 ${a.disabledModelCount}）` })
      }
      break
    }
    case 'workbuddy':
      if (p.region) out.push({ label: '区域', value: p.region === 'cn' ? '中国区' : '国际区' })
      out.push({ label: '模型', value: d ? `${enabled} 可用 · 禁用 ${a.disabledModelCount}` : `${a.modelCount}` })
      if (d && p.accountType) out.push({ label: '官方账户类型', value: p.accountType, mono: true })
      break
    case 'qoder': {
      if (p.region) out.push({ label: '路线', value: p.region === 'cn' ? 'Qoder 中国 · qoder.cn' : 'Qoder 国际 · qoder.com' })
      if (p.authMethod) out.push({ label: '接入', value: (p.authMethod === 'pat' ? 'PAT' : '设备授权') + (p.deviceChat ? ' · 设备 Token 对话' : '') })
      const pl = qoderPlanItem(sections)
      if (pl?.resetsAt && !pl.resetsAt.startsWith('9999')) out.push({ label: '套餐到期', value: dateTime(pl.resetsAt) })
      if (d && pick(rec(d.qoder), 'autoCheckin') === true) out.push({ label: '自动签到', value: '已开', tone: 'green' })
      break
    }
  }
  return out
}

function zhipuCards(z: Rec): { five: number; week: number } | null {
  const r = rec(pick(z, 'reset'))
  const data = rec(pick(r, 'data') ?? r)
  const five = pick<unknown[]>(data, 'availableFiveHourResets')
  const week = pick<unknown[]>(data, 'availableWeekResets')
  if (!Array.isArray(five) && !Array.isArray(week)) return null
  return { five: Array.isArray(five) ? five.length : 0, week: Array.isArray(week) ? week.length : 0 }
}

/** TG status tag + 冷却行 + platform warnings. */
export function accountAlerts(a: AccountSummary, card: AccountCardData | null, opts: { detail?: AccountDetail | null } = {}): Alert[] {
  const out: Alert[] = []
  const tag = card?.statusTag
  if (tag === 'authFailed') out.push({ text: '认证失效，需要重新登录', tone: 'red', kind: 'relogin' })
  else if (tag === 'quotaDisabled') {
    const until = a.disabledUntil
    out.push({ text: until ? `额度暂停，预计 ${dateTime(until)} 恢复` : '额度用尽，自动暂停', tone: 'orange' })
  } else if (tag === 'providerRestricted' || card?.cooldown.accountPaused) {
    out.push({ text: '供应商限制账户：全部模型暂停，请在官方客户端申诉', tone: 'red', kind: 'restriction' })
  }
  const c = card?.cooldown
  if (c?.quotaPausedModels) out.push({ text: `额度暂停 ${c.quotaPausedModels} 个已启用模型（不是故障）`, tone: 'orange' })
  if (c && (c.permanentModels || c.coolingModels)) {
    const parts = [c.permanentModels ? `永久冻结 ${c.permanentModels} 个模型` : '', c.coolingModels ? `冷却 ${c.coolingModels} 个模型` : ''].filter(Boolean)
    out.push({ text: parts.join(' · '), tone: c.permanentModels ? 'red' : 'orange' })
  }
  const p = card?.plan
  if (a.provider === 'zhipu' && p) {
    if (p.credentialMode === 'oauth' && p.managementStatus === 'relogin_required') out.push({ text: '管理登录已过期，请重新登录', tone: 'red', kind: 'relogin' })
    if (p.credentialMode === 'oauth' && p.setupIncomplete) out.push({ text: '初始化未完成，可继续自动配置模型 Key', tone: 'orange' })
    if (p.managementStatus === 'model_key_rejected') out.push({ text: '模型 Key 认证失败，请更新凭据', tone: 'red' })
  }
  const d = opts.detail
  if (a.provider === 'qoder' && d && pick(rec(d.qoder), 'reloginRequired') === true) {
    out.push({ text: '刷新结果未确认，需要重新授权（不代表已吊销）', tone: 'orange', kind: 'relogin' })
  }
  return out
}

/** Usage sections; the detail falls back to the legacy windows when no section is cached. */
export function allSections(sections: UsageSection[] | null | undefined, windows?: AccountDetail['usageWindows']): UsageSection[] {
  if (sections?.length) return sections
  const items: UsageItem[] = (windows || [])
    .filter((w) => num(w.usedPercent) !== null)
    .map((w) => ({ label: ({ fiveHour: '5 小时', sevenDay: '7 天', thirtyDay: '30 天' } as Record<string, string>)[w.name] || w.name, usedPercent: w.usedPercent, resetsAt: w.resetsAt }))
  return items.length ? [{ id: 'windows', title: '用量窗口', items }] : []
}

/** Cursor 配额暂停 grouped by pool (TG _format_cursor_quota_pauses). */
export const POOL_LABEL: Record<string, string> = { cursor_models: 'Cursor Models / Auto', other_models: 'Other Models / API', unknown: '未知模型池' }
export function quotaPauseGroups(errs: RuntimeErr[]): { pool: string; count: number; until: string | null }[] {
  const groups = new Map<string, { count: number; until: string | null }>()
  for (const e of errs) {
    if (e.kind !== 'quotaPause') continue
    const key = e.pool || 'unknown'
    const g = groups.get(key) || { count: 0, until: null }
    g.count++
    if (e.cooldownUntil && (!g.until || e.cooldownUntil > g.until)) g.until = e.cooldownUntil
    groups.set(key, g)
  }
  return [...groups.entries()].map(([pool, g]) => ({ pool, ...g }))
}
