<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { CircleCheck, CircleX, LoaderCircle } from '@lucide/vue'
import { api } from '@/api/client'
import type { Operation } from '@/api/operations'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import Switch from '@/components/ui/Switch.vue'
import TextArea from '@/components/ui/TextArea.vue'
import TextInput from '@/components/ui/TextInput.vue'
import { runOperation } from '@/pages/channels/meta'
import { nameError, notifyProxiesChanged, parseProxyLine, type ProbeOutcome, type ProbeItem, type Proxy } from './meta'

// Two ways to add proxies: a field-by-field form, or pasting ss:// / socks5:// links (one per
// line, previewed live with editable names).

const props = defineProps<{ open: boolean; taken: string[] }>()
const emit = defineEmits<{ close: [] }>()

type Mode = 'form' | 'paste'
const mode = ref<Mode>('form')
const testAfter = ref(true)
const running = ref(false)
const added = ref<string[]>([])

// ---- form ----
const CIPHERS = ['2022-blake3-aes-128-gcm', '2022-blake3-aes-256-gcm', '2022-blake3-chacha20-poly1305', 'chacha20-ietf-poly1305', 'aes-128-gcm', 'aes-256-gcm']
const f = ref({ type: 'ss' as 'ss' | 'socks5', name: '', server: '', port: null as number | null, cipher: CIPHERS[0], password: '', username: '' })
const formTouched = ref(false)
const formResult = ref<{ ok: boolean; text: string } | null>(null)

// ---- paste ----
interface Row {
  url: string
  name: string
  parseError: string
  status: 'idle' | 'saving' | 'ok' | 'fail'
  message: string
}
const text = ref('')
const rows = ref<Row[]>([])

watch(
  () => props.open,
  (o) => {
    if (!o) return
    mode.value = 'form'
    f.value = { type: 'ss', name: '', server: '', port: null, cipher: CIPHERS[0], password: '', username: '' }
    formTouched.value = false
    formResult.value = null
    text.value = ''
    rows.value = []
    running.value = false
    added.value = []
  },
)

const taken = computed(() => [...props.taken, ...added.value])

const hostError = computed(() => {
  const h = f.value.server.trim()
  if (!h) return '请填写服务器地址'
  if (/[\s/@?#]/.test(h)) return '只填主机名或 IP，不要带协议、路径或端口'
  return ''
})
const formErrors = computed(() => ({
  name: nameError(f.value.name, taken.value),
  server: hostError.value,
  port: f.value.port == null || f.value.port < 1 || f.value.port > 65535 ? '端口范围 1～65535' : '',
  password: f.value.type === 'ss' && !f.value.password ? '请填写密码' : '',
}))
const formInvalid = computed(() => Object.values(formErrors.value).some(Boolean))
const formUrl = computed(() => {
  const host = f.value.server.trim().includes(':') ? `[${f.value.server.trim()}]` : f.value.server.trim()
  const hp = `${host}:${f.value.port}`
  if (f.value.type === 'ss') return `ss://${encodeURIComponent(f.value.cipher)}:${encodeURIComponent(f.value.password)}@${hp}`
  const user = f.value.username.trim()
  const auth = user ? `${encodeURIComponent(user)}${f.value.password ? `:${encodeURIComponent(f.value.password)}` : ''}@` : ''
  return `socks5://${auth}${hp}`
})

async function createOne(name: string, url: string): Promise<{ ok: boolean; text: string }> {
  try {
    const res = await api.post<Proxy>('/proxies', { name, url })
    added.value.push(name)
    if (!testAfter.value) return { ok: true, text: `已添加 ${name}` }
    const out = await runOperation<ProbeOutcome>(() => api.post<Operation>(`/proxies/${encodeURIComponent(res.data.proxyId)}/actions/test`))
    const item = (out.result?.results as ProbeItem | undefined) || null
    return out.ok && item?.ok
      ? { ok: true, text: `已添加 ${name} · 出口 IP ${item.ip || '未知'} · 延迟 ${item.latencyMilliseconds ?? '—'}ms` }
      : { ok: true, text: `已添加 ${name}，但测试未通过${item?.error?.message ? `：${item.error.message}` : out.error ? `：${out.error}` : ''}` }
  } catch (err) {
    return { ok: false, text: [errorTitle(err), errorDetail(err)].filter(Boolean).join('：') }
  }
}

async function submitForm(): Promise<void> {
  formTouched.value = true
  if (formInvalid.value) return
  running.value = true
  formResult.value = null
  const r = await createOne(f.value.name.trim(), formUrl.value)
  running.value = false
  formResult.value = r
  if (r.ok) {
    notifyProxiesChanged()
    toast('代理已添加', 'success')
    // Ready for the next one; keep type and cipher.
    f.value = { ...f.value, name: '', server: '', port: null, password: '', username: '' }
    formTouched.value = false
  }
}

// Paste mode: parse as you type, keeping names the user already edited.
watch(text, (t) => {
  const prev = Object.fromEntries(rows.value.map((r) => [r.url, r]))
  const used = [...taken.value]
  rows.value = t
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      const p = parseProxyLine(l, used)
      const old = prev[p.url]
      const name = old && old.status !== 'ok' ? old.name : p.name
      used.push(name)
      return old && old.status === 'ok' ? old : { url: p.url, name, parseError: p.error, status: 'idle' as const, message: '' }
    })
})
function rowError(r: Row, i: number): string {
  if (r.parseError) return r.parseError
  const others = [...props.taken, ...rows.value.filter((_, j) => j !== i).map((x) => x.name.trim())]
  return nameError(r.name, others)
}
const pending = computed(() => rows.value.filter((r) => r.status !== 'ok'))
const pasteInvalid = computed(() => rows.value.some((r, i) => r.status !== 'ok' && rowError(r, i)))

