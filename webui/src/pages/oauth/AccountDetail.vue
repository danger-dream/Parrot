<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Check, Eraser, KeyRound, Link2Off, ListFilter, LogIn, Pencil, RefreshCw, RotateCcw, Sparkles, Trash2, TriangleAlert, X } from '@lucide/vue'
import { api, ApiError, type Schemas } from '@/api/client'
import { goLogs, openModel } from '@/links'
import { confirmAction } from '@/stores/dialogs'
import { closeInspector, inspector, toast } from '@/stores/ui'
import { compact, dateTime, dollars, integer, pctValue, relative, usd } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import KeyValue from '@/components/ui/KeyValue.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import TextInput from '@/components/ui/TextInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import DetailSection from '@/components/DetailSection.vue'
import AccountModels from './AccountModels.vue'
import AddAccountModal from './AddAccountModal.vue'
import ClaudeResetModal from './ClaudeResetModal.vue'
import OpenAiCreditsPanel from './OpenAiCreditsPanel.vue'
import UsageView from './UsageView.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import QoderPanel from './QoderPanel.vue'
import SettingRow from '@/components/SettingRow.vue'
import SubBlock from '@/components/SubBlock.vue'
import UsageTiles, { type UsageTile } from '@/components/UsageTiles.vue'
import { deltas, loadCompare, minuteNow, type Compare } from '@/utils/compare'
import WorkBuddyPanel from './WorkBuddyPanel.vue'
import ZhipuPanel from './ZhipuPanel.vue'
import { accountPath, errorToast, notifyChanged, oauthBus, runOperation } from './oauthApi'
import { accountState, countdown, pick, providerMeta, reasonLabel, usageValue, type AccountDetail, type Provider, type QuotaResetPlan, type RuntimeErr, type UsageItem } from './meta'
import { accountAlerts, allSections, planFacts, planText, POOL_LABEL, quotaPauseGroups } from './facts'
import { cacheShare, loadAccountModelStats, MODEL_PERIODS, promptTokens, tps, type ModelRow, type StatsPeriod } from './channelStats'
import { errorTitle } from '@/utils/errors'

type ResetResult = Schemas['OAuthQuotaResetData']
type Mutation = Schemas['OAuthMutationData']

const props = defineProps<{ id: string }>()

const data = ref<AccountDetail | null>(null)
const error = ref('')
const notFound = ref(false)
const busy = ref('')
const usageNote = ref('')
const now = ref(Date.now())
const relogin = ref<{ accountId: string; provider: Provider; realm?: string | null; site?: string | null } | null>(null)
const claudeResetOpen = ref(false)
/** Provider panels render their parts into this page's sections through `#<scope>-<part>` slots. */
const scope = `acct-${Math.random().toString(36).slice(2, 10)}`
/** Bumped after 刷新额度 so the provider panels reload their own snapshots too. */
const panelTick = ref(0)
let ticker: number | undefined

async function load(): Promise<void> {
  try {
    data.value = (await api.get<AccountDetail>(accountPath(props.id))).data
    error.value = ''
    loadCompareStats()
    notFound.value = false
    if (inspector.key === `account:${props.id}`) inspector.title = data.value.account.displayName
  } catch (err) {
    notFound.value = err instanceof ApiError && err.code === 'RESOURCE_NOT_FOUND'
    error.value = notFound.value ? '找不到这个账号，可能已被删除。' : errorTitle(err)
  }
}
/** TG 详情「按模型」: getStatsBreakdown(dimension=model, channel=oauth:<id>). */
const modelPeriod = ref<StatsPeriod>('month')
const modelRows = ref<ModelRow[] | null>(null)
const modelError = ref('')
async function loadStats(): Promise<void> {
  try {
    modelRows.value = await loadAccountModelStats(props.id, modelPeriod.value)
    modelError.value = ''
  } catch (err) {
    modelRows.value = []
    modelError.value = errorTitle(err)
  }
}
watch(modelPeriod, loadStats)
onMounted(() => {
  load()
  loadStats()
  ticker = window.setInterval(() => (now.value = Date.now()), 30_000)
})
onBeforeUnmount(() => window.clearInterval(ticker))
watch(() => props.id, () => (load(), loadStats()))
watch(
  () => oauthBus.version,
  () => {
    if (!oauthBus.changed || oauthBus.changed === props.id) load()
  },
)

