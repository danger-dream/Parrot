<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ArrowLeft, CircleCheck, ExternalLink, FileUp, KeyRound, LogIn, RefreshCw, TriangleAlert } from '@lucide/vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import CopyText from '@/components/ui/CopyText.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import TextArea from '@/components/ui/TextArea.vue'
import TextInput from '@/components/ui/TextInput.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { IdentityConflict, errorLine, errorToast, notifyChanged, postKeepingConflict } from './oauthApi'
import { pick, PROVIDER_ORDER, providerMeta, type LoginFlow, type LoginPoll, type Provider } from './meta'

type Mutation = Schemas['OAuthMutationData']
type Method = 'login' | 'json' | 'refreshToken' | 'manual'

const props = defineProps<{
  open: boolean
  /**
   * Re-authorise an existing account (every platform; the account keeps its routing, stats and
   * settings). Zhipu needs its site; WorkBuddy/Qoder realms are restored by the server.
   */
  relogin?: { accountId: string; provider: Provider; realm?: string | null; site?: string | null } | null
}>()
const emit = defineEmits<{ close: []; created: [accountId: string | null]; import: [] }>()

const step = ref<'pick' | 'options' | 'flow' | 'manual' | 'done'>('pick')
const provider = ref<Provider>('claude')
const method = ref<Method>('login')
const realm = ref<'cn' | 'global'>('cn')
const site = ref<'bigmodel' | 'zai'>('bigmodel')
const qoderMethod = ref<'device' | 'pat'>('device')
const proxyTarget = ref<string>('')
const proxies = ref<string[]>([])
const proxyGroups = ref<{ name: string; members: string[] }[]>([])
const proxiesLoaded = ref(false)

const flow = ref<LoginFlow | null>(null)
const poll = ref<LoginPoll | null>(null)
const pasted = ref('')
const pat = ref('')
const platform = ref<string>('')
const busy = ref(false)
const flowError = ref('')
const result = ref<{ accountId: string; status: string } | null>(null)
const now = ref(Date.now())

// manual credential form
const json = ref('')
const refreshToken = ref('')
const emailHint = ref('')
const manual = ref({ email: '', accessToken: '', refreshToken: '', displayName: '' })

let pollTimer: number | undefined
let tick: number | undefined

function methodsFor(p: Provider): { value: Method; label: string }[] {
  if (p === 'workbuddy' || p === 'qoder') return [{ value: 'login', label: '网页登录' }]
  if (p === 'zhipu') return [
    { value: 'login', label: '网页登录' },
    { value: 'json', label: '粘贴账户 JSON' },
  ]
  return [
    { value: 'login', label: '网页登录' },
    { value: 'refreshToken', label: '粘贴 Refresh Token' },
    { value: 'json', label: '粘贴 JSON' },
    { value: 'manual', label: '手动填写令牌' },
  ]
}

function reset(): void {
  stopPolling()
  step.value = 'pick'
  method.value = 'login'
  flow.value = null
  poll.value = null
  pasted.value = ''
  pat.value = ''
  platform.value = ''
  flowError.value = ''
  result.value = null
  json.value = ''
  refreshToken.value = ''
  emailHint.value = ''
  manual.value = { email: '', accessToken: '', refreshToken: '', displayName: '' }
}

watch(
  () => props.open,
  (o) => {
    if (!o) {
      stopPolling()
      window.clearInterval(tick)
      return
    }
    reset()
    tick = window.setInterval(() => (now.value = Date.now()), 1000)
    if (props.relogin) {
      provider.value = props.relogin.provider
      realm.value = props.relogin.realm === 'cn' ? 'cn' : props.relogin.realm === 'global' ? 'global' : props.relogin.provider === 'qoder' ? 'global' : 'cn'
      site.value = props.relogin.site === 'zai' ? 'zai' : 'bigmodel'
      startFlow()
    }
  },
  { immediate: true },
)
onBeforeUnmount(() => {
  stopPolling()
  window.clearInterval(tick)
})

function choose(p: Provider): void {
  provider.value = p
  method.value = 'login'
  realm.value = p === 'qoder' ? 'global' : 'cn'
  qoderMethod.value = 'device'
  step.value = 'options'
  loadProxies()
}

