<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { Check, CircleCheck, RefreshCw, TriangleAlert } from '@lucide/vue'
import { api } from '@/api/client'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { dateTime } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Modal from '@/components/ui/Modal.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import { accountPath, errorLine, notifyChanged } from './oauthApi'
import {
  CLAUDE_GRANT_KIND_LABEL,
  CLAUDE_PROGRAM_LABEL,
  CLAUDE_RESULT,
  claudeGrantKind,
  claudeReason,
  pick,
  type CedarGrantKind,
  type ClaudeProgram,
  type ClaudeResetPlan,
  type ClaudeResetResult,
} from './meta'

type Rec = Record<string, unknown>

const props = defineProps<{ open: boolean; accountId: string; name: string }>()
const emit = defineEmits<{ close: [] }>()

const PROGRAMS: ClaudeProgram[] = ['cedar_ember', 'juniper_tide']
const plans = reactive<Record<ClaudeProgram, ClaudeResetPlan | null>>({ cedar_ember: null, juniper_tide: null })
const errors = reactive<Record<ClaudeProgram, string>>({ cedar_ember: '', juniper_tide: '' })
const loading = ref(false)
const selectingGrant = ref('')
const executing = ref<ClaudeProgram | ''>('')
const result = ref<ClaudeResetResult | null>(null)
let queryEpoch = 0

/** Step 1 — explanation: read-only plans for both programs (never consumes anything). */
async function query(): Promise<void> {
  if (loading.value) return
  loading.value = true
  selectingGrant.value = ''
  const epoch = ++queryEpoch
  const accountId = props.accountId
  result.value = null
  let gotAny = false
  await Promise.all(
    PROGRAMS.map(async (program) => {
      errors[program] = ''
      if (plans[program]) plans[program] = { ...plans[program]!, available: false, planToken: null }
      try {
        const data = (await api.post<ClaudeResetPlan>(accountPath(accountId, '/claude-reset/plans'), { program })).data
        if (epoch === queryEpoch) {
          plans[program] = data
          gotAny = true
        }
      } catch (err) {
        if (epoch === queryEpoch) errors[program] = errorLine(err).replace(/[。.]\s*$/, '')
      }
    }),
  )
  if (epoch === queryEpoch) {
    loading.value = false
    if (gotAny) notifyChanged(accountId)
  }
}

watch(
  () => props.accountId,
  () => {
    queryEpoch++
    loading.value = false
    selectingGrant.value = ''
    for (const program of PROGRAMS) {
      plans[program] = null
      errors[program] = ''
    }
    if (props.open) void query()
  },
)
watch(
  () => props.open,
  (o) => o && query(),
  { immediate: true },
)

const status = computed<Rec>(() => ({ ...(plans.juniper_tide?.status || {}), ...(plans.cedar_ember?.status || {}) }))
const cedar = computed(() => pick<Rec>(status.value, 'cedarEmber') || null)
const juniper = computed(() => pick<Rec>(status.value, 'juniperTide') || null)
const grants = computed(() => ((pick<Rec[]>(cedar.value, 'grants') || []) as Rec[]).filter((g) => g && typeof g === 'object'))
const queries = computed(
  () =>
    ({
      ...(pick<Rec>(plans.juniper_tide?.status, 'claudeResetQueries') || {}),
      ...(pick<Rec>(plans.cedar_ember?.status, 'claudeResetQueries') || {}),
    }) as Rec,
)

function queryMeta(program: ClaudeProgram): { time: string; warning: string } {
  const q = pick<Rec>(queries.value, program === 'cedar_ember' ? 'cedarEmber' : 'juniperTide') || {}
  const fetched = pick(q, 'fetchedAt')
  const retry = pick(q, 'retryAt')
  const warnParts: string[] = []
  if (retry) warnParts.push(`查询限流，冷却至 ${when(retry)}（到期不保证上游已解除）`)
  if (pick(q, 'state') === 'error') warnParts.push('本次查询失败，以下为上次缓存数据')
  return {
    time: fetched ? `更新于 ${when(fetched)}` : '',
    warning: warnParts.join(' · '),
  }
}