const acc = computed(() => data.value?.account)
const state = computed(() => (acc.value ? accountState(acc.value) : null))
const provider = computed(() => acc.value?.provider || '')
const sections = computed(() => allSections(data.value?.usageSections, data.value?.usageWindows))
/** 概要 keeps subscription facts; quota numbers, IDs and model counts live in their own sections below. */
const ELSEWHERE = new Set(['积分', '积分观测于', 'Credits', 'Credits 观测于', '官方重置', '重置卡', '组织 / 项目', 'Project', '模型', '模型目录', '凭据', '站点', '区域', '官方账户类型', '接入', '自动签到'])
const facts = computed(() =>
  acc.value ? planFacts(acc.value, data.value?.plan, data.value?.usageSections, { detail: data.value }).filter((f) => !ELSEWHERE.has(f.label)) : [],
)
/** Claude's official reset is one block with its action; OpenAI's reset cards come from their own block. */
const claudeReset = computed(() => sections.value.find((x) => x.id === 'claudeReset') || null)
const quotaSections = computed(() =>
  sections.value
    .filter((x) => x.id !== 'claudeReset')
    .map((x) => (provider.value === 'openai' && x.id === 'credits' ? { ...x, items: x.items.filter((i) => i.label !== '官方重置卡') } : x))
    .filter((x) => x.items.length),
)
function resetLine(it: UsageItem): string {
  return [usageValue(it), it.note, it.resetsAt ? `${relative(it.resetsAt, now.value)}可用` : ''].filter(Boolean).join(' · ') || '—'
}
const plan = computed(() => (acc.value ? planText(acc.value, data.value?.plan, data.value?.usageSections, data.value?.planType) : ''))
/** The disabled-reason banner is rendered separately; keep cooldown / restriction / platform alerts. */
const alerts = computed(() => (acc.value ? accountAlerts(acc.value, acc.value.card || null, { detail: data.value }).filter((al) => !al.text.startsWith('认证失效') && !al.text.startsWith('额度暂停，') && !al.text.startsWith('额度用尽')) : []))
const errs = computed(() => data.value?.runtimeErrors || [])
/** TG「⚠ 冷却中的模型」: real faults only; quota pauses are listed per pool, hidden ones (disabled models) skipped. */
const cooling = computed(() => errs.value.filter((e) => e.modelId && e.kind === 'fault'))
const pauseGroups = computed(() => quotaPauseGroups(errs.value))
const restrictionEntry = computed(() => errs.value.find((e) => e.kind === 'accountRestriction') || null)
const stats = computed(() => data.value?.localStats || null)
const periodLabel = computed(() => (stats.value?.since ? `自 ${dateTime(stats.value.since)} 起` : ''))
/** This period against the same length of time right before it. */
const compare = ref<Compare | null>(null)
async function loadCompareStats(): Promise<void> {
  const since = data.value?.localStats?.since
  if (!since) {
    compare.value = null
    return
  }
  compare.value = await loadCompare({ from: since, to: minuteNow(), channel: `oauth:${props.id}` }).catch(() => compare.value)
}
const change = computed(() => deltas(compare.value))
const tiles = computed<UsageTile[]>(() => {
  const st = stats.value
  if (!st || !st.total) return []
  const share = cacheShare(st)
  const d = change.value
  const out: UsageTile[] = [
    { k: '读入 ↑（含缓存）', v: compact(promptTokens(st)), delta: d.prompt },
    { k: '写出 ↓', v: compact(st.outputTokens), delta: d.output },
    {
      k: '金额',
      delta: d.cost,
      v: st.costTicks || !st.unpricedSuccess ? usd(st.costTicks ?? 0) : '未计价',
      s: st.unpricedSuccess ? `未计价调用 ${integer(st.unpricedSuccess)} 次` : '',
      t: '按上游调用计：一次请求的重试或多轮工具调用会产生多次上游调用，所以可能多于请求数',
    },
    { k: '缓存读取', v: compact(st.cacheReadTokens ?? 0), s: share !== null ? `占读入 ${pctValue(share)}` : '', delta: d.cacheRead },
    { k: '缓存写入', v: compact(st.cacheCreationTokens ?? 0), delta: d.cacheWrite },
    { k: '平均速度', v: tps(st.averageTokensPerSecond), s: st.maximumTokensPerSecond ? `峰值 ${tps(st.maximumTokensPerSecond)} · 最低 ${tps(st.minimumTokensPerSecond)}` : '' },
    { k: '请求', v: integer(st.total), s: st.errorCount ? `失败 ${integer(st.errorCount)}` : '全部成功', bad: Boolean(st.errorCount), delta: d.requests },
  ]
  const p = data.value?.weeklyProjection
  if (p) out.push({ k: '7 天额度预测', v: `${compact(p.tokens)} tokens`, s: p.costTicks ? usd(p.costTicks) : '' })
  return out
})
/** Same predicate as the server's relogin gate (account_authentication_failed); Qoder may always re-authorise. */
const needsLogin = computed(() => {
  const a = acc.value
  if (!a) return false
  return (
    a.invalid ||
    a.disabledReason === 'auth_error' ||
    (a.provider === 'zhipu' && pick(data.value?.zhipu, 'managementStatus') === 'relogin_required') ||
    (a.provider === 'qoder' && pick(data.value?.qoder, 'reloginRequired') === true)
  )
})
function startRelogin(realm?: string | null): void {
  const a = acc.value
  if (!a) return
  const snap = (a.provider === 'workbuddy' ? data.value?.workbuddy : a.provider === 'qoder' ? data.value?.qoder : a.provider === 'zhipu' ? data.value?.zhipu : null) as Record<string, unknown> | null
  relogin.value = {
    accountId: props.id,
    provider: a.provider,
    realm: realm ?? ((pick<string>(snap, 'realm') as string | undefined) || null),
    site: (pick<string>(snap, 'site') as string | undefined) || null,
  }
}
const longLived = (v: string | null | undefined) => Boolean(v && new Date(v).getFullYear() >= 2090)

