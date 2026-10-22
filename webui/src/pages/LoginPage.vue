<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Check, KeyRound, RotateCcw, Send, ShieldX, TimerOff, TriangleAlert } from '@lucide/vue'
import { api, ApiError, request } from '@/api/client'
import type { LoginOptions, TelegramApprovalCreated, TelegramApprovalStatus } from '@/api/types'
import { deviceSummary, loadSession } from '@/stores/session'
import BrandMark from '@/components/shell/BrandMark.vue'
import Btn from '@/components/ui/Btn.vue'

type Phase = 'idle' | 'starting' | 'waiting' | 'approved' | 'denied' | 'expired' | 'error'

const route = useRoute()
const router = useRouter()

const options = ref<LoginOptions | null>(null)
const phase = ref<Phase>('idle')
const errorText = ref('')
const tgFailed = ref(false)
const approval = ref<TelegramApprovalCreated | null>(null)
const now = ref(Date.now())
const keyValue = ref('')
const keyBusy = ref(false)
const keyError = ref('')
const keyOpen = ref(false)

let pollTimer: number | undefined
let tick: number | undefined
let pollAbort: AbortController | null = null

const expiredBanner = computed(() => route.query.expired === '1')
const tgAvailable = computed(() => options.value?.telegramApproval.available !== false)
const showKey = computed(() => (options.value?.managementKey.available !== false) && (!tgAvailable.value || tgFailed.value))
const ttl = computed(() => options.value?.telegramApproval.ttlSeconds || 180)
const remaining = computed(() => {
  if (!approval.value) return 0
  return Math.max(0, Math.round((new Date(approval.value.expiresAt).getTime() - now.value) / 1000))
})
const progress = computed(() => Math.min(1, remaining.value / ttl.value))
const countdown = computed(() => {
  const s = remaining.value
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
})
const codeChars = computed(() => (approval.value?.confirmCode || '').split(''))

const RING = 2 * Math.PI * 54

onMounted(async () => {
  try {
    options.value = (await api.get<LoginOptions>('/auth/login-options')).data
  } catch {
    options.value = { webUiEnabled: true, telegramApproval: { available: true, ttlSeconds: 180 }, managementKey: { available: true } }
  }
  if (!tgAvailable.value) keyOpen.value = true
})

onBeforeUnmount(stopPolling)

function stopPolling(): void {
  window.clearTimeout(pollTimer)
  window.clearInterval(tick)
  pollAbort?.abort()
  pollAbort = null
}

function describe(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === 'RATE_LIMITED') return '请求太频繁了，请稍等一分钟再试。'
    if (err.code === 'DEPENDENCY_UNAVAILABLE') return 'Telegram 消息发送失败，Bot 可能未配置或暂时无法连接。'
    if (err.code === 'ORIGIN_DENIED') return '当前访问地址未被允许，请检查反向代理配置。'
    if (err.code === 'SERVICE_NOT_READY') return 'Parrot 管理服务尚未就绪，请稍后再试。'
    if (err.status === 0) return '无法连接到 Parrot，请检查网络。'
    return err.message
  }
  return '出现了意外错误，请重试。'
}

async function startTelegram(): Promise<void> {
  stopPolling()
  phase.value = 'starting'
  errorText.value = ''
  try {
    const res = await api.post<TelegramApprovalCreated>('/auth/telegram-approvals', {
      clientName: 'Parrot WebUI',
      deviceSummary: deviceSummary(),
    })
    approval.value = res.data
    phase.value = 'waiting'
    now.value = Date.now()
    tick = window.setInterval(() => {
      now.value = Date.now()
      if (phase.value === 'waiting' && remaining.value <= 0) {
        stopPolling()
        phase.value = 'expired'
      }
    }, 250)
    schedulePoll(res.data.pollAfterSeconds)
  } catch (err) {
    phase.value = 'error'
    errorText.value = describe(err)
    if (err instanceof ApiError && err.code === 'DEPENDENCY_UNAVAILABLE') {
      tgFailed.value = true
      keyOpen.value = true
    }
  }
}

function schedulePoll(seconds: number): void {
  window.clearTimeout(pollTimer)
  pollTimer = window.setTimeout(poll, Math.max(1, seconds) * 1000)
}