/** Proxy choices for a new account: names of proxies and proxy groups (the values TG offers). */
function loadProxies(): void {
  if (proxiesLoaded.value) return
  proxiesLoaded.value = true
  api
    .get<{ name: string }[]>('/proxies')
    .then((r) => (proxies.value = r.data.map((x) => x.name)))
    .catch(() => (proxies.value = []))
  api
    .get<{ name: string; members?: string[] }[]>('/proxy-groups')
    .then((r) => (proxyGroups.value = r.data.map((g) => ({ name: g.name, members: g.members || [] }))))
    .catch(() => (proxyGroups.value = []))
}
const proxyOptions = computed(() => [
  { value: '', label: '继承现有规则' },
  { value: 'direct', label: '直连' },
  ...proxyGroups.value.map((g) => ({ value: g.name, label: `代理组 · ${g.name}${g.members.length ? `（${g.members.join('、')}）` : ''}` })),
  ...proxies.value.filter((p) => p !== 'direct').map((p) => ({ value: p, label: `代理 · ${p}` })),
])

const isDeviceFlow = computed(() => Boolean(flow.value && /^(wbflow|zhflow|qdflow)_/.test(flow.value.flowId)))
/** Claude / OpenAI / Grok / Cursor / Antigravity browser flows (oflow_*): pollable and cancellable. */
const isBrowserFlow = computed(() => Boolean(flow.value && !isDeviceFlow.value))
const preview = computed(() => (flow.value?.devicePreview || null) as Record<string, unknown> | null)
const phase = computed(() => String(pick(preview.value, 'phase') || ''))
const qoderPreparing = computed(() => provider.value === 'qoder' && flow.value && phase.value === 'preparing')
const choices = computed(() => {
  const list = pick<Record<string, unknown>[]>(poll.value?.accountPreview, 'choices')
  return Array.isArray(list) ? list : []
})
const expiresIn = computed(() => {
  if (!flow.value) return ''
  const s = Math.round((new Date(flow.value.expiresAt).getTime() - now.value) / 1000)
  if (s <= 0) return '已过期'
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')} 后过期`
})

/** Where the browser lands after login decides what the user has to paste back. */
const redirect = computed(() => {
  try {
    const u = new URL(flow.value?.authUrl || '')
    return u.searchParams.get('redirect_uri') || ''
  } catch {
    return ''
  }
})
const pasteMode = computed<'url' | 'code' | 'none'>(() => {
  const p = provider.value
  if (!['claude', 'openai', 'xai', 'antigravity'].includes(p)) return 'none'
  if (p === 'claude') return 'code'
  if (p === 'antigravity' && /code/i.test(flow.value?.instruction || '')) return 'code'
  return 'url'
})

const INSTRUCTIONS: Record<string, string> = {
  workbuddy: '在浏览器里用对应账号登录并授权。授权完成后这里会自动检测并保存账号，无需粘贴任何内容。同一身份重新登录只更新授权，原有设置保留。',
  zhipu: '打开官方授权页完成授权，这里会自动检测并保存账号、配置专用模型 Key 并同步额度与模型，无需粘贴回调地址。',
  qoder: '打开授权页面登录并授权此设备，这里每 3 秒自动检查一次，成功后自动保存。浏览器授权走浏览器自己的网络，不受 Parrot 代理设置影响。',
  cursor: '在浏览器完成 Cursor 登录后，回到这里点「我已登录」。Parrot 会自动获取令牌、套餐额度和可用模型。',
}

const POLL_TEXT: Record<string, string> = {
  preparing: '等待确认设备档案',
  confirmed: '设备档案已确认，等待浏览器授权…',
  pending: '等待浏览器授权，正在自动检查…',
  identity_pending: '已取得授权，正在确认账户身份…',
  select_project: '请选择要使用的组织 / 项目',
  ready: '授权完成，正在保存账号…',
  completed: '账号已保存',
  cancelled: '登录已取消',
  expired: '登录链接已过期，请重新开始',
  failed: '登录失败，请重新开始',
}
/** Status of a browser (oflow_) flow, polled while the dialog is open. */
const BROWSER_STATUS: Record<string, { text: string; tone: 'blue' | 'green' | 'orange' | 'red' | 'neutral' }> = {
  pending: { text: '等待授权', tone: 'blue' },
  confirmed: { text: '已收到授权，正在保存', tone: 'blue' },
  completed: { text: '已完成', tone: 'green' },
  failed: { text: '登录失败', tone: 'red' },
  cancelled: { text: '已取消', tone: 'neutral' },
  expired: { text: '已过期', tone: 'orange' },
}
const BROWSER_END: Record<string, string> = {
  failed: '这次登录失败了，请点「重新生成链接」再试一次。',
  cancelled: '这次登录已取消，请点「重新生成链接」重新开始。',
  expired: '登录链接已过期，请点「重新生成链接」重新开始。',
}

