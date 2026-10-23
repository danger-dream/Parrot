<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ChevronLeft, ChevronRight, Gift } from '@lucide/vue'
import { api, ApiError, type Schemas } from '@/api/client'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { dateTime } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import KeyValue from '@/components/ui/KeyValue.vue'
import SettingRow from '@/components/SettingRow.vue'
import SubBlock from '@/components/SubBlock.vue'
import { accountPath, errorToast, fieldCode, notifyChanged, runOperation } from './oauthApi'
import { amount, epoch, num, pick } from './meta'
import { errorTitle } from '@/utils/errors'

type View = Schemas['WorkBuddyViewData']
type Plan = Schemas['WorkBuddyPlanData']
type Policy = Schemas['WorkBuddyPolicyData']
type Records = Schemas['WorkBuddyRecordPageData']
type Rec = Record<string, unknown>

/** Renders into the detail page's 设置 / 额度 / 账号信息 / 操作记录 slots (`scope` prefix). */
const props = defineProps<{ accountId: string; revision: string; scope: string; tick?: number }>()
const emit = defineEmits<{ changed: [] }>()

const view = ref<View | null>(null)
const policy = ref<Policy | null>(null)
const error = ref('')
const acting = ref('')
const savingAuto = ref(false)
const records = ref<Records | null>(null)
const recordsPage = ref(1)

async function load(): Promise<void> {
  try {
    view.value = (await api.get<View>(accountPath(props.accountId, '/workbuddy'))).data
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  }
}
onMounted(() => {
  load()
  loadRecords()
  api.get<Policy>('/oauth/workbuddy/settings').then((r) => (policy.value = r.data)).catch(() => undefined)
})
watch([() => props.revision, () => props.tick], load)

const snap = computed(() => (view.value?.snapshot || {}) as Rec)
const realm = computed(() => String(pick(snap.value, 'realm') || ''))
const packages = computed(() => (pick<Rec[]>(snap.value, 'packages') || []) as Rec[])
const pkgAll = ref(false)
const pkgEnd = computed(() => {
  const ends = packages.value.map((p) => pick(p, 'expiresAt')).filter((v) => v !== null && v !== undefined && v !== '') as (string | number)[]
  return ends.length ? ends.reduce((a, b) => (new Date(epoch(a) as string | number).getTime() <= new Date(epoch(b) as string | number).getTime() ? a : b)) : null
})
const checkin = computed(() => {
  const c = pick<Rec>(snap.value, 'checkin')
  return c && Object.keys(c).length ? c : (pick<Rec>(snap.value, 'lastSuccessCheckin') || {})
})
const checkinText = computed(() => {
  const c = checkin.value
  if (!Object.keys(c).length) return '尚未查询'
  if (pick(c, 'active') === false) return '当前活动未开放'
  const t = pick(c, 'todayCheckedIn')
  if (t === true) return '今日已签到'
  if (t === false) return '今日未签到'
  return '今日状态未知'
})
const info = computed(() => {
  const sn = snap.value
  return [
    { label: '账号类型', value: ACCOUNT_TYPE[String(pick(sn, 'workbuddyAccountType'))] || '未知' },
    { label: '区域', value: realm.value === 'cn' ? '中国区' : realm.value === 'global' ? '国际区' : '—' },
    ...(pick(sn, 'nickname') ? [{ label: '昵称', value: String(pick(sn, 'nickname')) }] : []),
    ...(pick(sn, 'paymentType') ? [{ label: '计费方式', value: String(pick(sn, 'paymentType')) }] : []),
    ...(pick(sn, 'fetchedAt') ? [{ label: '状态更新于', value: dateTime(epoch(pick(sn, 'fetchedAt'))) }] : []),
  ]
})
const ACCOUNT_TYPE: Record<string, string> = { personal: '个人', pro: '个人 Pro', enterprise: '企业版', exclusive: '专享版', private: '私有部署' }
const ERR_LABEL: Record<string, string> = { credits: '积分/资源包', pagination: '资源包分页', enterprise: '企业积分', checkin: '签到状态', payment_type: '计费类型', paymentType: '计费类型' }
const errors = computed(() => Object.entries((pick<Rec>(snap.value, 'errors') || {}) as Record<string, Rec>))
const effects = computed(() => pick(snap.value, 'effectsEnabled') !== false)
const autoCheckin = computed(() => pick(snap.value, 'autoCheckin') === true)
const schedule = computed(() => (policy.value?.autoCheckinTimes?.length ? policy.value.autoCheckinTimes.join('、') : '09:05、21:05'))