async function poll(): Promise<void> {
  const current = approval.value
  if (!current || phase.value !== 'waiting') return
  pollAbort = new AbortController()
  try {
    const res = await request<TelegramApprovalStatus>('GET', `/auth/telegram-approvals/${encodeURIComponent(current.approvalId)}`, {
      headers: { Authorization: `Approval ${current.exchangeSecret}` },
      signal: pollAbort.signal,
    })
    const status = res.data.status
    if (status === 'pending') return schedulePoll(res.data.pollAfterSeconds)
    if (status === 'approved') return exchange(current)
    stopPolling()
    phase.value = status === 'denied' ? 'denied' : 'expired'
  } catch (err) {
    if ((err as Error).name === 'AbortError') return
    if (err instanceof ApiError && err.status === 0) return schedulePoll(3)
    stopPolling()
    phase.value = 'error'
    errorText.value = describe(err)
  }
}

async function exchange(current: TelegramApprovalCreated): Promise<void> {
  stopPolling()
  phase.value = 'approved'
  try {
    await api.post('/auth/sessions', {
      grantType: 'telegramApproval',
      approvalId: current.approvalId,
      exchangeSecret: current.exchangeSecret,
      sessionTransport: 'cookie',
    })
    await enter()
  } catch (err) {
    phase.value = 'error'
    errorText.value = describe(err)
  }
}

async function enter(): Promise<void> {
  await loadSession()
  const next = typeof route.query.next === 'string' && route.query.next.startsWith('/') ? route.query.next : '/'
  window.setTimeout(() => router.replace(next), 450)
}

function cancel(): void {
  stopPolling()
  approval.value = null
  phase.value = 'idle'
}

async function submitKey(): Promise<void> {
  if (!keyValue.value.trim() || keyBusy.value) return
  keyBusy.value = true
  keyError.value = ''
  try {
    await api.post('/auth/sessions', {
      grantType: 'managementKey',
      managementKey: keyValue.value.trim(),
      sessionTransport: 'cookie',
      clientName: 'Parrot WebUI',
      deviceSummary: deviceSummary(),
    })
    keyValue.value = ''
    phase.value = 'approved'
    await enter()
  } catch (err) {
    keyError.value = err instanceof ApiError && err.code === 'AUTHENTICATION_FAILED' ? '管理密钥不正确。' : describe(err)
  } finally {
    keyBusy.value = false
  }
}
</script>