async function startFlow(): Promise<void> {
  // Regenerating: retire the previous one-shot flow on the server first.
  if (flow.value && !result.value) await cancelFlow()
  busy.value = true
  flowError.value = ''
  poll.value = null
  pasted.value = ''
  try {
    const p = provider.value
    const body: Record<string, unknown> = { provider: p }
    if (p === 'workbuddy') body.realm = realm.value
    if (p === 'zhipu') body.site = site.value
    if (p === 'qoder') {
      body.realm = qoderMethod.value === 'pat' && !props.relogin ? 'global' : realm.value
      if (!props.relogin) body.loginMethod = qoderMethod.value
    }
    // A relogin keeps the account's routing; proxy selection only applies to new accounts.
    if (props.relogin) body.reloginAccountId = props.relogin.accountId
    else if (proxyTarget.value) body.proxyTarget = proxyTarget.value
    const res = await api.post<LoginFlow>('/oauth/login-flows', body)
    flow.value = res.data
    platform.value = String(pick(res.data.devicePreview, 'platformId') || '')
    step.value = 'flow'
    if (isDeviceFlow.value && !qoderPreparing.value) schedulePoll(1500)
    else if (isBrowserFlow.value) schedulePoll(3000)
  } catch (err) {
    if (props.relogin) {
      step.value = 'flow'
      flowError.value = errorLine(err)
    } else errorToast(err)
  } finally {
    busy.value = false
  }
}

function stopPolling(): void {
  window.clearTimeout(pollTimer)
  pollTimer = undefined
}
function schedulePoll(ms = 3000): void {
  stopPolling()
  pollTimer = window.setTimeout(doPoll, ms)
}

async function doPoll(): Promise<void> {
  const f = flow.value
  if (!f || !props.open) return
  try {
    const res = await api.post<LoginPoll>(`/oauth/login-flows/${enc(f.flowId)}/poll`, { flowSecret: f.flowSecret })
    if (flow.value?.flowId !== f.flowId || result.value) return
    poll.value = res.data
    const s = res.data.status
    if (isBrowserFlow.value) {
      // Browser flows complete through the pasted code / 「我已登录」; polling reports progress.
      if (s === 'completed' && res.data.accountId) {
        finish({ accountId: res.data.accountId, revision: res.data.revision || '', status: res.data.saveStatus || 'created' })
        return
      }
      if (BROWSER_END[s]) {
        // Keep the reason of a failed submission; just add that this login is over.
        flowError.value = flowError.value ? `${flowError.value} 这次登录已结束，请点「重新生成链接」重新开始。` : BROWSER_END[s]
        return
      }
      schedulePoll(3000)
      return
    }
    if (s === 'ready' || s === 'completed') {
      await complete({ completed: true })
      return
    }
    if (['cancelled', 'expired', 'failed'].includes(s)) {
      flowError.value = POLL_TEXT[s]
      return
    }
    if (s === 'select_project') return
    schedulePoll(provider.value === 'workbuddy' ? 5000 : 3000)
  } catch (err) {
    if (flow.value?.flowId !== f.flowId) return
    flowError.value = errorLine(err)
  }
}

async function complete(extra: Record<string, unknown>, replacePlanToken?: string): Promise<void> {
  const f = flow.value
  if (!f) return
  busy.value = true
  try {
    const body: Record<string, unknown> = { flowSecret: f.flowSecret, ...extra }
    if (replacePlanToken) body.replacePlanToken = replacePlanToken
    const data = await postKeepingConflict<Mutation>(`/oauth/login-flows/${enc(f.flowId)}/complete`, body)
    finish(data)
  } catch (err) {
    const identityMismatch = props.relogin && (err instanceof IdentityConflict || (err instanceof ApiError && err.code === 'IDENTITY_CONFLICT'))
    if (identityMismatch) {
      flowError.value = '登录的不是原来那个账号（身份不一致），原账号没有被修改。请用原账号重新登录。'
    } else if (err instanceof IdentityConflict) {
      const ok = await confirmOverwrite(err.accountId)
      if (ok) {
        busy.value = false
        await complete(extra, err.replacePlanToken)
        return
      }
      flowError.value = '已取消覆盖，现有账号未作任何修改。'
    } else if (isBrowserFlow.value && provider.value !== 'cursor' && err instanceof ApiError && err.code === 'STATE_CONFLICT') {
      // The server's state check: the pasted callback / code belongs to another login.
      flowError.value = '提交的内容和这次登录对不上（state 不一致）。请确认粘贴的是用本次链接登录后得到的内容，或点「重新生成链接」。'
    } else {
      flowError.value = errorLine(err)
    }
    // A failed submission may end the one-shot flow; refresh its status right away.
    if (isBrowserFlow.value && !result.value) schedulePoll(300)
  } finally {
    busy.value = false
  }
}

