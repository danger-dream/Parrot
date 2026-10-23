<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Building2, Gift, KeyRound, RefreshCw, RotateCcw } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { dateTime, relative } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import KeyValue from '@/components/ui/KeyValue.vue'
import SubBlock from '@/components/SubBlock.vue'
import ZhipuProjectModal from './ZhipuProjectModal.vue'
import { accountPath, errorToast, notifyChanged } from './oauthApi'
import { epoch, pick } from './meta'
import { errorTitle } from '@/utils/errors'

type View = Schemas['ZhipuViewData']
type Plan = Schemas['ZhipuPlanData']
type Action = Schemas['ZhipuActionData']
type Rec = Record<string, unknown>

/** Renders into the detail page's 额度 / 账号信息 / 维护 / 操作记录 slots (`scope` prefix). */
const props = defineProps<{ accountId: string; revision: string; scope: string; tick?: number }>()
const emit = defineEmits<{ changed: [] }>()

const view = ref<View | null>(null)
const error = ref('')
const checking = ref(false)
const acting = ref('')
const projectOpen = ref(false)

async function load(): Promise<void> {
  try {
    view.value = (await api.get<View>(accountPath(props.accountId, '/zhipu'))).data
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  }
}
onMounted(load)
watch([() => props.revision, () => props.tick], load)

const snap = computed(() => (view.value?.snapshot || {}) as Rec)
const isOAuth = computed(() => pick(snap.value, 'credentialMode') === 'oauth')
const reset = computed(() => {
  const r = (view.value?.reset || pick<Rec>(snap.value, 'reset') || null) as Rec | null
  return (pick<Rec>(r, 'data') || r || {}) as Rec
})
const resetFetched = computed(() => pick((view.value?.reset || pick<Rec>(snap.value, 'reset')) as Rec, 'fetchedAt'))
const cards = computed(() => ({
  five: ((pick<Rec[]>(reset.value, 'availableFiveHourResets') || []) as Rec[]).map((c) => pick(c, 'expireAt')),
  week: ((pick<Rec[]>(reset.value, 'availableWeekResets') || []) as Rec[]).map((c) => pick(c, 'expireAt')),
}))
const lastUsed = computed(() => ({
  five: pick(pick<Rec>(reset.value, 'latestFiveHourResetHistory'), 'usedAt'),
  week: pick(pick<Rec>(reset.value, 'latestWeekResetHistory'), 'usedAt'),
}))

const MGMT: Record<string, string> = {
  ready: '就绪',
  relogin_required: '管理登录已过期，请重新登录',
  key_pending: '模型 Key 待配置',
  model_key_rejected: '模型 Key 认证失败，请更新凭据',
  not_granted: '暂未获得',
  pending: '处理中',
}
const ACTION: Record<string, string> = { opportunity: '领取重置卡', use: '使用重置卡', create_key: '配置模型 Key' }
const STATUS: Record<string, string> = {
  succeeded: '已完成',
  completed: '已完成',
  done: '已完成',
  known: '已完成',
  not_submitted: '未提交',
  unknown: '结果待核对',
  pending: '处理中',
  key_pending: '已有 Key 待读取',
  rejected: '上游拒绝',
  not_granted: '暂未获得',
  cancelled: '已取消',
}
const actions = computed(() => ((view.value?.actions || []) as Rec[]).slice(0, 8))
const info = computed(() => {
  const sn = snap.value
  const org = pick(sn, 'organizationId')
  const project = pick(sn, 'projectId')
  const status = String(pick(sn, 'managementStatus') || '')
  return [
    { label: '站点', value: pick(sn, 'site') === 'zai' ? 'Z.ai 国际站' : 'BigModel 中国站' },
    { label: '登录方式', value: isOAuth.value ? 'OAuth 登录' : 'API Key' },
    { label: '管理状态', value: MGMT[status] || status || '未知', tone: status === 'ready' ? ('green' as const) : ('orange' as const) },
    { label: '模型 Key', value: pick(sn, 'modelKeyConfigured') ? '已配置' : '未配置', tone: pick(sn, 'modelKeyConfigured') ? undefined : ('orange' as const) },
    ...(org || project ? [{ label: '组织 / 项目', value: `${org || '—'} / ${project || '—'}`, mono: true, full: true, private: true }] : []),
  ]
})

async function checkReset(): Promise<void> {
  checking.value = true
  try {
    view.value = (await api.get<View>(accountPath(props.accountId, '/zhipu/reset-status'))).data
    toast('重置卡状态已更新', 'success')
  } catch (err) {
    errorToast(err)
  } finally {
    checking.value = false
  }
}

