<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { CircleCheck, RefreshCw, TriangleAlert } from '@lucide/vue'
import { api } from '@/api/client'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { dateTime } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Modal from '@/components/ui/Modal.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import { accountPath, errorLine, notifyChanged } from './oauthApi'
import { CLAUDE_PROGRAM_LABEL, CLAUDE_RESULT, claudeReason, pick, type ClaudeProgram, type ClaudeResetPlan, type ClaudeResetResult } from './meta'

type Rec = Record<string, unknown>

const props = defineProps<{ open: boolean; accountId: string; name: string }>()
const emit = defineEmits<{ close: [] }>()

const PROGRAMS: ClaudeProgram[] = ['cedar_ember', 'juniper_tide']
const plans = reactive<Record<ClaudeProgram, ClaudeResetPlan | null>>({ cedar_ember: null, juniper_tide: null })
const errors = reactive<Record<ClaudeProgram, string>>({ cedar_ember: '', juniper_tide: '' })
const loading = ref(false)
const executing = ref<ClaudeProgram | ''>('')
const result = ref<ClaudeResetResult | null>(null)

/** Step 1 — explanation: read-only plans for both programs (never consumes anything). */
async function query(): Promise<void> {
  loading.value = true
  result.value = null
  await Promise.all(
    PROGRAMS.map(async (program) => {
      errors[program] = ''
      plans[program] = null
      try {
        plans[program] = (await api.post<ClaudeResetPlan>(accountPath(props.accountId, '/claude-reset/plans'), { program })).data
      } catch (err) {
        errors[program] = errorLine(err).replace(/[。.]\s*$/, '')
      }
    }),
  )
  loading.value = false
}
watch(
  () => props.open,
  (o) => o && query(),
  { immediate: true },
)

/** Status blocks share one observation; take whichever plan returned it. */
const status = computed<Rec>(() => ({ ...(plans.juniper_tide?.status || {}), ...(plans.cedar_ember?.status || {}) }))
const cedar = computed(() => pick<Rec>(status.value, 'cedarEmber') || null)
const juniper = computed(() => pick<Rec>(status.value, 'juniperTide') || null)
const grants = computed(() => ((pick<Rec[]>(cedar.value, 'grants') || []) as Rec[]).filter((g) => g && typeof g === 'object'))
const queries = computed(() => (pick<Rec>(status.value, 'claudeResetQueries') || {}) as Rec)

function missing(program: ClaudeProgram): string {
  const q = (pick<Rec>(queries.value, program === 'cedar_ember' ? 'cedarEmber' : 'juniperTide') || pick<Rec>(queries.value, program) || {}) as Rec
  const state = pick(q, 'state')
  if (state === 'not_provided') return '已查询，官方没有返回这项功能的状态（不等于 0 张或没有资格）'
  if (state === 'invalid_response') return '官方返回的格式无法识别，请重新查询'
  if (state === 'error') {
    const code = pick<number>(q, 'httpStatus')
    const why = code === 401 || code === 403 ? '认证或权限被拒绝' : code === 429 ? '请求被限流' : '请求失败'
    return `查询失败：${why}${code ? `（HTTP ${code}）` : ''}`
  }
  return '暂无状态数据'
}
function yes(v: unknown): string {
  return v === true ? '是' : v === false ? '否' : '未知'
}
function when(v: unknown): string {
  return typeof v === 'string' || typeof v === 'number' ? dateTime(v) : '—'
}
/** Upstream window ids (seven_day, five_hour, seven_day_opus …) in plain words. */
const WINDOW_ID: Record<string, string> = {
  five_hour: '5 小时',
  seven_day: '7 天',
  seven_day_opus: 'Opus 7 天',
  seven_day_sonnet: 'Sonnet 7 天',
  seven_day_fable: 'Fable 7 天',
  seven_day_oauth_apps: '7 天（OAuth 应用）',
}
function list(v: unknown): string {
  return Array.isArray(v) && v.length ? v.map((x) => WINDOW_ID[String(x)] || String(x)).join('、') : '—'
}