<template>
  <div class="login">
    <div class="aurora" aria-hidden="true">
      <span class="blob b1" />
      <span class="blob b2" />
      <span class="blob b3" />
    </div>
    <div class="grain" aria-hidden="true" />

    <main class="panel glass">
      <header class="brand">
        <BrandMark :size="54" />
        <h1>Parrot</h1>
        <p>控制台</p>
      </header>

      <p v-if="expiredBanner && phase === 'idle'" class="banner"><TriangleAlert /> 登录已过期，请重新登录。</p>

      <Transition name="swap" mode="out-in">
        <section v-if="phase === 'idle' || phase === 'starting'" key="idle" class="stage">
          <template v-if="tgAvailable">
            <Btn variant="telegram" size="lg" block :loading="phase === 'starting'" @click="startTelegram">
              <Send /> 通过 Telegram 登录
            </Btn>
            <p class="hint">会向管理员的 Telegram 发送一条登录确认，在 Telegram 里点「批准」即可进入。</p>
          </template>
          <p v-else class="hint warn">当前未配置 Telegram Bot，请使用管理密钥登录。</p>
        </section>

        <section v-else-if="phase === 'waiting'" key="waiting" class="stage">
          <div class="ring-wrap">
            <svg class="ring" viewBox="0 0 120 120" aria-hidden="true">
              <circle class="track" cx="60" cy="60" r="54" />
              <circle class="bar" cx="60" cy="60" r="54" :stroke-dasharray="RING" :stroke-dashoffset="RING * (1 - progress)" />
            </svg>
            <span class="wave w1" />
            <span class="wave w2" />
            <span class="plane"><Send /></span>
          </div>
          <p class="lead">已发送到 Telegram</p>
          <div v-if="codeChars.length" class="code" aria-label="确认码">
            <span v-for="(c, i) in codeChars" :key="i" :style="{ animationDelay: `${i * 70}ms` }">{{ c }}</span>
          </div>
          <p class="hint">
            <template v-if="codeChars.length">请核对 Telegram 消息里的确认码与上面一致，再点「✅ 批准」。</template>
            <template v-else>请在 Telegram 中点「✅ 批准」。</template>
          </p>
          <div class="row">
            <span class="timer mono">{{ countdown }}</span>
            <Btn variant="ghost" size="sm" @click="cancel">取消</Btn>
          </div>
        </section>

        <section v-else-if="phase === 'approved'" key="ok" class="stage">
          <div class="result ok"><Check /></div>
          <p class="lead">已批准，正在进入…</p>
        </section>

        <section v-else key="fail" class="stage">
          <div class="result" :class="phase === 'expired' ? 'warn' : 'bad'">
            <TimerOff v-if="phase === 'expired'" />
            <ShieldX v-else-if="phase === 'denied'" />
            <TriangleAlert v-else />
          </div>
          <p class="lead">
            {{ phase === 'expired' ? '确认已过期' : phase === 'denied' ? '登录请求被拒绝' : '登录没有成功' }}
          </p>
          <p class="hint">
            {{ phase === 'expired' ? '3 分钟内没有收到批准。' : phase === 'denied' ? '管理员在 Telegram 中拒绝了这次登录。' : errorText }}
          </p>
          <Btn v-if="tgAvailable" variant="secondary" @click="startTelegram"><RotateCcw /> 重新发起</Btn>
        </section>
      </Transition>

      <Transition name="swap">
        <section v-if="showKey && phase !== 'approved' && phase !== 'waiting'" class="key">
          <button v-if="!keyOpen" class="key-toggle" @click="keyOpen = true"><KeyRound /> 使用管理密钥登录</button>
          <form v-else class="key-form" @submit.prevent="submitKey">
            <label for="mk">管理密钥</label>
            <div class="key-row">
              <input
                id="mk"
                v-model="keyValue"
                type="password"
                autocomplete="current-password"
                placeholder="config.json 中的 management.managementKey"
              />
              <Btn type="submit" variant="primary" :loading="keyBusy" :disabled="!keyValue.trim()">登录</Btn>
            </div>
            <p v-if="keyError" class="key-error">{{ keyError }}</p>
          </form>
        </section>
      </Transition>
    </main>
  </div>
</template>

