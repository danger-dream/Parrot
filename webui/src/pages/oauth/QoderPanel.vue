<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Fingerprint, Gift } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import Btn from '@/components/ui/Btn.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import KeyValue from '@/components/ui/KeyValue.vue'
import SettingRow from '@/components/SettingRow.vue'
import SubBlock from '@/components/SubBlock.vue'
import { accountPath, errorToast, notifyChanged } from './oauthApi'
import { amount, num, pick } from './meta'
import { errorTitle } from '@/utils/errors'

type View = Schemas['QoderViewData']
type Checkin = Schemas['QoderCheckinData']
type Rec = Record<string, unknown>

/** Renders into the detail page's 设置 / 额度 / 账号信息 / 维护 slots (`scope` prefix). */
const props = defineProps<{ accountId: string; revision: string; scope: string; tick?: number }>()
const emit = defineEmits<{ changed: []; relogin: [realm: string | null] }>()

const view = ref<View | null>(null)
const error = ref('')
const checking = ref(false)
const savingAuto = ref(false)

async function load(): Promise<void> {
  try {
    view.value = (await api.get<View>(accountPath(props.accountId, '/qoder'))).data
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  }
}
onMounted(load)
watch([() => props.revision, () => props.tick], load)

const CHECKIN: Record<string, string> = {
  claimed: '已领取今日积分',
  already_done: '今日已领取',
  not_offered: '今日暂未开放',
  inactive: '当前无签到活动',
  unsupported: '当前账户不支持',
  failed: '签到失败',
}
const ERROR_KIND: Record<string, string> = {
  refresh_outcome_unknown_reauthorize: '令牌刷新结果不明，为安全起见不重放，请重新授权',
  reauthorize_required: '需要重新授权',
  device_unconfirmed: '请先确认设备档案并重新授权',
  upstream: '上游返回了错误',
  network: '网络连接失败',
  timeout: '请求超时',
  internal: '内部错误',
}
const DEVICE: Record<string, string> = {
  bound: '已绑定',
  initialization_required: '待初始化确认',
  migration_required: '待迁移确认',
  invalid_profile: '档案无效',
}
const device = computed(() => (view.value?.device || {}) as Rec)
const hardware = computed(() => (pick<Rec>(device.value, 'hardware') || {}) as Rec)
const hwModel = computed(() => ['manufacturer', 'model'].map((k) => pick(hardware.value, k)).filter(Boolean).join(' · '))
const hwCap = computed(() => {
  const parts: string[] = []
  for (const [k, label] of [['memoryBytes', '内存'], ['availableMemoryBytes', '可用内存'], ['storageBytes', '磁盘']] as const) {
    const v = num(pick(hardware.value, k))
    if (v && v > 0) parts.push(`${label} ${amount(v / 1024 ** 3, 1)} GiB`)
  }
  return parts.join(' · ')
})
const TICKET: Record<string, string> = { cached: '已缓存', backoff: '等待重试', not_cached: '未缓存', unavailable: '不可用' }
const PHASES: Record<string, string> = {
  intent: '刷新结果未确认，需要重新授权（不代表已吊销）',
  unknown: '刷新结果不明，为安全起见不重放，请重新授权',
  candidate: '已取得候选凭据，只会重试保存',
  store_unavailable: '安全存储不可用，暂不使用刷新凭据',
}
const refreshPhases = computed(() => Object.values((view.value?.refreshStatus || {}) as Record<string, string>).filter((v) => PHASES[v]).map((v) => PHASES[v]))
const checkin = computed(() => (view.value?.checkin || {}) as Rec)
const checkinText = computed(() => {
  const s = String(pick(checkin.value, 'status') || '')
  if (!s) return '尚未执行'
  const day = pick(checkin.value, 'businessDate')
  const kind = String(pick(checkin.value, 'errorKind') || '')
  return `${CHECKIN[s] || s}${day ? ` · ${day}` : ''}${s === 'failed' && kind ? `（${ERROR_KIND[kind] || '原因未知'}）` : ''}`
})
const info = computed(() => {
  const v = view.value
  if (!v) return []
  const d = device.value
  return [
    { label: '接入方式', value: (v.authMethod === 'pat' ? 'PAT 接入' : '设备授权') + (v.deviceChat ? ' · 设备 Token 对话' : '') },
    ...(v.nickname ? [{ label: '昵称', value: v.nickname }] : []),
    { label: '设备档案', value: DEVICE[String(pick(d, 'status'))] || String(pick(d, 'status') || '—'), tone: pick(d, 'status') === 'bound' ? undefined : ('orange' as const) },
    ...(pick(d, 'platformLabel') || pick(d, 'profileId') ? [{ label: '设备', value: [pick(d, 'platformLabel'), pick(d, 'profileId')].filter(Boolean).join(' · '), mono: true, private: true }] : []),
    ...(hwModel.value ? [{ label: '型号', value: [hwModel.value, pick(hardware.value, 'system'), pick(hardware.value, 'architecture')].filter(Boolean).join(' · ') }] : []),
    ...(hwCap.value ? [{ label: '硬件', value: hwCap.value }] : []),
    ...(pick(d, 'ticketStatus') ? [{ label: '票据', value: TICKET[String(pick(d, 'ticketStatus'))] || String(pick(d, 'ticketStatus')) }] : []),
    { label: '代理', value: proxyText.value },
  ]
})
const proxyText = computed(() => {
  const p = view.value?.proxy
  if (!p) return '继承现有规则'
  if (Array.isArray(p)) return p.join('、')
  return p === 'direct' ? '直连' : p
})