/** Steps 2 + 3 — final confirmation, then the consuming request (one-shot plan, never retried). */
async function execute(program: ClaudeProgram): Promise<void> {
  const plan = plans[program]
  if (!plan?.available || !plan.planToken) return
  const cedarProgram = program === 'cedar_ember'
  const ok = await confirmAction({
    title: `最终确认：Claude 官方${CLAUDE_PROGRAM_LABEL[program]}`,
    detail: [
      `账号：${props.name}`,
      cedarProgram
        ? `使用周额度重置卡 ${plan.grantId || ''}，消耗这张卡的一次次数；每周自然重置的日期不变。`
        : '重置 5 小时会话额度，会消耗周额度份额，并受每周次数限制；不是补充周额度。',
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
    toast('官方重置没有完成', 'error', `${errorLine(err).replace(/[。.]\s*$/, "")}。不会自动重发，请重新查询核对。`, 9000)
  } finally {
    // The plan was one-shot either way; a new action needs a fresh query.
    plans[program] = plans[program] ? { ...plans[program]!, available: false, planToken: null, reason: USED } : null
    executing.value = ''
  }
}

/** Marks a plan whose one-shot token was spent in this dialog. */
const USED = '__used'

const LOCAL_ACTION: Record<string, string> = {
  resumed: '最新用量已低于阈值，账号已自动恢复',
  kept_enabled: '账号保持可用',
  still_over_quota: '最新用量仍超限，本地限制已保留',
  refresh_failed_keep_disabled: '刷新用量失败，本地限制已保留',
  quota_unknown_keep_disabled: '最新用量没有有效数据，本地限制已保留',
}
</script>

<template>
  <Modal :open="open" title="Claude 官方额度重置" :subtitle="`${name} · 先查询（不消耗），再最终确认后执行`" width="lg" @close="emit('close')">
    <div class="wrap">
      <div class="explain">
        <p class="et">说明（本页查询不会消耗任何次数）</p>
        <ul>
          <li><b>周额度重置卡</b>：清除指定窗口的用量，<b>不改变</b>每周自然重置的日期；只使用官方指定的下一张卡。</li>
          <li><b>5 小时重置</b>：重置 5 小时会话额度，<b>会消耗周额度份额</b>，并受每周次数限制。</li>
          <li>和「清除本地额度暂停」不同：这里会向 Anthropic 发送真实的消耗请求，不可撤销；不会解除手动停用或认证失效。</li>
        </ul>
      </div>

      <div v-if="result" class="result">
        <p class="rt"><CircleCheck /> 执行结果：{{ CLAUDE_RESULT[result.result || ''] || result.result || '未知' }}</p>
        <dl class="kv">
          <template v-if="result.reason"><dt>原因</dt><dd>{{ claudeReason(result.reason) }}</dd></template>
          <template v-if="result.resetsLeft !== null && result.resetsLeft !== undefined"><dt>卡剩余次数</dt><dd class="mono">{{ result.resetsLeft }}</dd></template>
          <template v-if="result.cleared?.length"><dt>实际清除的窗口</dt><dd class="mono">{{ list(result.cleared) }}</dd></template>
          <template v-if="result.cooldownUntil"><dt>冷却至</dt><dd>{{ when(result.cooldownUntil) }}</dd></template>
          <template v-if="result.nextAvailableAt"><dt>下次可用</dt><dd>{{ when(result.nextAvailableAt) }}</dd></template>
          <template v-if="result.weeklyResetsAt"><dt>周重置日期（不变）</dt><dd>{{ when(result.weeklyResetsAt) }}</dd></template>
          <template v-if="pick(result.quotaAction, 'action')"><dt>本地额度评估</dt><dd>{{ LOCAL_ACTION[String(pick(result.quotaAction, 'action'))] || pick(result.quotaAction, 'action') }}</dd></template>
        </dl>
        <p class="faint small">返回「已重置 / 已使用过」也不保证立即可用；结果未知、仍超限或刷新失败时会保留本地限制。</p>
      </div>

      <div v-if="loading" class="sk"><Skeleton h="90px" r="10px" /><Skeleton h="70px" r="10px" /></div>
      <div v-else class="programs">
        <!-- weekly cards -->
        <section class="prog">
          <header>
            <p class="pt">周额度重置卡</p>
            <Badge v-if="plans.cedar_ember?.available" tone="green">可执行</Badge>
            <Badge v-else-if="plans.cedar_ember" tone="neutral">不可执行</Badge>
            <Badge v-else-if="errors.cedar_ember" tone="red">查询失败</Badge>
          </header>
          <p v-if="errors.cedar_ember" class="err"><TriangleAlert /> {{ errors.cedar_ember }}。没有发送任何消耗请求。</p>
          <template v-else-if="cedar">
            <p class="small">
              共 <b>{{ grants.length }}</b> 张 · 有资格：{{ yes(pick(cedar, 'eligible')) }} · 已撞限：{{ yes(pick(cedar, 'atLimit')) }}
              <template v-if="pick(cedar, 'ineligibleReason')"> · 原因：{{ claudeReason(String(pick(cedar, 'ineligibleReason'))) }}</template>
            </p>
            <p class="small faint">耗尽的窗口：{{ list(pick(cedar, 'exhausted')) }} · 冷却至：{{ pick(cedar, 'cooldownUntil') ? when(pick(cedar, 'cooldownUntil')) : '无' }} · 周重置：{{ when(pick(cedar, 'weeklyResetsAt')) }}</p>
            <ul v-if="grants.length" class="grants">
              <li v-for="g in grants" :key="String(pick(g, 'id'))" :class="{ next: pick(g, 'id') === pick(cedar, 'nextGrantId') }">
                <p class="gl">
                  <span class="truncate">{{ pick(g, 'label') || pick(g, 'id') }}</span>
                  <Badge v-if="pick(g, 'id') === pick(cedar, 'nextGrantId')" tone="blue">下一张</Badge>
                  <Badge v-if="pick(g, 'paused')" tone="orange">已暂停</Badge>
                  <span class="mono gr">剩 {{ pick(g, 'resetsLeft') ?? '?' }}/{{ pick(g, 'resetsTotal') ?? '?' }}</span>
                </p>
                <p class="faint small">
                  有效期 {{ when(pick(g, 'startsAt')) }} → {{ when(pick(g, 'endsAt')) }} · 可清除 {{ list(pick(g, 'clears')) }}
                  · {{ pick(g, 'useRequiresLimit') === false ? '随时可用' : '须撞限后才能用' }}
                </p>
              </li>
            </ul>
          </template>
          <p v-else-if="plans.cedar_ember" class="small faint">{{ missing('cedar_ember') }}</p>
          <p v-if="plans.cedar_ember && !plans.cedar_ember.available && plans.cedar_ember.reason !== USED" class="small c-orange">
            不可执行：{{ claudeReason(plans.cedar_ember.reason) || '原因未知' }}
          </p>
          <p v-if="plans.cedar_ember?.reason === USED" class="small faint">本次查询得到的确认已用掉；如需再次操作，请先「重新查询」。</p>
          <Btn v-if="plans.cedar_ember?.available" variant="danger" size="sm" :loading="executing === 'cedar_ember'" @click="execute('cedar_ember')">
            我已理解，使用周额度重置卡…
          </Btn>
        </section>

        <!-- 5h reset -->
        <section class="prog">
          <header>
            <p class="pt">5 小时重置 <span class="faint small">（消耗周额度）</span></p>
            <Badge v-if="plans.juniper_tide?.available" tone="green">可执行</Badge>
            <Badge v-else-if="plans.juniper_tide" tone="neutral">不可执行</Badge>
            <Badge v-else-if="errors.juniper_tide" tone="red">查询失败</Badge>
          </header>
          <p v-if="errors.juniper_tide" class="err"><TriangleAlert /> {{ errors.juniper_tide }}。没有发送任何消耗请求。</p>
          <template v-else-if="juniper">
            <p class="small">
              当前可用：{{ yes(pick(juniper, 'available')) }} · 有资格：{{ yes(pick(juniper, 'eligible')) }} · 每周 {{ pick(juniper, 'resetsPerWeek') ?? 1 }} 次
              <template v-if="pick(juniper, 'ineligibleReason')"> · 原因：{{ claudeReason(String(pick(juniper, 'ineligibleReason'))) }}</template>
            </p>
            <p class="small faint">下次可用：{{ when(pick(juniper, 'nextAvailableAt')) }} · 周重置：{{ when(pick(juniper, 'weeklyResetsAt')) }}</p>
          </template>
          <p v-else-if="plans.juniper_tide" class="small faint">{{ missing('juniper_tide') }}</p>
          <p v-if="plans.juniper_tide && !plans.juniper_tide.available && plans.juniper_tide.reason !== USED" class="small c-orange">
            不可执行：{{ claudeReason(plans.juniper_tide.reason) || '原因未知' }}
          </p>
          <p v-if="plans.juniper_tide?.reason === USED" class="small faint">本次查询得到的确认已用掉；如需再次操作，请先「重新查询」。</p>
          <Btn v-if="plans.juniper_tide?.available" variant="danger" size="sm" :loading="executing === 'juniper_tide'" @click="execute('juniper_tide')">
            我已理解，执行 5 小时重置…
          </Btn>
        </section>
      </div>
    </div>
    <template #footer>
      <Btn variant="ghost" @click="emit('close')">关闭</Btn>
      <Btn :loading="loading" :disabled="Boolean(executing)" @click="query"><RefreshCw /> 重新查询（不消耗）</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.wrap {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.explain {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-2);
}
.et {
  font-weight: 600;
  color: var(--text);
  margin-bottom: 2px;
}
.explain ul {
  margin: 0;
  padding-left: 18px;
}
.programs {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.prog {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  min-width: 0;
  padding: 12px;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border-strong);
}
.prog header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
}
.pt {
  font-size: 13px;
  font-weight: 600;
}
.grants {
  list-style: none;
  margin: 0;
  padding: 0;
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.grants li {
  padding: 6px 8px;
  border-radius: 8px;
  background: var(--fill-3);
}
.grants li.next {
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--blue) 50%, transparent);
}
.gl {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  min-width: 0;
}
.gr {
  margin-left: auto;
  font-size: 11.5px;
}
.err {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--red);
  word-break: break-word;
}
.err svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  margin-top: 2px;
}
.result {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--green) 10%, transparent);
}
.rt {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
}
.rt svg {
  width: 15px;
  height: 15px;
  color: var(--green);
}
.kv {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 3px 12px;
  margin: 6px 0;
  font-size: 12px;
}
.kv dt {
  color: var(--text-3);
}
.kv dd {
  margin: 0;
  word-break: break-word;
}
.small {
  font-size: 11.5px;
  line-height: 1.5;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
@media (max-width: 640px) {
  .programs {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