const RESULT: Record<string, string> = {
  succeeded: '已完成',
  success: '已完成',
  completed: '已完成',
  claimed: '已完成',
  already_done: '已完成（上游确认，未重复提交）',
  rejected: '上游明确拒绝，未获得积分',
  unknown: '结果未知，可能已提交；只会查询核对，不会自动重试',
  pending: '处理中',
  paused: '账号已停用或认证失效，活动暂停',
  not_submitted: '未提交',
  ineligible: '上游确认无资格',
  ended: '活动已结束',
  failed: '失败',
}
const ACTION_LABEL: Record<string, string> = { checkin: '签到', claim_trial: '申请试用额度' }

async function planAction(action: 'checkin' | 'claim_trial', flags: { allowUnknown?: boolean; freeTrialConfirmed?: boolean; retryFailed?: boolean } = {}): Promise<Plan | null> {
  try {
    return (await api.post<Plan>(accountPath(props.accountId, '/workbuddy/action-plans'), { action, ...flags })).data
  } catch (err) {
    const code = fieldCode(err)
    if (code === 'STATUS_UNKNOWN' && !flags.allowUnknown) {
      const ok = await confirmAction({
        title: '今日签到状态未知',
        detail: '查询不到今天是否已签到——这不等于未签到。建议先点「刷新状态」核对。\n如果你已在官方页面确认今天还没签到，可以接受重复提交的风险继续。',
        confirmText: '已核对，继续',
      })
      return ok ? planAction(action, { ...flags, allowUnknown: true }) : null
    }
    if (code === 'ACTIVITY_INACTIVE') {
      toast('当前活动未开放', 'info', '上游确认当前没有可参加的活动，未提交。')
      return null
    }
    if (err instanceof ApiError) errorToast(err)
    return null
  }
}

async function act(action: 'checkin' | 'claim_trial'): Promise<void> {
  acting.value = action
  try {
    const flags: { freeTrialConfirmed?: boolean } = {}
    if (action === 'claim_trial') {
      const ok = await confirmAction({
        title: '申请试用额度',
        detail: '请先核实这个账号当前的官方试用条件。这里只申请免费试用，不会购买或续费；额度和有效期以上游结果为准。',
        confirmText: '已核实免费条件，继续',
      })
      if (!ok) return
      flags.freeTrialConfirmed = true
    }
    let plan = await planAction(action, flags)
    if (!plan) return
    const prior = (plan.priorResult || null) as Rec | null
    const priorStatus = String(pick(prior, 'status') || '')
    if (priorStatus && ['succeeded', 'success', 'completed', 'claimed', 'already_done'].includes(priorStatus)) {
      toast(`今日已${ACTION_LABEL[action]}`, 'info', '已有成功记录，未重复提交。')
      return
    }
    if (priorStatus === 'rejected') {
      const ok = await confirmAction({ title: '上次被上游明确拒绝', detail: '要再尝试一次吗？这会是一次新的提交。', confirmText: '再试一次' })
      if (!ok) return
      plan = await planAction(action, { ...flags, allowUnknown: plan.allowUnknown, retryFailed: true })
      if (!plan) return
    }
    const ok = await confirmAction({
      title: `确认${ACTION_LABEL[action]}`,
      detail: `日期：${plan.businessDate === 'lifetime' ? '—' : plan.businessDate}\n请求发出后不能撤销；奖励只以上游明确返回的结果为准。`,
      confirmText: `确认${ACTION_LABEL[action]}`,
    })
    if (!ok) return
    const op = await runOperation(() => api.post(accountPath(props.accountId, '/workbuddy/actions/execute'), { planToken: plan!.planToken }), {
      failure: `${ACTION_LABEL[action]}失败`,
    })
    if (op?.status === 'succeeded') {
      const r = (op.result || {}) as Rec
      const status = String(pick(r, 'status') || '')
      const credits = num(pick(r, 'awardedCredits'))
      toast(`${ACTION_LABEL[action]}：${RESULT[status] || status || '已提交'}`, status === 'rejected' || status === 'unknown' ? 'warning' : 'success', credits ? `本次获得 ${amount(credits)} 积分` : undefined, 7000)
    }
    await load()
    loadRecords()
    emit('changed')
  } finally {
    acting.value = ''
  }
}