<style scoped>
.login {
  position: relative;
  min-height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
  overflow: hidden;
  background: var(--bg);
}
.aurora {
  position: absolute;
  inset: -20%;
  filter: blur(80px) saturate(140%);
  opacity: 0.9;
}
.blob {
  position: absolute;
  border-radius: 50%;
  animation: drift 22s ease-in-out infinite alternate;
}
.b1 {
  width: 46vmax;
  height: 46vmax;
  left: 8%;
  top: 6%;
  background: radial-gradient(circle, rgba(48, 209, 88, 0.42), transparent 65%);
}
.b2 {
  width: 52vmax;
  height: 52vmax;
  right: 2%;
  top: 18%;
  background: radial-gradient(circle, rgba(10, 132, 255, 0.45), transparent 65%);
  animation-duration: 28s;
  animation-delay: -6s;
}
.b3 {
  width: 40vmax;
  height: 40vmax;
  left: 32%;
  bottom: 0;
  background: radial-gradient(circle, rgba(100, 210, 255, 0.32), transparent 65%);
  animation-duration: 25s;
  animation-delay: -12s;
}
:root[data-theme='light'] .aurora {
  opacity: 0.55;
}
@keyframes drift {
  0% {
    transform: translate(0, 0) scale(1);
  }
  100% {
    transform: translate(6vmax, -4vmax) scale(1.12);
  }
}
.grain {
  position: absolute;
  inset: 0;
  opacity: 0.06;
  pointer-events: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
}
.panel {
  position: relative;
  width: min(392px, 100%);
  padding: 34px 30px 26px;
  border-radius: 22px;
  background: color-mix(in srgb, var(--surface-raised) 72%, transparent);
  box-shadow: var(--shadow-lg), inset 0 1px 0 rgba(255, 255, 255, 0.08);
  animation: fade-up 0.6s var(--ease-out) both;
}
.brand {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  margin-bottom: 26px;
}
.brand h1 {
  margin-top: 12px;
  font-family: var(--font-display);
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.brand p {
  font-size: 13px;
  color: var(--text-2);
  letter-spacing: 0.3em;
  padding-left: 0.3em;
}
.banner {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  padding: 9px 12px;
  border-radius: var(--radius-md);
  font-size: 12.5px;
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 12%, transparent);
}
.banner svg {
  width: 15px;
  height: 15px;
}
.stage {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  text-align: center;
}
.hint {
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-2);
  max-width: 300px;
}
.hint.warn {
  color: var(--orange);
}
.lead {
  font-size: 15px;
  font-weight: 650;
}
.ring-wrap {
  position: relative;
  width: 120px;
  height: 120px;
  display: grid;
  place-items: center;
}
.ring {
  position: absolute;
  inset: 0;
  transform: rotate(-90deg);
}
.ring circle {
  fill: none;
  stroke-width: 4;
}
.track {
  stroke: var(--fill-2);
}
.bar {
  stroke: var(--telegram);
  stroke-linecap: round;
  transition: stroke-dashoffset 0.3s linear;
}
.wave {
  position: absolute;
  width: 64px;
  height: 64px;
  border-radius: 50%;
  border: 1.5px solid var(--telegram);
  animation: pulse-ring 2.4s var(--ease-out) infinite;
}
.w2 {
  animation-delay: 1.2s;
}
.plane {
  position: relative;
  width: 64px;
  height: 64px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  color: #fff;
  background: linear-gradient(180deg, #37b5f3, #229ed9);
  box-shadow: 0 10px 30px -8px rgba(34, 158, 217, 0.7);
}
.plane svg {
  width: 28px;
  height: 28px;
  transform: translate(-1px, 1px);
}
.code {
  display: flex;
  gap: 8px;
  margin: 2px 0;
}
.code span {
  width: 46px;
  height: 56px;
  display: grid;
  place-items: center;
  border-radius: 12px;
  font-family: var(--font-mono);
  font-size: 28px;
  font-weight: 600;
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border-strong);
  animation: fade-up 0.45s var(--ease-spring) both;
}
.row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.timer {
  font-size: 13px;
  color: var(--text-2);
}
.result {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  animation: fade-up 0.4s var(--ease-spring) both;
}
.result svg {
  width: 30px;
  height: 30px;
}
.result.ok {
  color: #fff;
  background: var(--green);
  box-shadow: 0 10px 30px -8px color-mix(in srgb, var(--green) 70%, transparent);
}
.result.bad {
  color: var(--red);
  background: color-mix(in srgb, var(--red) 14%, transparent);
}
.result.warn {
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 14%, transparent);
}
.key {
  margin-top: 22px;
  padding-top: 18px;
  border-top: 0.5px solid var(--border);
}
.key-toggle {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: 100%;
  font-size: 12.5px;
  color: var(--text-2);
}
.key-toggle:hover {
  color: var(--text);
}
.key-toggle svg {
  width: 14px;
  height: 14px;
}
.key-form label {
  display: block;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--text-2);
}
.key-row {
  display: flex;
  gap: 8px;
}
.key-row input {
  flex: 1;
  min-width: 0;
  height: 32px;
  padding: 0 10px;
  border-radius: var(--radius-sm);
  border: 0;
  outline: 0;
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border-strong);
  font-size: 13px;
}
.key-row input:focus {
  box-shadow: inset 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft);
}
.key-row :deep(.btn) {
  height: 32px;
}
.key-error {
  margin-top: 8px;
  font-size: 12px;
  color: var(--red);
}
.swap-enter-active,
.swap-leave-active {
  transition: opacity 0.2s, transform 0.25s var(--ease-out);
}
.swap-enter-from {
  opacity: 0;
  transform: translateY(6px);
}
.swap-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
@media (max-width: 480px) {
  .panel {
    padding: 28px 20px 22px;
  }
}
</style>