const fields = computed(() => {
  const d = data.value
  if (!d) return []
  const a = d.account
  const credits = d.plan?.credits
  // Only what the account ID does not already spell out; Zhipu lists organisation / project with its own
  // facts and Cursor's "workspace" is the 概要 name plus the user ID again.
  const workspace =
    a.provider === 'zhipu' || a.provider === 'cursor'
      ? []
      : [d.workspaceName, d.workspaceId].filter((v): v is string => Boolean(v) && !a.accountId.includes(v as string))
  return [
    { label: '身份', value: a.identity, mono: true, full: true, private: true },
    { label: '账号 ID', value: a.accountId, mono: true, full: true, private: true },
    ...(workspace.length ? [{ label: '工作区 / 项目', value: workspace.join(' · '), mono: !d.workspaceName, full: true, private: true }] : []),
    { label: '令牌到期', value: d.expiresAt ? (longLived(d.expiresAt) ? '长期有效（自动续期）' : `${dateTime(d.expiresAt)}（${relative(d.expiresAt, now.value)}）`) : null },
    { label: '令牌刷新', value: d.lastRefreshAt ? `${dateTime(d.lastRefreshAt)}（${relative(d.lastRefreshAt, now.value)}）` : null },
    { label: '凭据', value: d.credentialConfigured ? '已配置' : '缺失', tone: d.credentialConfigured ? undefined : ('red' as const) },
    ...(credits?.observedAt ? [{ label: '积分数据', value: `${dateTime(credits.observedAt)} · ${credits.source === 'official_usage' ? '官方用量接口' : '上游响应采样'}${credits.stale ? '（可能已过期）' : ''}` }] : []),
  ]
})

async function patch(body: Record<string, unknown>, success: string): Promise<boolean> {
  if (!acc.value) return false
  try {
    data.value = (await api.patch<AccountDetail>(accountPath(props.id), body, { ifMatch: acc.value.revision })).data
    toast(success, 'success')
    notifyChanged(props.id)
    return true
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') {
      toast('账号已在别处被修改', 'warning', '已载入最新数据，请再试一次。')
      await load()
    } else errorToast(err)
    return false
  }
}

async function setEnabled(on: boolean): Promise<void> {
  busy.value = 'enabled'
  await patch({ enabled: on }, on ? '账号已启用' : '账号已停用')
  busy.value = ''
}

async function setAllowCredits(on: boolean): Promise<void> {
  busy.value = 'credits'
  await patch(
    { allowCredits: on },
    on ? '已允许使用积分；下次刷新用量后才会解除额度暂停' : '已禁止使用积分，已按当前额度重新检查',
  )
  busy.value = ''
}

// ---- inline edits (no dialogs) ----
const ccDraft = ref<number | null>(0)
watch(
  () => acc.value?.maxConcurrent,
  (v) => (ccDraft.value = v ?? 0),
  { immediate: true },
)
const ccDirty = computed(() => acc.value !== undefined && ccDraft.value !== null && ccDraft.value !== acc.value.maxConcurrent)
async function saveConcurrency(): Promise<void> {
  if (!ccDirty.value || ccDraft.value === null) return
  busy.value = 'concurrency'
  const v = ccDraft.value
  if (!(await patch({ maxConcurrent: v }, v ? `最大并发已设为 ${v}` : '已改为不限并发'))) ccDraft.value = acc.value?.maxConcurrent ?? 0
  busy.value = ''
}

const nameDraft = ref<string | null>(null)
const nameError = computed(() => {
  const v = (nameDraft.value || '').trim()
  return !v ? '名称不能为空' : v.length > 200 ? '最多 200 个字' : ''
})
async function saveName(): Promise<void> {
  if (nameDraft.value === null || nameError.value) return
  const v = nameDraft.value.trim()
  if (v === acc.value?.displayName) return void (nameDraft.value = null)
  busy.value = 'name'
  if (await patch({ displayName: v }, '名称已更新')) nameDraft.value = null
  busy.value = ''
}

async function refreshToken(): Promise<void> {
  busy.value = 'token'
  try {
    const r = (await api.post<Mutation>(accountPath(props.id, '/actions/refresh-token'))).data
    toast(r.status === 'model_key_resolved' ? '已重新解析模型 Key' : '令牌已刷新', 'success')
    notifyChanged(props.id)
  } catch (err) {
    errorToast(err)
  } finally {
    busy.value = ''
  }
}