async function submitPaste(): Promise<void> {
  if (pasteInvalid.value || !pending.value.length) return
  running.value = true
  let okCount = 0
  for (const r of rows.value) {
    if (r.status === 'ok') continue
    r.status = 'saving'
    r.message = ''
    const out = await createOne(r.name.trim(), r.url)
    r.status = out.ok ? 'ok' : 'fail'
    r.message = out.text
    if (out.ok) okCount++
  }
  running.value = false
  if (okCount) {
    notifyProxiesChanged()
    toast(`已添加 ${okCount} 个代理`, 'success')
  }
}
</script>

<template>
  <Modal :open="open" title="添加代理" width="lg" :dismissible="!running" @close="emit('close')">
    <div class="mode">
      <Segmented
        v-model="mode"
        :options="[
          { value: 'form', label: '逐项填写' },
          { value: 'paste', label: '粘贴链接（可多条）' },
        ]"
      />
    </div>

    <template v-if="mode === 'form'">
      <div class="form">
        <Field label="名称" required hint="小写字母、数字、- 和 _" :error="formTouched || f.name ? formErrors.name : ''">
          <TextInput v-model="f.name" mono placeholder="例如：us-west" :invalid="Boolean((formTouched || f.name) && formErrors.name)" />
        </Field>
        <Field label="类型">
          <Segmented
            v-model="f.type"
            size="sm"
            :options="[
              { value: 'ss', label: 'Shadowsocks' },
              { value: 'socks5', label: 'SOCKS5' },
            ]"
          />
        </Field>
        <Field label="服务器" required :error="formTouched || f.server ? formErrors.server : ''">
          <TextInput v-model="f.server" mono placeholder="1.2.3.4 或 proxy.example.com" :invalid="Boolean((formTouched || f.server) && formErrors.server)" />
        </Field>
        <Field label="端口" required :error="formTouched ? formErrors.port : ''">
          <NumberInput v-model="f.port" :min="1" :max="65535" integer width="100%" placeholder="8388" />
        </Field>
        <template v-if="f.type === 'ss'">
          <Field label="加密方式">
            <Select v-model="f.cipher" :options="CIPHERS.map((c) => ({ value: c, label: c }))" />
          </Field>
          <Field label="密码" required :error="formTouched ? formErrors.password : ''">
            <TextInput v-model="f.password" type="password" mono :invalid="Boolean(formTouched && formErrors.password)" />
          </Field>
        </template>
        <template v-else>
          <Field label="用户名" hint="不需要认证时留空">
            <TextInput v-model="f.username" mono />
          </Field>
          <Field label="密码">
            <TextInput v-model="f.password" type="password" mono />
          </Field>
        </template>
      </div>
      <p v-if="formResult" class="result" :class="formResult.ok ? 'ok' : 'fail'">{{ formResult.text }}</p>
    </template>

    <template v-else>
      <TextArea v-model="text" :rows="6" mono placeholder="ss://method:password@host:port#名称&#10;ss://base64@host:port#名称&#10;socks5://user:pass@host:port#名称" />
      <div class="help">
        <ul>
          <li><span class="mono">ss://method:password@host:port#名称</span> 或 <span class="mono">ss://&lt;base64&gt;@host:port#名称</span>（Shadowsocks 2022 或 AEAD，不支持 SIP003 插件）</li>
          <li><span class="mono">socks5://[用户名:密码@]host:port#名称</span></li>
        </ul>
        <p># 后面的名称可选，没有时会自动起名，下面可以直接修改。</p>
      </div>
      <div v-if="rows.length" class="preview">
        <p class="pt">将添加 {{ pending.length }} 个</p>
        <ul class="rows">
          <li v-for="(r, i) in rows" :key="r.url + i" :class="r.status">
            <span class="st">
              <LoaderCircle v-if="r.status === 'saving'" class="spin" />
              <CircleCheck v-else-if="r.status === 'ok'" />
              <CircleX v-else-if="r.status === 'fail' || rowError(r, i)" class="bad" />
              <span v-else class="n mono">{{ i + 1 }}</span>
            </span>
            <div class="main">
              <div class="line">
                <input v-model="r.name" class="ctl mono name" :class="{ invalid: r.status !== 'ok' && rowError(r, i) }" :disabled="r.status === 'ok' || running" placeholder="名称" />
                <span class="url mono truncate" :title="r.url">{{ r.url.replace(/:\/\/[^@]*@/, '://***@') }}</span>
              </div>
              <p v-if="r.status !== 'ok' && rowError(r, i)" class="msg err">{{ rowError(r, i) }}</p>
              <p v-if="r.message" class="msg" :class="{ err: r.status === 'fail' }">{{ r.message }}</p>
            </div>
          </li>
        </ul>
      </div>
    </template>
    <label class="test"><Switch v-model="testAfter" label="添加后测试" /> 添加后立即测试连通性</label>

    <template #footer>
      <span class="grow" />
      <Btn variant="ghost" :disabled="running" @click="emit('close')">{{ added.length ? '完成' : '取消' }}</Btn>
      <Btn v-if="mode === 'form'" variant="primary" :loading="running" @click="submitForm">添加代理</Btn>
      <Btn v-else variant="primary" :loading="running" :disabled="pasteInvalid || !pending.length" @click="submitPaste">添加 {{ pending.length }} 个</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.help {
  margin-top: 10px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-3);
}
.help ul {
  margin: 2px 0;
  padding-left: 18px;
}
.help .mono {
  word-break: break-all;
}
.rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.rows li {
  display: flex;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 10px;
  background: var(--fill-3);
}
.rows li.ok {
  background: color-mix(in srgb, var(--green) 9%, transparent);
}
.rows li.fail {
  background: color-mix(in srgb, var(--red) 9%, transparent);
}
.st {
  width: 18px;
  display: grid;
  place-items: center;
  height: 30px;
  flex-shrink: 0;
}
.st svg {
  width: 15px;
  height: 15px;
}
.ok .st {
  color: var(--green);
}
.bad {
  color: var(--red);
}
.spin {
  color: var(--blue);
  animation: spin 0.9s linear infinite;
}
.n {
  font-size: 11px;
  color: var(--text-3);
}
.main {
  flex: 1;
  min-width: 0;
}
.line {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.name {
  width: 170px;
  flex-shrink: 0;
}
.url {
  flex: 1;
  font-size: 11.5px;
  color: var(--text-2);
}
.msg {
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--text-2);
  word-break: break-word;
}
.msg.err {
  color: var(--red);
}
.test {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  font-size: 12.5px;
}
.grow {
  flex: 1;
}
@media (max-width: 560px) {
  .line {
    flex-direction: column;
    align-items: stretch;
    gap: 4px;
  }
  .name {
    width: 100%;
  }
}
.mode {
  margin-bottom: 14px;
}
.form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 14px;
}
.form .full {
  grid-column: 1 / -1;
}
.result {
  margin-top: 12px;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 12.5px;
  word-break: break-word;
}
.result.ok {
  background: color-mix(in srgb, var(--green) 10%, transparent);
}
.result.fail {
  color: var(--red);
  background: color-mix(in srgb, var(--red) 10%, transparent);
}
.preview {
  margin-top: 12px;
}
.pt {
  margin-bottom: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
}
@media (max-width: 560px) {
  .form {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