function confirmOverwrite(accountId: string): Promise<boolean> {
  return confirmAction({
    title: '已存在相同身份的账号',
    detail: `账号 ${accountId} 已经在账号池里。\n是否用本次登录的凭据覆盖它？账号的统计、调度顺序和设置都会保留。\n取消则不修改现有账号。`,
    confirmText: '覆盖',
  })
}

function finish(data: Mutation): void {
  if (result.value) return
  stopPolling()
  result.value = { accountId: data.accountId, status: data.status }
  step.value = 'done'
  notifyChanged()
  toast(props.relogin ? '账号已重新登录' : data.status === 'replaced' ? '账号已覆盖更新' : '账号已添加', 'success')
}

function submitPasted(): void {
  const v = pasted.value.trim()
  if (!v) return
  complete(/^https?:\/\//i.test(v) ? { callbackUrl: v } : { code: v })
}

async function cancelFlow(): Promise<void> {
  const f = flow.value
  stopPolling()
  const s = poll.value?.flowId === f?.flowId ? poll.value?.status || '' : ''
  if (f && !result.value && !['completed', 'cancelled', 'expired', 'failed'].includes(s)) {
    try {
      await api.post(`/oauth/login-flows/${enc(f.flowId)}/cancel`, { flowSecret: f.flowSecret })
    } catch {
      /* completed meanwhile (400) — nothing to undo */
    }
  }
}

async function back(): Promise<void> {
  await cancelFlow()
  flow.value = null
  poll.value = null
  flowError.value = ''
  step.value = 'options'
}

async function close(): Promise<void> {
  await cancelFlow()
  if (result.value) emit('created', result.value.accountId)
  emit('close')
}

async function openResult(): Promise<void> {
  const id = result.value?.accountId || null
  result.value = null
  emit('created', id)
  emit('close')
}

// ---- Qoder device profile ----
const platformOptions = computed(() => {
  const list = pick<{ id: string; label: string; supported?: boolean }[]>(preview.value, 'platformChoices') || []
  return list.map((c) => ({ value: c.id, label: c.label, disabled: c.supported === false }))
})
const hardware = computed(() => (pick<Record<string, unknown>>(preview.value, 'hardware') || {}) as Record<string, unknown>)
function gb(v: unknown): string {
  return typeof v === 'number' ? `${Math.round(v / 1024 ** 3)} GB` : '—'
}
async function regenerate(): Promise<void> {
  const f = flow.value
  if (!f) return
  busy.value = true
  try {
    const res = await api.post<LoginFlow>(`/oauth/login-flows/${enc(f.flowId)}/qoder-device/regenerate`, {
      flowSecret: f.flowSecret,
      revision: String(pick(preview.value, 'revision') || ''),
      platform: platform.value || null,
    })
    flow.value = res.data
  } catch (err) {
    errorToast(err)
  } finally {
    busy.value = false
  }
}
async function confirmDevice(): Promise<void> {
  const f = flow.value
  if (!f) return
  busy.value = true
  flowError.value = ''
  try {
    const body: Record<string, unknown> = { flowSecret: f.flowSecret, revision: String(pick(preview.value, 'revision') || '') }
    if (qoderMethod.value === 'pat' && !props.relogin) body.pat = pat.value.trim()
    const res = await api.post<LoginFlow>(`/oauth/login-flows/${enc(f.flowId)}/qoder-device/confirm`, body)
    flow.value = res.data
    schedulePoll(800)
  } catch (err) {
    flowError.value = errorLine(err)
  } finally {
    busy.value = false
  }
}

// ---- Zhipu project selection ----
async function selectProject(c: Record<string, unknown>): Promise<void> {
  const f = flow.value
  if (!f) return
  busy.value = true
  try {
    const data = await postKeepingConflict<Mutation>(`/oauth/login-flows/${enc(f.flowId)}/zhipu/project`, {
      flowSecret: f.flowSecret,
      organizationId: String(pick(c, 'organizationId')),
      projectId: String(pick(c, 'projectId')),
    })
    finish(data)
  } catch (err) {
    flowError.value = errorLine(err)
  } finally {
    busy.value = false
  }
}

// ---- direct credentials ----
const manualValid = computed(() => {
  if (method.value === 'json') return json.value.trim().length >= 2
  if (method.value === 'refreshToken') return refreshToken.value.trim().length >= 20
  if (method.value === 'manual') return Boolean(manual.value.email.trim() && manual.value.accessToken.trim() && manual.value.refreshToken.trim())
  return false
})

function credentialBody(): Record<string, unknown> {
  const p = provider.value
  if (method.value === 'json') return { kind: 'json', provider: p, payload: json.value.trim() }
  if (method.value === 'refreshToken') return { kind: 'refreshToken', provider: p, refreshToken: refreshToken.value.trim(), emailHint: emailHint.value.trim() || null }
  const m = manual.value
  return {
    kind: 'manual',
    provider: p,
    email: m.email.trim(),
    accessToken: m.accessToken.trim(),
    refreshToken: m.refreshToken.trim(),
    displayName: m.displayName.trim() || null,
  }
}

async function createDirect(replacePlanToken?: string): Promise<void> {
  busy.value = true
  try {
    const body: Record<string, unknown> = { credential: credentialBody() }
    if (replacePlanToken) body.replacePlanToken = replacePlanToken
    const data = await postKeepingConflict<Mutation>('/oauth/accounts', body)
    finish(data)
  } catch (err) {
    if (err instanceof IdentityConflict) {
      const ok = await confirmOverwrite(err.accountId)
      busy.value = false
      if (ok) await createDirect(err.replacePlanToken)
      return
    }
    errorToast(err)
  } finally {
    busy.value = false
  }
}

function next(): void {
  if (method.value === 'login') startFlow()
  else step.value = 'manual'
}

const title = computed(() => {
  if (props.relogin) return `${providerMeta(provider.value).label} 重新登录`
  if (step.value === 'pick') return '新增账号'
  return `新增 ${providerMeta(provider.value).label} 账号`
})
const JSON_HINT: Record<string, string> = {
  zhipu: '必填 site=bigmodel|zai、credential_mode=api_key|oauth；API Key 模式填 model_key；OAuth 模式填 subject / access_token / zcode_token。团队账号另填 plan_scope=team、organization_id、project_id。',
}
</script>

<template>
  <Modal :open="open" :title="title" :subtitle="relogin ? '用原账号重新授权：凭据更新后，统计、调度顺序、代理和设置都保留' : step === 'pick' ? '选择要登录的平台' : providerMeta(provider).hint" width="md" :dismissible="!busy" @close="close">
    <!-- 1. platform -->
    <div v-if="step === 'pick'" class="providers">
      <button v-for="p in PROVIDER_ORDER" :key="p" type="button" class="prov" @click="choose(p)">
        <ProviderIcon :provider="p" :size="32" />
        <span class="pl">{{ providerMeta(p).label }}</span>
        <span class="ph">{{ providerMeta(p).hint }}</span>
      </button>
    </div>

    <!-- 2. options -->
    <div v-else-if="step === 'options'" class="form">
      <Field label="添加方式">
        <div class="seg-wrap"><Segmented v-model="method" :options="methodsFor(provider)" size="sm" /></div>
      </Field>
      <template v-if="method === 'login'">
        <Field v-if="provider === 'workbuddy'" label="账号区域" hint="中国区使用国内账号；国际区使用 Google / GitHub 登录。">
          <Segmented
            v-model="realm"
            :options="[
              { value: 'cn', label: '中国区' },
              { value: 'global', label: '国际区' },
            ]"
            size="sm"
          />
        </Field>
        <Field v-if="provider === 'zhipu'" label="站点">
          <Segmented
            v-model="site"
            :options="[
              { value: 'bigmodel', label: 'BigModel 中国站' },
              { value: 'zai', label: 'Z.ai 国际站' },
            ]"
            size="sm"
          />
        </Field>
        <template v-if="provider === 'qoder'">
          <Field label="登录方式" hint="国际区（qoder.com）支持 Google / GitHub / 邮箱，可用浏览器授权或 PAT；中国区（qoder.cn）仅支持浏览器授权。">
            <Segmented
              v-model="qoderMethod"
              :options="[
                { value: 'device', label: '浏览器授权' },
                { value: 'pat', label: 'PAT（仅国际区）' },
              ]"
              size="sm"
            />
          </Field>
          <Field v-if="qoderMethod === 'device'" label="账号区域">
            <Segmented
              v-model="realm"
              :options="[
                { value: 'global', label: '国际区' },
                { value: 'cn', label: '中国区' },
              ]"
              size="sm"
            />
          </Field>
        </template>
        <Field label="本次新增使用的代理" hint="只影响 Parrot 服务端的请求（换取令牌、刷新、调用），浏览器出口不受控制。登录成功后，这里的选择会保存为该账号的代理；最好不要多个账号共用同一出口。">
          <Select v-model="proxyTarget" :options="proxyOptions" />
        </Field>
        <p v-if="INSTRUCTIONS[provider]" class="note">{{ INSTRUCTIONS[provider] }}</p>
      </template>
      <p v-else-if="method === 'json'" class="note">{{ JSON_HINT[provider] || '粘贴包含 email、access_token、refresh_token 的账号 JSON。' }}</p>
      <p v-else-if="method === 'refreshToken'" class="note">已经有 refresh token 时直接粘贴，Parrot 会自动换取令牌并识别账号信息。</p>
      <button v-if="provider === 'openai'" type="button" class="link" @click="emit('import')"><FileUp /> 有 CPA / Sub2API 导出文件？改用批量导入</button>
    </div>

    <!-- 2b. direct credentials -->
    <div v-else-if="step === 'manual'" class="form">
      <template v-if="method === 'json'">
        <Field label="账号 JSON" :hint="JSON_HINT[provider]">
          <TextArea v-model="json" :rows="9" mono placeholder='{"email": "...", "access_token": "...", "refresh_token": "..."}' />
        </Field>
      </template>
      <template v-else-if="method === 'refreshToken'">
        <Field label="Refresh Token" required>
          <TextArea v-model="refreshToken" :rows="4" mono placeholder="粘贴 refresh token" />
        </Field>
        <Field label="邮箱（可选）" hint="帮助识别账号；不填会从令牌里自动解析。">
          <TextInput v-model="emailHint" type="email" placeholder="name@example.com" />
        </Field>
      </template>
      <template v-else>
        <Field label="邮箱" required><TextInput v-model="manual.email" type="email" placeholder="name@example.com" /></Field>
        <Field label="Access Token" required><TextInput v-model="manual.accessToken" type="password" mono /></Field>
        <Field label="Refresh Token" required><TextInput v-model="manual.refreshToken" type="password" mono /></Field>
        <Field label="显示名称（可选）"><TextInput v-model="manual.displayName" placeholder="默认使用邮箱" /></Field>
      </template>
    </div>

    <!-- 3. login flow -->
    <div v-else-if="step === 'flow'" class="form">
      <p v-if="flowError" class="alert"><TriangleAlert /> {{ flowError }}</p>

      <!-- Qoder device profile draft -->
      <template v-if="flow && qoderPreparing">
        <div class="box">
          <p class="bt">设备档案草稿</p>
          <p class="small faint">仅在本地生成，尚未登录。保存账号后会长期绑定这个设备档案，重新授权时沿用。</p>
          <dl class="kv">
            <dt>档案 ID</dt><dd class="mono">{{ pick(preview, 'profileId') || '—' }}</dd>
            <dt>平台</dt><dd>{{ pick(preview, 'platformLabel') || '—' }}</dd>
            <dt>型号</dt><dd>{{ hardware.manufacturer || '' }} {{ hardware.model || '' }}</dd>
            <dt>系统</dt><dd>{{ hardware.system || '—' }}</dd>
            <dt>内存 / 磁盘</dt><dd>{{ gb(pick(hardware, 'memoryBytes')) }} / {{ gb(pick(hardware, 'storageBytes')) }}</dd>
            <dt>代理</dt><dd>{{ pick(preview, 'proxy') === 'direct' ? '直连' : pick(preview, 'proxy') || '继承现有规则' }}</dd>
          </dl>
          <p v-if="pick(preview, 'migration')" class="small c-orange">旧档案需要修正；确认并重新授权成功后保存，机器 ID 与身份字段保持不变。</p>
          <div v-if="pick(preview, 'canRegenerate')" class="row">
            <Select v-model="platform" :options="platformOptions" width="220px" />
            <Btn size="sm" :loading="busy" @click="regenerate"><RefreshCw /> 重新生成</Btn>
          </div>
        </div>
        <Field v-if="qoderMethod === 'pat' && !relogin" label="PAT" hint="以 pt- 开头，在 qoder.com/account/integrations 创建。" required>
          <TextInput v-model="pat" type="password" mono placeholder="pt-..." />
        </Field>
        <p class="note">{{ flow.instruction }}</p>
      </template>

      <template v-else-if="flow">
        <div v-if="flow.authUrl" class="box">
          <p class="bt">
            <span>1. 打开授权页面</span>
            <span class="bt-r">
              <Badge v-if="isBrowserFlow && poll" :tone="BROWSER_STATUS[poll.status]?.tone || 'neutral'" dot>{{ BROWSER_STATUS[poll.status]?.text || poll.status }}</Badge>
              <span class="faint exp">{{ expiresIn }}</span>
            </span>
          </p>
          <div class="url"><CopyText :text="flow.authUrl" /></div>
          <a :href="flow.authUrl" target="_blank" rel="noopener noreferrer" class="open"><ExternalLink /> 在新标签页打开</a>
        </div>
        <p v-if="INSTRUCTIONS[provider]" class="note">{{ INSTRUCTIONS[provider] }}</p>

        <!-- paste back -->
        <template v-if="pasteMode !== 'none'">
          <Field
            :label="pasteMode === 'code' ? '2. 粘贴授权码' : '2. 粘贴跳转后的完整网址'"
            :hint="
              pasteMode === 'code'
                ? '登录并授权后页面会显示一段授权码（例如 abc#xyz），整段复制粘贴到这里。'
                : `登录后浏览器会跳转到 ${redirect || '本地回调地址'}?code=…（显示「无法访问此网站」是正常的），把地址栏里整段网址复制到这里。`
            "
          >
            <TextArea v-model="pasted" :rows="3" mono :placeholder="pasteMode === 'code' ? '授权码' : redirect ? redirect + '?code=…&state=…' : 'http://localhost/...'" />
          </Field>
        </template>

        <!-- device flows -->
        <template v-if="isDeviceFlow">
          <div v-if="choices.length" class="box">
            <p class="bt">选择组织 / 项目</p>
            <p class="small faint">席位和订阅状态会在选择后单独核验。</p>
            <button v-for="c in choices" :key="String(pick(c, 'projectId'))" type="button" class="choice" :disabled="busy" @click="selectProject(c)">
              <span class="truncate">{{ pick(c, 'organizationName') || pick(c, 'organizationId') }} · {{ pick(c, 'projectName') || pick(c, 'projectId') }}</span>
              <span class="faint small">{{ pick(c, 'planScope') === 'team' ? '团队' : '个人' }}</span>
            </button>
          </div>
          <p v-else-if="!flowError" class="waiting">
            <span class="spin" /> {{ POLL_TEXT[poll?.status || 'pending'] }}
          </p>
        </template>
      </template>
      <p v-else-if="busy" class="waiting"><span class="spin" /> 正在准备授权链接…</p>
    </div>

    <!-- 4. done -->
    <div v-else-if="step === 'done' && result" class="done">
      <CircleCheck class="ok" />
      <p class="dt">{{ relogin ? '账号已重新登录' : result.status === 'replaced' ? '账号已覆盖更新' : '账号已添加' }}</p>
      <p class="mono small faint">{{ result.accountId }}</p>
      <p class="small faint">{{ relogin || result.status === 'replaced' ? '凭据已更新，统计、顺序和设置保留；模型目录和额度会在后台同步。' : '模型目录和额度会在后台同步。新账号排在调度顺序的末尾。' }}</p>
    </div>

    <template #footer>
      <template v-if="step === 'pick'">
        <Btn variant="ghost" @click="close">取消</Btn>
      </template>
      <template v-else-if="step === 'options'">
        <Btn variant="ghost" @click="step = 'pick'"><ArrowLeft /> 上一步</Btn>
        <Btn variant="primary" :loading="busy" @click="next">
          <template v-if="method === 'login'"><LogIn /> 开始登录</template>
          <template v-else>下一步</template>
        </Btn>
      </template>
      <template v-else-if="step === 'manual'">
        <Btn variant="ghost" @click="step = 'options'"><ArrowLeft /> 上一步</Btn>
        <Btn variant="primary" :loading="busy" :disabled="!manualValid" @click="createDirect()"><KeyRound /> 保存账号</Btn>
      </template>
      <template v-else-if="step === 'flow'">
        <Btn v-if="!relogin" variant="ghost" :disabled="busy" @click="back"><ArrowLeft /> 取消登录</Btn>
        <Btn v-else variant="ghost" :disabled="busy" @click="close">取消</Btn>
        <Btn v-if="flowError || (flow && !isDeviceFlow)" :disabled="busy" @click="startFlow"><RefreshCw /> 重新生成链接</Btn>
        <Btn v-if="qoderPreparing" variant="primary" :loading="busy" :disabled="qoderMethod === 'pat' && !relogin && pat.trim().length < 4" @click="confirmDevice">确认并继续</Btn>
        <Btn v-else-if="pasteMode !== 'none' && flow" variant="primary" :loading="busy" :disabled="!pasted.trim()" @click="submitPasted">完成登录</Btn>
        <Btn v-else-if="provider === 'cursor' && flow" variant="primary" :loading="busy" @click="complete({ completed: true })">我已登录</Btn>
        <Btn v-else-if="isDeviceFlow && flowError" variant="primary" :loading="busy" @click="(flowError = ''), doPoll()">再次检查</Btn>
      </template>
      <template v-else>
        <Btn @click="close">关闭</Btn>
        <Btn variant="primary" @click="openResult">查看账号</Btn>
      </template>
    </template>
  </Modal>