/** The single 刷新额度: WorkBuddy's status refresh also reconciles pending check-ins; Qoder's answers in place. */
async function refreshUsage(): Promise<void> {
  busy.value = 'usage'
  const started = Date.now()
  usageNote.value = '正在刷新额度…'
  let ok = false
  if (provider.value === 'qoder') {
    try {
      await api.post(accountPath(props.id, '/qoder/actions/refresh-status'))
      toast('额度已刷新', 'success')
      ok = true
    } catch (err) {
      errorToast(err)
    }
  } else {
    const path = provider.value === 'workbuddy' ? '/workbuddy/actions/refresh-status' : '/actions/refresh-usage'
    ok = Boolean(
      await runOperation(() => api.post(accountPath(props.id, path)), {
        success: '额度已刷新',
        failure: '额度刷新失败，保留上次数据',
        onProgress: () => (usageNote.value = `正在刷新额度… ${Math.round((Date.now() - started) / 1000)} 秒`),
      }),
    )
  }
  usageNote.value = ''
  busy.value = ''
  if (ok) {
    panelTick.value++
    notifyChanged(props.id)
  }
}

async function simple(path: string, key: string, success: string): Promise<void> {
  busy.value = key
  try {
    await api.post(accountPath(props.id, path))
    toast(success, 'success')
    notifyChanged(props.id)
  } catch (err) {
    errorToast(err)
  } finally {
    busy.value = ''
  }
}

const OUTCOME: Record<string, string> = {
  reset: '官方已重置额度窗口',
  alreadyRedeemed: '这次重置此前已经执行过（未重复消耗）',
  noCredit: '当前没有可用的官方重置次数',
  nothingToReset: '当前没有符合条件、需要重置的额度窗口',
}
const LOCAL: Record<string, string> = {
  resumed: '已刷新最新额度并确认低于阈值，账号已自动恢复',
  kept_enabled: '已刷新最新额度，账号保持可用',
  still_over_quota: '最新额度仍超限，本地限制已保留',
  refresh_failed_keep_disabled: '最新额度刷新失败，未自动解除本地限制，请稍后刷新用量',
  quota_unknown_keep_disabled: '最新额度没有有效数据，未自动解除本地限制',
  state_update_failed_keep_disabled: '本地额度状态更新失败，未自动解除限制',
  runtime_state_clear_failed: '本地冷却状态未能完全清除，账号保持限制',
  reset: '已清除本地额度暂停和模型冷却，账号已启用',
  already_enabled: '账号本来就可用，已清理本地冷却',
  cleared_runtime_state: '已清理本地额度 / 冷却状态',
  reset_failed: '重置失败，账号保持停用',
  noop_user: '账号是手动停用的，不会自动恢复',
  noop_auth_error: '账号认证已失效，需要重新登录',
  noop_missing: '账号已不存在',
  state_conflict: '账号状态已变化，未自动启用',
  invalid_state: '账号状态无效，未自动启用',
  not_openai: '仅 OpenAI 有官方重置次数',
}

/** OpenAI official reset (optionally with a chosen card) or Claude's local quota-pause clearing. */
async function quotaReset(creditId: string | null = null, cardLabel = ''): Promise<void> {
  if (!acc.value) return
  busy.value = 'reset'
  try {
    let plan: QuotaResetPlan
    try {
      plan = (await api.post<QuotaResetPlan>(accountPath(props.id, '/actions/reset-quota-plan'), creditId ? { creditId } : {})).data
    } catch (err) {
      errorToast(err)
      return
    }
    const openai = plan.provider === 'openai'
    const ok = await confirmAction(
      openai
        ? {
            title: '消耗 1 次 OpenAI 官方重置？',
            detail: [
              `当前可用官方重置次数：${plan.creditCount ?? '未知'}`,
              `使用的卡：${cardLabel || (plan.creditId ? plan.creditId : '由 OpenAI 自动选择的默认卡')}`,
              '',
              '• 这是 OpenAI / Codex 官方的重置次数，不是 Parrot 本地清缓存；执行后立刻消耗 1 次，不可撤销。',
              '• 如果 OpenAI 判断当前没有可重置的窗口，可能返回「无可用次数」或「无需重置」。',
              '• 成功后会立即刷新额度；只有最新额度确认低于阈值才会解除暂停。',
              '• 不会解除手动停用或认证失效。',
            ].join('\n'),
            confirmText: '确认消耗 1 次',
            danger: true,
            requireText: '重置',
          }
        : {
            title: '清除本地额度暂停？',
            detail: '清除这个账号的额度暂停、模型冷却和本地额度缓存，下一次请求或刷新会重新采样。\n不会请求上游，也不会解除手动停用或认证失效。',
            confirmText: '清除',
            danger: true,
          },
    )
    if (!ok) return
    try {
      const r = (await api.post<ResetResult>(accountPath(props.id, '/actions/reset-quota'), { planToken: plan.planToken })).data
      const lines = [r.upstreamOutcome ? OUTCOME[r.upstreamOutcome] || r.upstreamOutcome : '', r.localAction ? LOCAL[r.localAction] || r.localAction : ''].filter(Boolean)
      const good = r.status === 'reset' || r.localAction === 'resumed' || r.localAction === 'kept_enabled' || r.localAction === 'reset'
      toast(openai ? (r.upstreamOutcome ? 'OpenAI 官方重置已执行' : '没有执行官方重置（未消耗次数）') : '本地额度状态已处理', good ? 'success' : 'warning', lines.join('；') || undefined, 9000)
    } catch (err) {
      errorToast(err)
    }
    notifyChanged(props.id)
  } finally {
    busy.value = ''
  }
}