async function doCheckin(): Promise<void> {
  checking.value = true
  try {
    const r = (await api.post<Checkin>(accountPath(props.accountId, '/qoder/actions/checkin'))).data
    const good = r.status === 'claimed' || r.status === 'already_done'
    const why = r.errorKind ? ERROR_KIND[r.errorKind] || '原因未知' : undefined
    toast(CHECKIN[r.status] || r.status, good ? 'success' : 'warning', r.credit ? `获得 ${amount(r.credit)} 积分` : why, 7000)
    await load()
  } catch (err) {
    errorToast(err)
  } finally {
    checking.value = false
  }
}

async function setAuto(on: boolean): Promise<void> {
  if (on) {
    const ok = await confirmAction({
      title: '开启每日自动签到？',
      detail: '每个北京日领取一次，之后每 30 分钟核对是否有新活动；账号暂停或认证失效时跳过。',
      confirmText: '开启',
    })
    if (!ok) return
  }
  savingAuto.value = true
  try {
    await api.patch(accountPath(props.accountId, '/qoder/settings'), { autoCheckin: on }, { ifMatch: props.revision })
    toast(on ? '已开启每日自动签到' : '已关闭每日自动签到', 'success')
    notifyChanged(props.accountId)
    await load()
  } catch (err) {
    errorToast(err)
  } finally {
    savingAuto.value = false
  }
}
</script>

<template>
  <Teleport defer :to="`#${scope}-settings`">
    <SettingRow v-if="view" title="每日自动签到" :hint="view.checkinSupported ? '每个北京日领取一次；账号暂停或认证失效时跳过' : view.checkinUnsupportedReason || '当前账户不支持签到'">
      <Switch :model-value="view.autoCheckin" :loading="savingAuto" :disabled="!view.checkinSupported" label="每日自动签到" @update:model-value="setAuto" />
    </SettingRow>
  </Teleport>

  <Teleport defer :to="`#${scope}-quota`">
    <p v-if="error" class="faint small">Qoder 状态加载失败：{{ error }}</p>
    <Skeleton v-else-if="!view" h="64px" r="10px" />
    <SubBlock v-else title="每日签到">
      <p class="small">
        {{ view.checkinSupported ? checkinText : view.checkinUnsupportedReason || '当前账户不支持' }}
        <span v-if="num(pick(checkin, 'credit'))" class="mono"> · +{{ amount(pick(checkin, 'credit')) }} 积分</span>
      </p>
      <template #footer>
        <Btn size="sm" :loading="checking" :disabled="!view.checkinSupported" @click="doCheckin"><Gift /> 立即签到</Btn>
      </template>
    </SubBlock>
  </Teleport>

  <Teleport defer :to="`#${scope}-info`">
    <template v-if="view">
      <p v-if="view.reloginRequired" class="small c-red">刷新结果未确认，需要重新授权（不代表凭据已被吊销）。</p>
      <p v-for="(ph, k) in refreshPhases" :key="k" class="small c-orange">{{ ph }}</p>
      <KeyValue :items="info" />
    </template>
  </Teleport>

  <Teleport defer :to="`#${scope}-maint`">
    <Btn v-if="view" size="sm" @click="emit('relogin', view.realm || null)"><Fingerprint /> 设备档案 / 重新授权</Btn>
  </Teleport>
</template>

<style scoped>
.small {
  font-size: 11.5px;
}
</style>