async function setAuto(on: boolean): Promise<void> {
  const ok = await confirmAction(
    on
      ? {
          title: '开启自动签到？',
          detail: `每天北京时间 ${schedule.value} 自动签到（仅中国区）。会先查询状态，已签到不重复提交，状态未知只核对。账号停用或认证失效时暂停，不会自动申请试用。`,
          confirmText: '开启',
        }
      : { title: '关闭自动签到？', detail: '不会撤销已经发出的请求。', confirmText: '关闭' },
  )
  if (!ok) return
  savingAuto.value = true
  try {
    await api.patch(accountPath(props.accountId, '/workbuddy/settings'), { autoCheckin: on }, { ifMatch: props.revision })
    toast('自动签到设置已保存', 'success')
    notifyChanged(props.accountId)
    await load()
  } catch (err) {
    errorToast(err)
  } finally {
    savingAuto.value = false
  }
}

async function loadRecords(): Promise<void> {
  try {
    records.value = (await api.get<Records>(accountPath(props.accountId, '/workbuddy/action-records'), { page: recordsPage.value, pageSize: 10 })).data
  } catch (err) {
    errorToast(err)
  }
}
function goRecords(d: number): void {
  recordsPage.value = Math.max(1, recordsPage.value + d)
  loadRecords()
}
</script>