function missing(program: ClaudeProgram): string {
  const q = (pick<Rec>(queries.value, program === 'cedar_ember' ? 'cedarEmber' : 'juniperTide') || pick<Rec>(queries.value, program) || {}) as Rec
  const state = pick(q, 'state')
  if (state === 'not_provided') return '已查询，官方未返回此功能状态（不代表 0 张或无资格）'
  if (state === 'invalid_response') return '官方返回格式无法识别，请稍后重新查询'
  if (state === 'error') {
    const code = pick<number>(q, 'httpStatus')
    const why = code === 401 || code === 403 ? '认证或权限被拒绝' : code === 429 ? '请求被限流' : '请求失败'
    return `查询失败：${why}${code ? `（HTTP ${code}）` : ''}`
  }
  return '暂无状态数据'
}

function when(v: unknown): string {
  return typeof v === 'string' || typeof v === 'number' ? dateTime(v) : '—'
}

/** Normalize both snake_case and camelCase upstream window IDs to clean Chinese labels. */
const WINDOW_ID: Record<string, string> = {
  five_hour: '5 小时',
  seven_day: '7 天',
  seven_day_overage_included: 'Fable 7 天',
  seven_day_opus: 'Opus 7 天',
  seven_day_sonnet: 'Sonnet 7 天',
  seven_day_fable: 'Fable 7 天',
  seven_day_cowork: 'Cowork 7 天',
  seven_day_omelette: 'Omelette 7 天',
  seven_day_oauth_apps: '7 天（OAuth 应用）',
}
function windowNameById(rawKey: unknown): string {
  const s = String(rawKey || '').trim()
  if (!s) return '—'
  if (WINDOW_ID[s]) return WINDOW_ID[s]
  const snake = s.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`)
  return WINDOW_ID[snake] || s
}
function list(v: unknown): string {
  return Array.isArray(v) && v.length ? v.map(windowNameById).join('、') : '—'
}
function grantClearsText(g: Rec): string {
  const base = list(pick(g, 'clears'))
  if (pick(g, 'givesBackWeekly') === true) {
    return base === '—' ? '清 Fable 用量并退回 7 天周池' : `${base}（退回本周 Fable 占用的 7 天周额度）`
  }
  return base
}
function grantPercentInfo(g: Rec): { text: string; allZero: boolean } {
  const raw = pick<Rec>(g, 'percentUsed')
  if (!raw || typeof raw !== 'object') return { text: '', allZero: false }
  const entries = Object.entries(raw).filter(([, v]) => typeof v === 'number' && Number.isFinite(v)) as [string, number][]
  if (!entries.length) return { text: '', allZero: false }
  return {
    text: entries.map(([k, v]) => `${windowNameById(k)} ${v}%`).join(' · '),
    allZero: entries.every(([, v]) => v <= 0),
  }
}

function stampMs(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v > 1e12 ? v : v * 1000
  if (typeof v === 'string' && v) {
    const ms = Date.parse(v)
    return Number.isNaN(ms) ? null : ms
  }
  return null
}
function grantTone(kind: CedarGrantKind): 'purple' | 'teal' | 'blue' | 'neutral' {
  return kind === 'fable' ? 'purple' : kind === 'session' ? 'teal' : kind === 'weekly' ? 'blue' : 'neutral'
}
function grantReasonCode(g: Rec): string | null {
  if (pick(g, 'paused')) return 'paused'
  const now = Date.now()
  const end = stampMs(pick(g, 'endsAt'))
  if (end !== null && end <= now) return 'expired'
  const start = stampMs(pick(g, 'startsAt'))
  if (start !== null && start > now) return 'not_started'
  const left = pick(g, 'resetsLeft')
  if (typeof left !== 'number' || !Number.isInteger(left) || left <= 0) return 'already_used'
  const rawBlocking = pick<unknown[]>(g, 'blocking')
  const blocking = Array.isArray(rawBlocking) ? rawBlocking.map(String) : []
  const ur = pick<string>(g, 'unavailableReason')
  if (typeof ur === 'string' && ur.trim()) {
    const reason = ur.trim()
    if (pick(g, 'givesBackWeekly') === true && (reason === 'blocked_seven_day' || blocking.includes('seven_day'))) {
      return 'fable_no_weekly_usage'
    }
    return reason
  }
  if (pick(g, 'useRequiresLimit') !== false && pick(cedar.value, 'atLimit') !== true) return 'not_limited'
  if (blocking.length) {
    if (pick(g, 'givesBackWeekly') === true && blocking.includes('seven_day')) return 'fable_no_weekly_usage'
    const first = blocking[0]
    if (first === 'five_hour') return 'blocked_five_hour'
    if (first === 'seven_day') return 'blocked_seven_day'
    if (first === 'seven_day_opus') return 'blocked_opus'
    if (first === 'seven_day_sonnet') return 'blocked_sonnet'
    return 'blocked_other'
  }
  if (pick(g, 'usableNow') !== true) return 'unavailable'
  return null
}
function grantUnavailableText(g: Rec): string {
  const code = grantReasonCode(g)
  return code ? claudeReason(code) : ''
}

const activeGrantCount = computed(() =>
  grants.value.reduce((acc, g) => {
    const left = pick<number>(g, 'resetsLeft')
    return !pick(g, 'paused') && typeof left === 'number' && left > 0 ? acc + left : acc
  }, 0),
)

const cedarBlockExecutable = computed<boolean>(() => {
  const q = (pick<Rec>(queries.value, 'cedarEmber') || pick<Rec>(queries.value, 'cedar_ember') || {}) as Rec
  const state = pick(q, 'state')
  if (state === 'error' || state === 'cached') return false
  if (!cedar.value || pick(cedar.value, 'eligible') !== true) return false
  const cd = stampMs(pick(cedar.value, 'cooldownUntil'))
  return !(cd !== null && cd > Date.now())
})
function isSelectedGrant(g: Rec): boolean {
  return Boolean(plans.cedar_ember?.available && plans.cedar_ember.grantId && String(pick(g, 'id')) === plans.cedar_ember.grantId)
}
function canSelectGrant(g: Rec): boolean {
  const id = String(pick(g, 'id') || '')
  return Boolean(id) && cedarBlockExecutable.value && plans.cedar_ember?.reason !== USED && !grantReasonCode(g)
}
const selectedCedarGrant = computed<Rec | null>(() => {
  const gid = plans.cedar_ember?.grantId
  return gid ? grants.value.find((g) => String(pick(g, 'id')) === gid) || null : null
})
const selectedCedarSummary = computed<string>(() => {
  const g = selectedCedarGrant.value
  const gid = plans.cedar_ember?.grantId || ''
  if (!g) return gid
  return `${CLAUDE_GRANT_KIND_LABEL[claudeGrantKind(g)]}（${gid}）`
})

/** Bind a one-shot plan to a user-chosen eligible card (hits 60s cache, no extra upstream GET). */
async function selectGrant(grantId: string): Promise<void> {
  if (!grantId || loading.value || executing.value || selectingGrant.value) return
  if (plans.cedar_ember?.available && plans.cedar_ember.grantId === grantId) return
  selectingGrant.value = grantId
  const epoch = queryEpoch
  const accountId = props.accountId
  errors.cedar_ember = ''
  try {
    const data = (await api.post<ClaudeResetPlan>(accountPath(accountId, '/claude-reset/plans'), { program: 'cedar_ember', grantId })).data
    if (epoch === queryEpoch) plans.cedar_ember = data
  } catch (err) {
    if (epoch === queryEpoch) toast('切换重置卡失败', 'error', errorLine(err), 7000)
  } finally {
    if (epoch === queryEpoch) selectingGrant.value = ''
  }
}

function onCardClick(g: Rec): void {
  if (!canSelectGrant(g) || isSelectedGrant(g)) return
  void selectGrant(String(pick(g, 'id') || ''))
}

/** Steps 2 + 3 — final confirmation, then the consuming request (one-shot plan, never retried). */
async function execute(program: ClaudeProgram): Promise<void> {
  const plan = plans[program]
  if (!plan?.available || !plan.planToken) return
  const cedarProgram = program === 'cedar_ember'
  const g = cedarProgram ? selectedCedarGrant.value : null
  const kind = g ? claudeGrantKind(g) : null
  const kindLabel = kind ? CLAUDE_GRANT_KIND_LABEL[kind] : CLAUDE_PROGRAM_LABEL.cedar_ember
  const grantLabel = g ? String(pick(g, 'label') || plan.grantId || '') : plan.grantId || ''
  const cedarDesc =
    kind === 'fable'
      ? `使用「${kindLabel}」（ID: ${plan.grantId || ''} · ${grantLabel}），清除 Fable 周子限额并将本周 Fable 已用份额退回 7 天周池；每周自然重置日期不变。`
      : `使用「${kindLabel}」（ID: ${plan.grantId || ''} · ${grantLabel}），清除 ${g ? grantClearsText(g) : '对应窗口'} 的已用额度；每周自然重置日期不变。`
  const ok = await confirmAction({
    title: `最终确认：Claude 官方${cedarProgram ? kindLabel : CLAUDE_PROGRAM_LABEL[program]}`,
    detail: [
      `账号：${props.name}`,
      cedarProgram ? cedarDesc : '重置 5 小时会话额度，会消耗周额度份额，并受每周次数限制；不是补充周额度。',
      '',
      '• 会立刻向 Anthropic 发送消耗请求，不可撤销。',
      '• 成功不等于立即可用：会按刷新后的真实用量判断，仍超限会保留本地限制。',
      '• 结果不确定时不会自动重发，请重新查询核对。',
    ].join('\n'),
    confirmText: '执行官方重置',
    danger: true,
    requireText: '重置',
  })
  if (!ok) return
  executing.value = program
  try {
    result.value = (await api.post<ClaudeResetResult>(accountPath(props.accountId, '/claude-reset/execute'), { planToken: plan.planToken })).data
    const good = result.value.result === 'reset'
    toast(`Claude 官方重置：${CLAUDE_RESULT[result.value.result || ''] || result.value.result || '已提交'}`, good ? 'success' : 'warning', claudeReason(result.value.reason) || undefined, 8000)
    notifyChanged(props.accountId)
  } catch (err) {
    toast('官方重置没有完成', 'error', `${errorLine(err).replace(/[。.]\s*$/, '')}。不会自动重发，请重新查询核对。`, 9000)
  } finally {
    plans[program] = plans[program] ? { ...plans[program]!, available: false, planToken: null, reason: USED } : null
    executing.value = ''
  }
}

const USED = '__used'

const LOCAL_ACTION: Record<string, string> = {
  resumed: '最新用量已低于阈值，账号已自动恢复',
  kept_enabled: '账号保持可用',
  still_over_quota: '最新用量仍超限，本地限制已保留',
  refresh_failed_keep_disabled: '刷新用量失败，本地限制已保留',
  quota_unknown_keep_disabled: '最新用量没有有效数据，本地限制已保留',
  claude_fable_model_recovered: '最新 Fable 用量已低于阈值，Fable 模型限流已自动恢复',
  claude_fable_model_still_cooling: '最新 Fable 用量仍超阈值，Fable 模型冷却已保留',
  claude_fable_model_cooldown: '最新 Fable 用量已超阈值，仅对 Fable 模型保留冷却',
}

const juniperReasonText = computed(() => {
  if (!juniper.value) return ''
  if (pick(juniper.value, 'available') === true) return '当前可执行（会消耗周额度份额）'
  const raw = pick<string>(juniper.value, 'ineligibleReason') || plans.juniper_tide?.reason
  return claudeReason(raw ? String(raw) : 'not_at_wall')
})
</script>

<template>
  <Modal :open="open" title="Claude 官方额度重置" :subtitle="`${name} · 查询不消耗次数，最终确认后才向官方提交`" width="lg" @close="emit('close')">
    <div class="wrap">
      <div class="notice">
        查询仅只读获取卡片状态；点击下方红色执行按钮并输入「重置」确认后，才会向 Anthropic 发送不可撤销的消耗请求（每周自然重置日期不变）。
      </div>

      <div v-if="result" class="result">
        <p class="rt"><CircleCheck /> 执行结果：{{ CLAUDE_RESULT[result.result || ''] || result.result || '未知' }}</p>
        <dl class="kv">
          <template v-if="result.grantId"><dt>使用卡片</dt><dd class="mono">{{ result.grantId }}</dd></template>
          <template v-if="result.reason"><dt>原因说明</dt><dd>{{ claudeReason(result.reason) }}</dd></template>
          <template v-if="result.resetsLeft !== null && result.resetsLeft !== undefined"><dt>剩余次数</dt><dd class="mono">{{ result.resetsLeft }}</dd></template>
          <template v-if="result.cleared?.length"><dt>已清窗口</dt><dd>{{ list(result.cleared) }}</dd></template>
          <template v-if="result.cooldownUntil"><dt>冷却至</dt><dd>{{ when(result.cooldownUntil) }}</dd></template>
          <template v-if="result.nextAvailableAt"><dt>下次可用</dt><dd>{{ when(result.nextAvailableAt) }}</dd></template>
          <template v-if="result.weeklyResetsAt"><dt>周重置日</dt><dd>{{ when(result.weeklyResetsAt) }}</dd></template>
          <template v-if="pick(result.quotaAction, 'action')">
            <dt>本地评估</dt>
            <dd>{{ LOCAL_ACTION[String(pick(result.quotaAction, 'action'))] || pick(result.quotaAction, 'action') }}</dd>
          </template>
        </dl>
      </div>

      <div v-if="loading" class="sk">
        <Skeleton h="150px" r="10px" />
        <Skeleton h="76px" r="10px" />
      </div>

      <div v-else class="sections">
        <!-- 1. 周额度重置卡 (Full-width structured stack) -->
        <section class="sec">
          <header class="sec-head">
            <div class="sec-title-wrap">
              <p class="sec-title">周额度重置卡</p>
              <span v-if="cedar" class="sec-sub">
                可用 <b class="num">{{ activeGrantCount }}</b> 次（共 {{ grants.length }} 张卡）
                <template v-if="pick(cedar, 'weeklyResetsAt')"> · 周重置 {{ when(pick(cedar, 'weeklyResetsAt')) }}</template>
                <template v-if="Array.isArray(pick(cedar, 'exhausted')) && (pick<unknown[]>(cedar, 'exhausted') || []).length">
                  · 已撞限：{{ list(pick(cedar, 'exhausted')) }}
                </template>
              </span>
            </div>
            <div class="sec-right">
              <span v-if="queryMeta('cedar_ember').time" class="stamp">{{ queryMeta('cedar_ember').time }}</span>
              <Badge v-if="plans.cedar_ember?.available" tone="green">可执行</Badge>
              <Badge v-else-if="plans.cedar_ember" tone="neutral">不可执行</Badge>
              <Badge v-else-if="errors.cedar_ember" tone="red">查询失败</Badge>
            </div>
          </header>

          <p v-if="errors.cedar_ember" class="err"><TriangleAlert /> {{ errors.cedar_ember }}（未发送任何消耗请求）</p>
          <p v-if="queryMeta('cedar_ember').warning" class="warn-line">{{ queryMeta('cedar_ember').warning }}</p>

          <template v-if="cedar">
            <ul v-if="grants.length" class="glist">
              <li
                v-for="g in grants"
                :key="String(pick(g, 'id'))"
                class="gcard"
                :class="{
                  selected: isSelectedGrant(g),
                  selectable: canSelectGrant(g) && !isSelectedGrant(g),
                  disabled: Boolean(grantUnavailableText(g)),
                }"
                @click="onCardClick(g)"
              >
                <div class="gtop">
                  <span
                    v-if="canSelectGrant(g) || isSelectedGrant(g)"
                    class="radio"
                    :class="{ on: isSelectedGrant(g), busy: selectingGrant === String(pick(g, 'id')) }"
                    aria-hidden="true"
                  >
                    <Check v-if="isSelectedGrant(g)" />
                  </span>
                  <div class="ghead">
                    <div class="gbadges">
                      <Badge :tone="grantTone(claudeGrantKind(g))">{{ CLAUDE_GRANT_KIND_LABEL[claudeGrantKind(g)] }}</Badge>
                      <Badge v-if="pick(g, 'id') === pick(cedar, 'nextGrantId')" tone="blue">官方推荐</Badge>
                      <Badge v-if="pick(g, 'paused')" tone="orange">已暂停</Badge>
                      <span class="trigger-pill">{{ pick(g, 'useRequiresLimit') === false ? '随时可用' : '撞限后可用' }}</span>
                    </div>
                    <p class="gname">{{ pick(g, 'label') || pick(g, 'id') }}</p>
                  </div>
                  <span class="gcount mono">剩余 {{ pick(g, 'resetsLeft') ?? '?' }} / {{ pick(g, 'resetsTotal') ?? '?' }}</span>
                </div>

                <dl class="gspecs">
                  <dt>作用范围</dt>
                  <dd>{{ grantClearsText(g) }}</dd>
                  <template v-if="grantPercentInfo(g).text">
                    <dt>当前已用</dt>
                    <dd>
                      <span class="num">{{ grantPercentInfo(g).text }}</span>
                      <span v-if="grantPercentInfo(g).allZero" class="zero-hint">（当前各窗口已用均为 0%，暂无需消耗此卡）</span>
                    </dd>
                  </template>
                  <dt>有效期限</dt>
                  <dd class="num">
                    {{ when(pick(g, 'startsAt')) }} → {{ when(pick(g, 'endsAt')) }}
                    <span class="gid mono">ID: {{ pick(g, 'id') }}</span>
                  </dd>
                </dl>

                <p v-if="grantUnavailableText(g)" class="gunavail">不可用原因：{{ grantUnavailableText(g) }}</p>
              </li>
            </ul>
            <p v-else class="empty-line">
              当前账号暂无发放周额度重置卡
              <template v-if="pick(cedar, 'ineligibleReason')">（{{ claudeReason(String(pick(cedar, 'ineligibleReason'))) }}）</template>
            </p>
          </template>
          <p v-else-if="plans.cedar_ember" class="empty-line">{{ missing('cedar_ember') }}</p>

          <div v-if="plans.cedar_ember?.available" class="action-bar">
            <p class="selected-hint truncate">
              当前已选：<b>{{ selectedCedarSummary }}</b>
              <span v-if="grants.length > 1" class="faint">（点击上方其它卡片可切换）</span>
            </p>
            <Btn
              variant="danger"
              size="sm"
              :loading="executing === 'cedar_ember'"
              :disabled="Boolean(selectingGrant)"
              @click="execute('cedar_ember')"
            >
              使用已选重置卡…
            </Btn>
          </div>
          <p v-else-if="plans.cedar_ember && plans.cedar_ember.reason !== USED && grants.length" class="unavail-summary">
            当前无法执行：{{ claudeReason(plans.cedar_ember.reason) || '原因未知' }}
          </p>
          <p v-else-if="plans.cedar_ember?.reason === USED" class="empty-line">本次确认令牌已使用；如需再次操作，请点右下角「重新查询」。</p>
        </section>

        <!-- 2. 5 小时会话重置 (Compact clean block) -->
        <section class="sec sec-compact">
          <header class="sec-head">
            <div class="sec-title-wrap">
              <p class="sec-title">5 小时会话重置 <span class="sec-sub">（消耗 7 天周额度份额 · 每周 {{ pick(juniper, 'resetsPerWeek') ?? 1 }} 次）</span></p>
            </div>
            <div class="sec-right">
              <span v-if="queryMeta('juniper_tide').time" class="stamp">{{ queryMeta('juniper_tide').time }}</span>
              <Badge v-if="plans.juniper_tide?.available" tone="green">可执行</Badge>
              <Badge v-else-if="plans.juniper_tide" tone="neutral">不可执行</Badge>
              <Badge v-else-if="errors.juniper_tide" tone="red">查询失败</Badge>
            </div>
          </header>

          <p v-if="errors.juniper_tide" class="err"><TriangleAlert /> {{ errors.juniper_tide }}（未发送任何消耗请求）</p>
          <p v-if="queryMeta('juniper_tide').warning" class="warn-line">{{ queryMeta('juniper_tide').warning }}</p>

          <div v-if="juniper" class="juniper-body">
            <div class="juniper-info">
              <p class="juniper-state">
                状态：<span :class="plans.juniper_tide?.available ? 'c-green' : 'c-muted'">{{ juniperReasonText }}</span>
              </p>
              <p v-if="pick(juniper, 'nextAvailableAt') || pick(juniper, 'weeklyResetsAt')" class="juniper-meta">
                <template v-if="pick(juniper, 'nextAvailableAt')">下次可用：{{ when(pick(juniper, 'nextAvailableAt')) }}</template>
                <template v-if="pick(juniper, 'nextAvailableAt') && pick(juniper, 'weeklyResetsAt')"> · </template>
                <template v-if="pick(juniper, 'weeklyResetsAt')">周重置：{{ when(pick(juniper, 'weeklyResetsAt')) }}</template>
              </p>
            </div>
            <Btn
              v-if="plans.juniper_tide?.available"
              variant="danger"
              size="sm"
              :loading="executing === 'juniper_tide'"
              @click="execute('juniper_tide')"
            >
              执行 5 小时重置…
            </Btn>
          </div>
          <p v-else-if="plans.juniper_tide" class="empty-line">{{ missing('juniper_tide') }}</p>
        </section>
      </div>
    </div>

    <template #footer>
      <Btn variant="ghost" @click="emit('close')">关闭</Btn>
      <Btn :loading="loading" :disabled="Boolean(executing || selectingGrant)" @click="query"><RefreshCw /> 重新查询（不消耗）</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.wrap {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.notice {
  padding: 9px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-2);
}
.sections {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.sec {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border-strong);
}
.sec-compact {
  gap: 8px;
  padding: 12px 14px;
}
.sec-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px 12px;
}
.sec-title-wrap {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px 10px;
  min-width: 0;
}
.sec-title {
  font-size: 13.5px;
  font-weight: 650;
  color: var(--text);
}
.sec-sub {
  font-size: 12px;
  font-weight: 400;
  color: var(--text-3);
}
.sec-sub b {
  color: var(--text);
  font-weight: 650;
}
.sec-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.stamp {
  font-size: 11.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
.warn-line {
  font-size: 11.5px;
  color: var(--text-orange);
}
.glist {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.gcard {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 11px 13px;
  border-radius: 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  transition: border-color 0.15s, background 0.15s, box-shadow 0.15s;
}
.gcard.selectable {
  cursor: pointer;
}
.gcard.selectable:hover {
  border-color: var(--border-strong);
  box-shadow: var(--shadow-sm);
}
.gcard.selected {
  border-color: color-mix(in srgb, var(--green) 55%, var(--border));
  background: color-mix(in srgb, var(--green) 4%, var(--surface));
}
.gcard.disabled {
  opacity: 0.78;
}
.gtop {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.radio {
  margin-top: 2px;
  width: 17px;
  height: 17px;
  border-radius: 50%;
  border: 1.5px solid var(--border-strong);
  background: var(--surface);
  display: grid;
  place-items: center;
  flex-shrink: 0;
  transition: border-color 0.15s, background 0.15s;
}
.radio.on {
  border-color: var(--green);
  background: var(--green);
  color: #fff;
}
.radio.busy {
  opacity: 0.5;
}
.radio svg {
  width: 11px;
  height: 11px;
  stroke-width: 3;
}
.ghead {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.gbadges {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
}
.trigger-pill {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  color: var(--text-2);
  background: var(--fill-2);
}
.gname {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.4;
  color: var(--text);
  overflow-wrap: anywhere;
}
.gcount {
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--fill-2);
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-2);
  white-space: nowrap;
  flex-shrink: 0;
}
.gspecs {
  display: grid;
  grid-template-columns: 58px minmax(0, 1fr);
  gap: 4px 10px;
  margin: 0;
  padding-left: 27px;
  font-size: 12px;
  line-height: 1.45;
}
.gspecs dt {
  color: var(--text-3);
}
.gspecs dd {
  margin: 0;
  color: var(--text-2);
  overflow-wrap: anywhere;
}
.zero-hint {
  color: var(--text-3);
  font-size: 11.5px;
}
.gid {
  margin-left: 8px;
  font-size: 11px;
  color: var(--text-3);
}
.gunavail {
  margin: 0;
  padding-left: 27px;
  font-size: 12px;
  color: var(--text-orange);
}
.action-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-top: 4px;
  border-top: 0.5px solid var(--hairline);
}
.selected-hint {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--text-2);
}
.selected-hint b {
  color: var(--text);
}
.unavail-summary {
  font-size: 12px;
  color: var(--text-orange);
}
.juniper-body {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 12px;
}
.juniper-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.juniper-state {
  font-size: 12.5px;
  color: var(--text-2);
}
.juniper-meta {
  font-size: 11.5px;
  color: var(--text-3);
}
.empty-line {
  font-size: 12px;
  color: var(--text-3);
}
.result {
  padding: 12px 14px;
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--green) 7%, var(--surface));
  border: 1px solid color-mix(in srgb, var(--green) 30%, var(--border));
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rt {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 650;
}
.rt svg {
  width: 15px;
  height: 15px;
  color: var(--green);
}
.kv {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 4px 12px;
  margin: 0;
  font-size: 12px;
}
.kv dt {
  color: var(--text-3);
}
.kv dd {
  margin: 0;
}
.err {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 12px;
  color: var(--text-red);
}
.err svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  margin-top: 2px;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.c-green {
  color: var(--text-green);
  font-weight: 600;
}
.c-muted {
  color: var(--text-2);
}
@media (max-width: 560px) {
  .gspecs,
  .gunavail {
    padding-left: 0;
  }
  .action-bar {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