async function run(action: 'opportunity' | 'use' | 'create_key', resetType?: 'FIVE_HOUR' | 'WEEK', cardIndex = 0): Promise<void> {
  acting.value = action + (resetType || '')
  try {
    let plan: Plan
    try {
      plan = (await api.post<Plan>(accountPath(props.accountId, '/zhipu/action-plans'), { action, resetType: resetType || null, cardIndex })).data
    } catch (err) {
      errorToast(err)
      return
    }
    const lines: string[] = []
    if (plan.organizationId) lines.push(`组织/项目：${plan.organizationId} / ${plan.projectId || '—'}`)
    if (action === 'use') {
      lines.push(`将使用 ${resetType === 'WEEK' ? '周额度' : '5 小时额度'}重置卡第 ${plan.cardIndex + 1} 张${plan.expireAt ? `（${dateTime(epoch(plan.expireAt))} 到期）` : ''}。`)
      lines.push('提交后无法撤销，卡片会被消耗。')
    } else if (action === 'create_key') {
      lines.push(plan.resumeOnly ? '此前的操作需要核对：只查询并保存已有 Key，不会再次提交创建请求。' : '当前项目没有已有的 Key。确认后会在该项目创建一个模型 Key，并继续获取额度和模型。')
    } else {
      lines.push('尝试领取官方重置卡。领取后卡片保留，不会自动使用。')
    }
    const ok = await confirmAction({
      title: ACTION[action] + (resetType ? (resetType === 'WEEK' ? '（周额度）' : '（5 小时）') : ''),
      detail: lines.join('\n'),
      confirmText: action === 'use' ? '确认使用' : '确认',
      danger: action === 'use',
    })
    if (!ok) return
    try {
      const res = await api.post<Action>(accountPath(props.accountId, '/zhipu/actions/execute'), { planToken: plan.planToken })
      const s = res.data.status
      const good = ['succeeded', 'completed', 'done', 'known'].includes(s)
      toast(`${ACTION[action]}：${STATUS[s] || s}`, good ? 'success' : 'warning', s === 'unknown' ? '请求可能已经发出，请先查询核对，不要重复消费。' : undefined, 7000)
    } catch (err) {
      errorToast(err)
    }
    await load()
    notifyChanged(props.accountId)
    emit('changed')
  } finally {
    acting.value = ''
  }
}

function onProjectSaved(): void {
  projectOpen.value = false
  load()
  notifyChanged(props.accountId)
  emit('changed')
}
</script>

<template>
  <Teleport defer :to="`#${scope}-quota`">
    <p v-if="error" class="faint small">智谱状态加载失败：{{ error }}</p>
    <Skeleton v-else-if="!view" h="96px" r="10px" />
    <SubBlock v-else-if="isOAuth" title="官方重置卡" :note="resetFetched ? `查询于 ${relative(epoch(resetFetched) as string)}` : ''">
      <template #actions>
        <Btn variant="ghost" size="sm" :loading="checking" @click="checkReset"><RefreshCw /> 查询</Btn>
      </template>
      <div class="cards">
        <div class="card">
          <p class="k">5 小时额度卡</p>
          <p class="v mono">{{ cards.five.length }} 张</p>
          <p class="faint small">{{ cards.five[0] ? `最早 ${dateTime(epoch(cards.five[0]))} 到期` : '没有可用的卡' }}</p>
          <p v-if="lastUsed.five" class="faint small">上次使用 {{ dateTime(epoch(lastUsed.five)) }}</p>
        </div>
        <div class="card">
          <p class="k">7 天额度卡</p>
          <p class="v mono">{{ cards.week.length }} 张</p>
          <p class="faint small">{{ cards.week[0] ? `最早 ${dateTime(epoch(cards.week[0]))} 到期` : '没有可用的卡' }}</p>
          <p v-if="lastUsed.week" class="faint small">上次使用 {{ dateTime(epoch(lastUsed.week)) }}</p>
        </div>
      </div>
      <template #footer>
        <Btn size="sm" :loading="acting === 'opportunity'" @click="run('opportunity')"><Gift /> 领取重置卡</Btn>
        <Btn size="sm" :disabled="!cards.five.length" :loading="acting === 'useFIVE_HOUR'" @click="run('use', 'FIVE_HOUR')"><RotateCcw /> 用 5 小时卡</Btn>
        <Btn size="sm" :disabled="!cards.week.length" :loading="acting === 'useWEEK'" @click="run('use', 'WEEK')"><RotateCcw /> 用 7 天卡</Btn>
      </template>
    </SubBlock>
  </Teleport>

  <Teleport defer :to="`#${scope}-info`">
    <KeyValue v-if="view" :items="info" />
  </Teleport>

  <Teleport defer :to="`#${scope}-maint`">
    <template v-if="view && isOAuth">
      <Btn size="sm" :loading="acting === 'create_key'" @click="run('create_key')"><KeyRound /> {{ pick(snap, 'modelKeyConfigured') ? '重新配置模型 Key' : '配置模型 Key' }}</Btn>
      <Btn size="sm" @click="projectOpen = true"><Building2 /> 切换团队 / 项目</Btn>
    </template>
  </Teleport>

  <Teleport defer :to="`#${scope}-records`">
    <p v-if="!actions.length" class="faint small">还没有操作记录。</p>
    <ul v-else class="hist">
      <li v-for="(a, i) in actions" :key="i">
        <span>{{ ACTION[String(pick(a, 'action'))] || pick(a, 'action') }}</span>
        <Badge :tone="['rejected'].includes(String(pick(a, 'status'))) ? 'red' : String(pick(a, 'status')) === 'unknown' ? 'orange' : 'neutral'">
          {{ STATUS[String(pick(a, 'status'))] || pick(a, 'status') }}
        </Badge>
        <span class="faint when">{{ dateTime(epoch(pick(a, 'updatedAt') ?? pick(a, 'createdAt'))) }}</span>
      </li>
    </ul>
  </Teleport>

  <ZhipuProjectModal :open="projectOpen" :account-id="accountId" @close="projectOpen = false" @saved="onProjectSaved" />
</template>

<style scoped>
.cards {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.card {
  padding: 9px 11px;
  border-radius: var(--radius-sm);
  background: var(--surface);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.k {
  font-size: 11px;
  color: var(--text-3);
}
.v {
  margin: 1px 0 2px;
  font-size: 16px;
  font-weight: 650;
}
.hist {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.hist li {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
}
.hist li:last-child {
  border-bottom: 0;
}
.when {
  margin-left: auto;
  font-size: 11.5px;
}
.small {
  font-size: 11.5px;
}
</style>