</template>

<style scoped>
.providers {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.prov {
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr);
  grid-template-rows: auto auto;
  column-gap: 10px;
  align-items: center;
  padding: 10px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
  text-align: left;
  min-height: 56px;
}
.prov:hover {
  background: var(--fill-2);
}
.prov > :first-child {
  grid-row: 1 / 3;
}
.pl {
  font-size: 13px;
  font-weight: 600;
}
.ph {
  font-size: 11px;
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}
.seg-wrap {
  max-width: 100%;
  overflow-x: auto;
}
.note {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-2);
  padding: 9px 11px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
}
.link {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  align-self: flex-start;
  font-size: 12px;
  color: var(--accent);
}
.link svg {
  width: 13px;
  height: 13px;
}
.box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border-strong);
  min-width: 0;
}
.bt {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12.5px;
  font-weight: 600;
}
.bt-r {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.exp {
  font-weight: 400;
  font-size: 11.5px;
}
.url {
  min-width: 0;
  font-size: 11.5px;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--fill-3);
  display: flex;
}
.open {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  align-self: flex-start;
  font-size: 12.5px;
  font-weight: 500;
}
.open svg {
  width: 13px;
  height: 13px;
}
.kv {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 4px 14px;
  margin: 0;
  font-size: 12px;
}
.kv dt {
  color: var(--text-3);
}
.kv dd {
  margin: 0;
  word-break: break-all;
}
.row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.choice {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 36px;
  padding: 6px 10px;
  border-radius: 8px;
  background: var(--fill-3);
  font-size: 12.5px;
  text-align: left;
}
.choice:hover:not(:disabled) {
  background: var(--accent-soft);
}
.waiting {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--text-2);
}
.spin {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
  border-radius: 50%;
  border: 2px solid var(--accent);
  border-right-color: transparent;
  animation: spin 0.8s linear infinite;
}
.alert {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px 11px;
  border-radius: var(--radius-md);
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--text);
  background: color-mix(in srgb, var(--orange) 14%, transparent);
}
.alert svg {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  margin-top: 2px;
  color: var(--orange);
}
.small {
  font-size: 11.5px;
}
.done {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 18px 0 8px;
  text-align: center;
  word-break: break-all;
}
.ok {
  width: 34px;
  height: 34px;
  color: var(--green);
}
.dt {
  font-size: 15px;
  font-weight: 650;
}
@media (max-width: 480px) {
  .providers {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