async function remove(): Promise<void> {
  if (!acc.value) return
  const ok = await confirmAction({
    title: `删除账号「${acc.value.displayName}」？`,
    detail: `${providerMeta(acc.value.provider).label} · ${acc.value.identity}\n会清除这个账号的统计与亲和绑定数据，并从调度顺序中移除。此操作无法撤销。`,
    confirmText: '删除账号',
    danger: true,
  })
  if (!ok) return
  busy.value = 'delete'
  try {
    await api.del(accountPath(props.id), { ifMatch: acc.value.revision })
    toast('账号已删除', 'success')
    notifyChanged()
    closeInspector()
  } catch (err) {
    errorToast(err)
    await load()
  } finally {
    busy.value = ''
  }
}

function onPanelChanged(): void {
  load()
}

function cooldownText(e: RuntimeErr): string {
  if (e.cooldownPermanent) return '永久冻结'
  if (!e.cooldownUntil) return '冷却中'
  const t = new Date(e.cooldownUntil).getTime()
  return t > now.value ? countdown(e.cooldownUntil, now.value).replace('后重置', '后解除') : '已到期'
}
</script>

<template>
  <div class="d">
    <div v-if="error" class="err">
      <TriangleAlert /> {{ error }}
      <Btn v-if="!notFound" size="sm" @click="load">重试</Btn>
    </div>
    <template v-else-if="data && acc && state">
      <!-- 1. who / state -->
      <section class="hero">
        <ProviderIcon :provider="acc.provider" :size="40" />
        <div class="who">
          <form v-if="nameDraft !== null" class="name-edit" @submit.prevent="saveName" @keydown.esc.stop="nameDraft = null">
            <TextInput v-model="nameDraft" autofocus :invalid="Boolean(nameError)" @enter="saveName" />
            <Btn type="submit" icon size="sm" variant="primary" :loading="busy === 'name'" :disabled="Boolean(nameError)" title="保存"><Check /></Btn>
            <Btn icon size="sm" variant="ghost" title="取消" @click="nameDraft = null"><X /></Btn>
          </form>
          <p v-else class="name">
            <span>{{ acc.displayName }}</span>
            <button type="button" class="edit" title="修改名称（只改显示名称）" @click="nameDraft = acc.displayName"><Pencil /></button>
          </p>
          <p class="sub">{{ providerMeta(acc.provider).label }}<template v-if="plan"> · <b>{{ plan }}</b></template><template v-if="acc.identity !== acc.displayName"> · {{ acc.identity }}</template></p>
        </div>
        <Badge :tone="state.tone" dot>{{ state.label }}</Badge>
      </section>

      <!-- 2. what needs attention -->
      <div v-if="(acc.disabledReason && acc.disabledReason !== 'user') || alerts.length || needsLogin" class="banners">
        <p v-if="acc.disabledReason && acc.disabledReason !== 'user' && acc.disabledReason !== '供应商限制'" class="banner" :class="acc.disabledReason === 'auth_error' ? 'red' : 'orange'">
          <TriangleAlert />
          <span>{{ reasonLabel(acc.disabledReason) }}<template v-if="acc.disabledUntil">，预计 {{ dateTime(acc.disabledUntil) }} 恢复</template></span>
          <Btn v-if="needsLogin" size="sm" variant="primary" @click="startRelogin()"><LogIn /> 重新登录</Btn>
          <Btn v-else-if="acc.disabledReason === 'quota' && provider === 'claude'" size="sm" :loading="busy === 'reset'" @click="quotaReset()"><RotateCcw /> 清除本地额度暂停</Btn>
        </p>
        <p v-for="(al, i) in alerts" :key="i" class="banner" :class="al.tone">
          <TriangleAlert />
          <span>{{ al.text }}<template v-if="al.kind === 'restriction' && restrictionEntry?.cooldownUntil">（本地退避至 {{ dateTime(restrictionEntry.cooldownUntil) }}，不是官方解封时间）</template></span>
          <Btn v-if="al.kind === 'restriction'" size="sm" :loading="busy === 'errors'" @click="simple('/actions/clear-errors', 'errors', '已清除本地暂停（不代表供应商已解除限制）')">限制已解除，清除暂停</Btn>
        </p>
        <p v-if="needsLogin && !(acc.disabledReason && acc.disabledReason !== 'user')" class="banner red">
          <TriangleAlert /><span>需要重新登录才能继续使用</span>
          <Btn size="sm" variant="primary" @click="startRelogin()"><LogIn /> 重新登录</Btn>
        </p>
      </div>

      <!-- 3. 概要: subscription facts only -->
      <dl v-if="facts.length" class="facts">
        <div v-for="f in facts" :key="f.label" class="fact">
          <dt>{{ f.label }}</dt>
          <dd :class="[f.tone && f.tone !== 'neutral' ? `c-${f.tone}` : '', { mono: f.mono }]">{{ f.value }}</dd>
        </div>
      </dl>

      <!-- 4. 设置: everything that is switched or typed in -->
      <DetailSection title="设置">
        <div class="settings">
          <SettingRow title="启用账号" hint="停用后不再调度到这个账号">
            <Switch :model-value="acc.enabled" :loading="busy === 'enabled'" label="启用账号" @update:model-value="setEnabled" />
          </SettingRow>
          <SettingRow title="最大并发" hint="同时处理的请求上限，0 表示不限">
            <span class="cc-in" @keydown.enter="saveConcurrency"><NumberInput v-model="ccDraft" :min="0" :max="10000" integer width="92px" /></span>
            <Btn v-if="ccDirty" size="sm" variant="primary" :loading="busy === 'concurrency'" @click="saveConcurrency">保存</Btn>
          </SettingRow>
          <SettingRow
            v-if="provider === 'openai' && data.allowCredits !== null && data.allowCredits !== undefined"
            title="允许使用积分"
            hint="额度用尽后继续消耗账号积分。关闭会立即重新检查额度；开启要等下次刷新额度后才解除暂停"
          >
            <Switch :model-value="data.allowCredits" :loading="busy === 'credits'" label="允许使用积分" @update:model-value="setAllowCredits" />
          </SettingRow>
          <div :id="`${scope}-settings`" class="slot" />
        </div>
      </DetailSection>

      <!-- 5. 额度: official quota, credits, reset cards and check-ins; refreshed in one place -->
      <DetailSection title="额度" :note="data.usageFetchedAt ? `更新于 ${relative(data.usageFetchedAt, now)}` : '官方用量'">
        <template #actions>
          <Btn variant="ghost" size="sm" :loading="busy === 'usage'" @click="refreshUsage"><RefreshCw /> 刷新额度</Btn>
        </template>
        <p v-if="usageNote" class="progress"><span class="spin" /> {{ usageNote }}</p>
        <p v-if="!quotaSections.length && provider !== 'claude'" class="faint small">暂无额度数据，点「刷新额度」从官方拉取。</p>
        <UsageView v-if="quotaSections.length" :sections="quotaSections" :now="now" />
        <SubBlock v-if="provider === 'claude'" title="官方额度重置" note="消耗 Anthropic 发放的重置次数">
          <template #actions>
            <Btn variant="ghost" size="sm" @click="claudeResetOpen = true"><Sparkles /> 额度重置…</Btn>
          </template>
          <div v-if="claudeReset" class="kv-rows">
            <p v-for="it in claudeReset.items" :key="it.label" class="kv-row">
              <span>{{ it.label }}</span>
              <span class="mono">{{ resetLine(it) }}</span>
            </p>
          </div>
          <p v-else class="faint small">还没有官方重置数据，打开「额度重置」可以实时查询。</p>
        </SubBlock>
        <div :id="`${scope}-quota`" class="slot stack" />
      </DetailSection>

      <!-- 6. 本期用量: what went through Parrot -->
      <DetailSection title="本期用量" :note="compare?.previousComplete ? `${periodLabel} · 右上角对比前一段等长时间` : periodLabel">
        <UsageTiles v-if="tiles.length" :tiles="tiles" />
        <p v-else-if="stats?.requestCount" class="small">
          {{ integer(stats.requestCount) }} 次 · ↑{{ compact(stats.inputTokens) }} ↓{{ compact(stats.outputTokens) }} · {{ dollars(stats.costUsd) }}
        </p>
        <p v-else class="faint small">这个周期还没有经过 Parrot 的请求。</p>

        <div class="models-top">
          <div class="mt-row">
            <p class="mt">按模型</p>
            <Segmented v-model="modelPeriod" :options="MODEL_PERIODS" size="sm" />
          </div>
          <p v-if="modelError" class="faint small">按模型统计加载失败：{{ modelError }}</p>
          <p v-else-if="modelRows === null" class="faint small">加载中…</p>
          <p v-else-if="!modelRows.length" class="faint small">这段时间没有请求。</p>
          <table v-else class="mtable">
            <thead>
              <tr><th class="l">模型</th><th>调用</th><th>失败</th><th>读入 / 写出</th><th>缓存占比</th><th>TPS</th><th>金额</th></tr>
            </thead>
            <tbody>
              <tr v-for="r in (modelRows || []).slice(0, 8)" :key="r.key" @click="openModel(r.key)">
                <td class="l mono">{{ r.key }}</td>
                <td class="num">{{ integer(r.metrics.total) }}</td>
                <td class="num" :class="r.metrics.errorCount ? 'c-red' : 'faint'">{{ integer(r.metrics.errorCount) }}</td>
                <td class="num">{{ compact(promptTokens(r.metrics)) }} / {{ compact(r.metrics.outputTokens) }}</td>
                <td class="num">{{ pctValue(cacheShare(r.metrics)) }}</td>
                <td class="num">{{ r.metrics.averageTokensPerSecond ? tps(r.metrics.averageTokensPerSecond) : '—' }}</td>
                <td class="num">{{ r.metrics.costTicks || !r.metrics.unpricedSuccess ? usd(r.metrics.costTicks) : '未计价' }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="(modelRows?.length || 0) > 8" class="faint small">其余 {{ modelRows!.length - 8 }} 个模型未展开</p>
        </div>
      </DetailSection>

      <!-- 7. 模型 -->
      <AccountModels :account-id="id" :last-sync="data.lastModelSync" />

      <!-- 8. 运行状态: quota pauses and model faults, cleared here -->
      <DetailSection v-if="pauseGroups.length || cooling.length" title="运行状态" :note="cooling.length ? `${cooling.length} 个模型冷却中` : '额度暂停'">
        <template v-if="cooling.length" #actions>
          <Btn variant="ghost" size="sm" :loading="busy === 'errors'" @click="simple('/actions/clear-errors', 'errors', '已清除该账号的模型故障与冷却')"><Eraser /> 清除模型故障</Btn>
        </template>
        <SubBlock v-if="pauseGroups.length" title="额度暂停" note="不是模型故障">
          <div class="kv-rows">
            <p v-for="g in pauseGroups" :key="g.pool" class="kv-row">
              <span>{{ POOL_LABEL[g.pool] || g.pool }} · <b class="mono">{{ g.count }}</b> 个已启用模型</span>
              <span v-if="g.until" class="faint">{{ dateTime(g.until) }} 恢复</span>
            </p>
          </div>
        </SubBlock>
        <ul v-if="cooling.length" class="rerrs">
          <li v-for="(e, i) in cooling" :key="i">
            <p class="line">
              <button type="button" class="mlink mono truncate" @click="openModel(e.modelId)">{{ e.modelId }}</button>
              <Badge :tone="e.cooldownPermanent ? 'red' : 'orange'">{{ cooldownText(e) }}</Badge>
              <span v-if="e.errorCount" class="faint small">累计失败 {{ e.errorCount }} 次</span>
            </p>
            <p v-if="e.message" class="msg">{{ e.message }}</p>
          </li>
        </ul>
      </DetailSection>

      <!-- 9. 账号信息 + maintenance -->
      <DetailSection title="账号信息">
        <KeyValue :items="fields" />
        <div :id="`${scope}-info`" class="slot stack" />
        <div class="maint">
          <Btn v-if="needsLogin" size="sm" variant="primary" @click="startRelogin()"><LogIn /> 重新登录</Btn>
          <Btn v-else size="sm" :loading="busy === 'token'" @click="refreshToken"><KeyRound /> {{ provider === 'zhipu' ? '更新凭据' : '刷新令牌' }}</Btn>
          <Btn size="sm" :loading="busy === 'affinity'" title="让之前固定走这个账号的会话重新分配" @click="simple('/actions/clear-affinity', 'affinity', '已清除亲和绑定')"><Link2Off /> 清除亲和绑定</Btn>
          <span :id="`${scope}-maint`" class="slot-inline" />
          <Btn size="sm" variant="ghost" @click="goLogs({ channel: `oauth:${id}` })"><ListFilter /> 查看相关请求</Btn>
        </div>
      </DetailSection>

      <!-- 10. 操作记录 (Zhipu / WorkBuddy) -->
      <DetailSection v-if="provider === 'zhipu' || provider === 'workbuddy'" title="操作记录">
        <div :id="`${scope}-records`" class="slot" />
      </DetailSection>

      <!-- 11. danger zone -->
      <section class="danger">
        <div>
          <p>删除账号</p>
          <p class="faint small">清除这个账号的统计与亲和绑定，并从调度顺序中移除，无法撤销。</p>
        </div>
        <Btn size="sm" variant="danger" :loading="busy === 'delete'" @click="remove"><Trash2 /> 删除账号</Btn>
      </section>

      <OpenAiCreditsPanel v-if="provider === 'openai'" :account-id="id" :revision="acc.revision" :scope="scope" :tick="panelTick" :resetting="busy === 'reset'" @reset="quotaReset" />
      <WorkBuddyPanel v-if="provider === 'workbuddy'" :account-id="id" :revision="acc.revision" :scope="scope" :tick="panelTick" @changed="onPanelChanged" />
      <ZhipuPanel v-if="provider === 'zhipu'" :account-id="id" :revision="acc.revision" :scope="scope" :tick="panelTick" @changed="onPanelChanged" />
      <QoderPanel v-if="provider === 'qoder'" :account-id="id" :revision="acc.revision" :scope="scope" :tick="panelTick" @changed="onPanelChanged" @relogin="startRelogin" />

      <AddAccountModal :open="Boolean(relogin)" :relogin="relogin" @close="relogin = null" @created="(relogin = null), load()" />
      <ClaudeResetModal v-if="provider === 'claude'" :open="claudeResetOpen" :account-id="id" :name="acc.displayName" @close="claudeResetOpen = false" />
    </template>
    <div v-else class="sk"><Skeleton h="44px" /><Skeleton h="80px" /><Skeleton h="60px" /><Skeleton h="120px" /></div>
  </div>
</template>

<style scoped>
.d {
  padding: 16px 18px 28px;
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-width: 0;
}
.hero {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.who {
  flex: 1;
  min-width: 0;
}
.name {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 16px;
  font-weight: 650;
  word-break: break-all;
}
.edit {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  flex-shrink: 0;
  border-radius: 6px;
  color: var(--text-3);
}
.edit:hover {
  color: var(--text);
  background: var(--fill-2);
}
.edit svg {
  width: 13px;
  height: 13px;
}
.sub {
  font-size: 12px;
  color: var(--text-3);
}
.banners {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: -6px;
}
.banner .btn {
  margin-left: auto;
  flex-shrink: 0;
}
.banner span {
  flex: 1;
  min-width: 0;
}
.banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-md);
  font-size: 12.5px;
  line-height: 1.5;
}
.banner svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  margin-top: 2px;
}
.banner.red {
  background: color-mix(in srgb, var(--red) 12%, transparent);
  color: var(--red);
}
.banner.blue {
  background: color-mix(in srgb, var(--blue) 12%, transparent);
  color: var(--blue);
}
.banner.orange {
  background: color-mix(in srgb, var(--orange) 12%, transparent);
  color: var(--orange);
}
.rerrs {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.rerrs li {
  padding: 8px 10px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
}
.line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.mlink {
  font-size: 12px;
  text-align: left;
}
.mlink:hover {
  color: var(--accent);
  text-decoration: underline;
}
.msg {
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--text-2);
  word-break: break-word;
}
.small {
  font-size: 11.5px;
}
.progress {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-2);
}
.spin {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: 2px solid var(--accent);
  border-right-color: transparent;
  animation: spin 0.8s linear infinite;
}
.err {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
}
.err svg {
  width: 16px;
  height: 16px;
  color: var(--orange);
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
@media (max-width: 400px) {
  .stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .stats > div:nth-child(3) {
    border-left: 0;
    border-top: 0.5px solid var(--hairline);
  }
  .stats > div:nth-child(4) {
    border-top: 0.5px solid var(--hairline);
  }
}
.sub b {
  font-weight: 600;
  color: var(--text-2);
}
.name-edit {
  display: flex;
  align-items: center;
  gap: 4px;
}
.name-edit :deep(.ctl) {
  flex: 1;
}
.facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 14px;
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.fact {
  min-width: 0;
}
.fact dt {
  font-size: 11px;
  color: var(--text-3);
}
.fact dd {
  margin: 2px 0 0;
  font-size: 12.5px;
  font-weight: 500;
  word-break: break-word;
}
.models-top {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.mt {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-2);
}
.mtable {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.mtable th {
  padding: 6px 6px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-3);
  text-align: right;
  white-space: nowrap;
  background: var(--surface-sunken);
}
.mtable td {
  padding: 7px 6px;
  text-align: right;
  white-space: nowrap;
  border-top: 1px solid var(--hairline);
}
.mtable .l {
  text-align: left;
  white-space: normal;
  word-break: break-all;
}
.mtable tbody tr {
  cursor: pointer;
}
.mtable tbody tr:hover td {
  background: var(--surface-hover);
}
@media (max-width: 400px) {
  .facts {
    grid-template-columns: minmax(0, 1fr);
  }
}
.settings {
  display: flex;
  flex-direction: column;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  overflow: hidden;
}
.slot:empty {
  display: none;
}
.stack {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.slot-inline {
  display: contents;
}
.kv-rows {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.kv-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
}
.kv-row > :last-child {
  text-align: right;
  color: var(--text-2);
}
.maint {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding-top: 4px;
}
.danger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 12px;
  border-radius: var(--radius-md);
  font-size: 13px;
  box-shadow: inset 0 0 0 0.5px color-mix(in srgb, var(--red) 35%, transparent);
  background: color-mix(in srgb, var(--red) 4%, transparent);
}
.mt-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 6px;
}
@media (max-width: 400px) {
  .cells {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .cells > div {
    border-right: 0.5px solid var(--hairline) !important;
    border-bottom: 0.5px solid var(--hairline) !important;
  }
}
</style>