<template>
  <Teleport defer :to="`#${scope}-settings`">
    <SettingRow v-if="view && realm !== 'global'" title="自动签到" :hint="`每天北京时间 ${schedule}；先查状态，已签不重复`">
      <Switch :model-value="autoCheckin" :loading="savingAuto" label="自动签到" @update:model-value="setAuto" />
    </SettingRow>
  </Teleport>

  <Teleport defer :to="`#${scope}-quota`">
    <p v-if="error" class="faint small">WorkBuddy 状态加载失败：{{ error }}</p>
    <Skeleton v-else-if="!view" h="80px" r="10px" />
    <template v-else>
      <p v-if="!effects" class="small c-orange">当前环境禁止上游网络请求，签到与试用申请不会提交。</p>
      <p v-for="[k, e] in errors" :key="k" class="small c-orange">{{ ERR_LABEL[k] || k }}查询失败{{ pick(e, 'authError') ? '：登录凭据已失效，请重新登录' : '' }}</p>
      <SubBlock v-if="packages.length" title="资源包" :note="`${packages.length} 个${pkgEnd ? ` · 最近 ${dateTime(epoch(pkgEnd))} 到期` : ''}`">
        <ul class="pkgs">
          <li v-for="(pk, i) in packages.slice(0, pkgAll ? packages.length : 4)" :key="String(pick(pk, 'id') || i)">
            <p class="pn truncate">{{ pick(pk, 'name') || '未命名资源包' }}</p>
            <p class="faint small mono">
              剩余 {{ amount(pick(pk, 'remaining')) }} / {{ amount(pick(pk, 'capacity')) }}
              <template v-if="pick(pk, 'expiresAt')"> · {{ dateTime(epoch(pick(pk, 'expiresAt'))) }} 到期</template>
              <template v-if="pick(pk, 'cycleEnd') && pick(pk, 'cycleEnd') !== pick(pk, 'expiresAt')"> · 本周期 {{ dateTime(epoch(pick(pk, 'cycleEnd'))) }} 结束</template>
            </p>
          </li>
        </ul>
        <button v-if="packages.length > 4" type="button" class="more" @click="pkgAll = !pkgAll">{{ pkgAll ? '收起' : `展开全部 ${packages.length} 个资源包` }}</button>
      </SubBlock>
      <SubBlock :title="realm === 'global' ? '试用额度' : '每日签到'">
        <p class="small">
          {{ realm === 'global' ? '国际区没有每日签到，可以申请试用额度。' : checkinText }}
          <span v-if="realm !== 'global' && pick(checkin, 'streakDays')" class="faint"> · 连续 {{ pick(checkin, 'streakDays') }} 天</span>
        </p>
        <template #footer>
          <Btn v-if="realm !== 'global'" size="sm" :loading="acting === 'checkin'" @click="act('checkin')"><Gift /> 立即签到</Btn>
          <Btn v-else size="sm" :loading="acting === 'claim_trial'" @click="act('claim_trial')"><Gift /> 申请试用额度</Btn>
        </template>
      </SubBlock>
    </template>
  </Teleport>

  <Teleport defer :to="`#${scope}-info`">
    <KeyValue v-if="view" :items="info" />
  </Teleport>

  <Teleport defer :to="`#${scope}-records`">
    <p v-if="!records" class="faint small">加载中…</p>
    <p v-else-if="!records.items.length" class="faint small">还没有执行记录。</p>
    <ul v-else class="records">
      <li v-for="(r, i) in records.items" :key="String(pick(r, 'id') || i)">
        <span>{{ ACTION_LABEL[String(pick(r, 'action'))] || pick(r, 'action') }}</span>
        <Badge :tone="['rejected', 'failed'].includes(String(pick(r, 'status'))) ? 'red' : String(pick(r, 'status')) === 'unknown' ? 'orange' : 'neutral'">
          {{ RESULT[String(pick(r, 'status'))] ? RESULT[String(pick(r, 'status'))].split('，')[0] : pick(r, 'status') }}
        </Badge>
        <span class="faint">{{ pick(r, 'source') === 'auto' ? '自动' : '手动' }}</span>
        <span v-if="num(pick(r, 'awardedCredits'))" class="mono">+{{ amount(pick(r, 'awardedCredits')) }}</span>
        <span class="faint when">{{ dateTime(epoch(pick(r, 'updatedAt') ?? pick(r, 'createdAt'))) }}</span>
      </li>
    </ul>
    <div v-if="records && (records.page > 1 || records.hasNext)" class="pager">
      <Btn icon size="sm" :disabled="records.page <= 1" @click="goRecords(-1)"><ChevronLeft /></Btn>
      <span class="faint small">第 {{ records.page }} 页</span>
      <Btn icon size="sm" :disabled="!records.hasNext" @click="goRecords(1)"><ChevronRight /></Btn>
    </div>
  </Teleport>
</template>

<style scoped>
.pkgs,
.records {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.pkgs li {
  padding: 6px 0;
  border-bottom: 0.5px solid var(--hairline);
}
.pkgs li:last-child,
.records li:last-child {
  border-bottom: 0;
}
.pn {
  font-size: 12.5px;
}
.records li {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
}
.when {
  margin-left: auto;
  font-size: 11.5px;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  padding-top: 6px;
}
.more {
  align-self: flex-start;
  font-size: 12px;
  color: var(--accent);
}
.small {
  font-size: 11.5px;
}
</style>
