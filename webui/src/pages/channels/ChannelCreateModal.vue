<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check, Link2, PenLine, TriangleAlert } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import type { Operation } from '@/api/operations'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle, fieldErrors } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import SearchField from '@/components/ui/SearchField.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import TextInput from '@/components/ui/TextInput.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import ModelPicker from './ModelPicker.vue'
import ProbePanel, { type ProbeState } from './ProbePanel.vue'
import {
  PROTOCOLS,
  notifyChannelsChanged,
  protocolLabel,
  runOperation,
  splitUrl,
  type Catalog,
  type Channel,
  type DiscoveryResult,
  type ModelPair,
  type Protocol,
} from './meta'

// New API channel as one form: provider preset, protocol, connection, models (discover or type)
// and an optional connectivity test, all on one page. Failed probes go to createChannel as
// initialProbeResults so those models start in cooldown, like the Telegram wizard.

const props = defineProps<{ open: boolean; existingNames: string[] }>()
const emit = defineEmits<{ close: []; created: [channel: Channel] }>()

const catalog = ref<Catalog | null>(null)
const catalogError = ref('')
const q = ref('')
const providerId = ref('') // '' = custom address
const presetId = ref('')
const protocol = ref<Protocol>('openai-chat')
const protocolTouched = ref(false)
const customUrl = ref(true)
const name = ref('')
const url = ref('')
const apiKey = ref('')
const models = ref<ModelPair[]>([])
const probes = ref<Record<string, ProbeState>>({})
const probeKey = ref(0)
const saving = ref(false)
const serverErrors = ref<Record<string, string>>({})
const touched = ref(false)

function reset(): void {
  q.value = ''
  providerId.value = ''
  presetId.value = ''
  protocol.value = 'openai-chat'
  protocolTouched.value = false
  customUrl.value = true
  name.value = ''
  url.value = ''
  apiKey.value = ''
  models.value = []
  probes.value = {}
  probeKey.value++
  serverErrors.value = {}
  touched.value = false
}

watch(
  () => props.open,
  async (o) => {
    if (!o) return
    reset()
    if (catalog.value) return
    try {
      catalog.value = (await api.get<Catalog>('/channel-catalog')).data
      catalogError.value = ''
    } catch (err) {
      catalogError.value = errorTitle(err)
    }
  },
  { immediate: true },
)

// ---- provider / preset / protocol ----
const providers = computed(() => {
  const list = catalog.value?.providers || []
  const s = q.value.trim().toLowerCase()
  return s ? list.filter((p) => p.name.toLowerCase().includes(s) || p.id.includes(s)) : list
})
const provider = computed(() => catalog.value?.providers.find((p) => p.id === providerId.value) || null)
const preset = computed(() => provider.value?.presets.find((p) => p.id === presetId.value) || null)
const usePreset = computed(() => Boolean(preset.value && !customUrl.value))
const protocolOptions = computed(() =>
  preset.value ? PROTOCOLS.filter((p) => preset.value!.protocols.some((x) => x.protocol === p.value)) : PROTOCOLS,
)
const protocolDesc = computed(() => PROTOCOLS.find((p) => p.value === protocol.value)?.desc || '')
const presetEndpoint = computed(() => preset.value?.protocols.find((p) => p.protocol === protocol.value)?.endpoint || '')

function pickProvider(id: string): void {
  providerId.value = id
  const p = catalog.value?.providers.find((x) => x.id === id)
  if (p) pickPreset(p.presets[0]?.id || '')
  else {
    presetId.value = ''
    customUrl.value = true
  }
}
function pickPreset(id: string): void {
  presetId.value = id
  customUrl.value = false
  const supported = preset.value?.protocols.map((p) => p.protocol) || []
  // The catalog lists each plan's recommended protocol first; keep an explicit choice when it still applies.
  if (supported.length && (!protocolTouched.value || !supported.includes(protocol.value))) protocol.value = supported[0]
}
function setProtocol(p: Protocol): void {
  protocol.value = p
  protocolTouched.value = true
}

// ---- connection ----
const parsed = computed(() => splitUrl(usePreset.value ? presetEndpoint.value : url.value))
// A pasted full call path decides the protocol until the user picks one explicitly.
watch(
  () => parsed.value.detected,
  (d) => {
    if (d && !usePreset.value && !protocolTouched.value) protocol.value = d
  },
)
const mismatch = computed(() => (!usePreset.value && parsed.value.detected && parsed.value.detected !== protocol.value ? parsed.value.detected : null))
const nameError = computed(() => {
  const n = name.value.trim()
  if (!n) return '请填写名称'
  if (n.length > 64) return '名称最多 64 个字符'
  if (props.existingNames.includes(n)) return '已有同名渠道，请换一个'
  return serverErrors.value.name || ''
})
const urlError = computed(() => {
  if (usePreset.value) return ''
  if (!url.value.trim()) return '请填写地址'
  return parsed.value.error || serverErrors.value.baseUrl || ''
})
const keyError = computed(() => (apiKey.value.trim().length < 5 ? '密钥太短（至少 5 个字符）' : serverErrors.value.apiKey || ''))
const connectionReady = computed(() => !urlError.value && !keyError.value && !mismatch.value)
const modelsError = computed(() => {
  if (!models.value.length) return '请至少选择一个模型'
  if (new Set(models.value.map((m) => m.alias)).size !== models.value.length) return '别名不能重复'
  return ''
})
const invalid = computed(() => Boolean(nameError.value || !connectionReady.value || modelsError.value))

// ---- discovery and probes ----
/** Connection fields shared by discovery (DraftChannelDiscoveryRequest) and probes (ProbeDraftRequest). */
function target(): Omit<Schemas['DraftChannelDiscoveryRequest'], 'source'> {
  const p = parsed.value
  const body: Omit<Schemas['DraftChannelDiscoveryRequest'], 'source'> = { baseUrl: p.base, apiPath: p.path, apiKey: apiKey.value.trim(), protocol: protocol.value }
  if (usePreset.value) Object.assign(body, { providerId: providerId.value, providerPresetId: presetId.value })
  return body
}
const discoverable = computed(() => !usePreset.value || preset.value?.modelsUrlConfigured)
async function discover() {
  if (!connectionReady.value) return { ok: false, error: '请先填好地址和密钥' }
  const body: Schemas['DraftChannelDiscoveryRequest'] = { source: 'draft', ...target() }
  return runOperation<DiscoveryResult>(() => api.post<Operation>('/channel-model-discoveries', body))
}
function startProbe(model: string) {
  const body: Schemas['ProbeDraftRequest'] = { name: name.value.trim() || 'management-draft', model, ...target() }
  return api.post<Operation>('/channel-drafts/probes', body)
}
function onProbe(model: string, s: ProbeState): void {
  probes.value = { ...probes.value, [model]: s }
}
// A changed connection invalidates earlier probe results.
watch([() => parsed.value.base, () => parsed.value.path, apiKey, protocol, presetId], () => {
  if (!Object.keys(probes.value).length) return
  probes.value = {}
  probeKey.value++
})

const probeSummary = computed(() => {
  const vals = models.value.map((m) => probes.value[m.real]?.status)
  return { tested: vals.filter((v) => v === 'ok' || v === 'fail').length, ok: vals.filter((v) => v === 'ok').length, fail: vals.filter((v) => v === 'fail').length }
})
const initialProbeResults = computed<Schemas['InitialProbeResultInput'][]>(() =>
  models.value
    .map((m) => ({ m, s: probes.value[m.real] }))
    .filter(({ s }) => s && (s.status === 'ok' || s.status === 'fail'))
    .map(({ m, s }) => (s!.status === 'ok' ? { model: m.real, ok: true } : { model: m.real, ok: false, reason: (s!.reason || '测试失败').slice(0, 500) })),
)

async function save(): Promise<void> {
  touched.value = true
  if (invalid.value) return
  saving.value = true
  serverErrors.value = {}
  const failed = models.value.filter((m) => probes.value[m.real]?.status === 'fail').map((m) => m.alias)
  const common = {
    name: name.value.trim(),
    apiKey: apiKey.value.trim(),
    protocol: protocol.value,
    models: models.value,
    ...(initialProbeResults.value.length ? { initialProbeResults: initialProbeResults.value } : {}),
  }
  const body = usePreset.value
    ? { mode: 'preset', ...common, providerId: providerId.value, providerPresetId: presetId.value }
    : { mode: 'manual', ...common, baseUrl: parsed.value.base, apiPath: parsed.value.path }
  try {
    const res = await api.post<Channel>('/channels', body)
    toast(
      '渠道已保存',
      failed.length ? 'warning' : 'success',
      [failed.length ? `不可用（已加入冷却）${failed.length} 个：${failed.join('、')}` : '', '已加入负载均衡优先级队列末尾，可在「负载均衡」中调整。']
        .filter(Boolean)
        .join('\n'),
      failed.length ? 9000 : 4200,
    )
    notifyChannelsChanged()
    emit('created', res.data)
    emit('close')
  } catch (err) {
    const fe = fieldErrors(err)
    serverErrors.value = Object.fromEntries(Object.entries(fe).map(([k, v]) => [k.replace(/^body\./, '').split('.')[0], v]))
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal :open="open" title="添加 API 渠道" subtitle="选好服务商和协议，填写连接信息，挑选模型后保存；测试是可选的。" width="xl" :dismissible="!saving" @close="emit('close')">
    <div class="form">
      <!-- left: where to connect -->
      <div class="col">
        <section class="sec">
          <p class="st">服务商</p>
          <p v-if="catalogError" class="c-red small">{{ catalogError }}</p>
          <Skeleton v-else-if="!catalog" h="120px" r="10px" />
          <template v-else>
            <SearchField v-model="q" placeholder="搜索服务商" width="100%" />
            <div class="providers">
              <button type="button" class="prov" :class="{ on: providerId === '' }" @click="pickProvider('')">
                <span class="custom"><PenLine /></span><span class="truncate">自定义地址</span>
              </button>
              <button v-for="p in providers" :key="p.id" type="button" class="prov" :class="{ on: providerId === p.id }" :title="p.name" @click="pickProvider(p.id)">
                <ProviderIcon :provider="p.id" :size="18" />
                <span class="truncate">{{ p.name }}</span>
              </button>
            </div>
            <div v-if="provider && provider.presets.length > 1" class="presets">
              <span class="pl">方案</span>
              <button v-for="ps in provider.presets" :key="ps.id" type="button" class="pchip" :class="{ on: presetId === ps.id }" @click="pickPreset(ps.id)">
                {{ ps.name }}<span v-if="ps.providerUsageSupported" class="tag">可查额度</span>
              </button>
            </div>
            <p v-else-if="preset?.providerUsageSupported" class="hint">这个方案支持查询上游额度。</p>
          </template>
        </section>

        <section class="sec">
          <p class="st">协议</p>
          <Segmented
            :model-value="protocol"
            :options="protocolOptions.map((p) => ({ value: p.value, label: p.short }))"
            aria-label="协议"
            @update:model-value="setProtocol($event as Protocol)"
          />
          <p class="hint">{{ protocolDesc }}</p>
        </section>

        <section class="sec">
          <p class="st">连接信息</p>
          <Field label="名称" required :error="touched || name ? nameError : ''">
            <TextInput v-model="name" placeholder="显示在列表中，空格和中文均可" :invalid="Boolean((touched || name) && nameError)" />
          </Field>
          <Field v-if="usePreset" label="地址" :hint="`由「${provider?.name} · ${preset?.name}」模板提供`">
            <div class="fixed mono">{{ presetEndpoint }}</div>
            <button type="button" class="link" @click="(customUrl = true), (url = presetEndpoint)"><Link2 /> 改为自定义地址</button>
          </Field>
          <Field v-else label="地址" required hint="填域名或 API 根路径，会按协议自动追加接口路径；接口路径不标准时直接粘贴完整调用路径，会自动拆分。" :error="touched || url ? urlError : ''">
            <TextInput v-model="url" type="url" mono placeholder="https://api.example.com" :invalid="Boolean((touched || url) && urlError)" />
            <p v-if="parsed.path && !urlError && !mismatch" class="small c-green">已识别完整路径：基础地址 {{ parsed.base }}，接口路径 {{ parsed.path }}（{{ protocolLabel(parsed.detected) }}）</p>
            <button v-if="preset && customUrl" type="button" class="link" @click="customUrl = false"><Link2 /> 恢复使用模板地址</button>
          </Field>
          <div v-if="mismatch" class="warn">
            <TriangleAlert />
            <div>
              <p class="small">地址末尾的路径属于 <b>{{ protocolLabel(mismatch) }}</b>，与所选的 {{ protocolLabel(protocol) }} 不一致。</p>
              <div class="warn-actions">
                <Btn size="sm" variant="primary" @click="setProtocol(mismatch)">改用 {{ protocolLabel(mismatch) }}</Btn>
                <Btn size="sm" @click="url = parsed.base">坚持 {{ protocolLabel(protocol) }}，只保留基础地址</Btn>
              </div>
            </div>
          </div>
          <Field label="密钥" required hint="服务商提供的 API Key，保存后不会再显示" :error="touched || apiKey ? keyError : ''">
            <TextInput v-model="apiKey" type="password" mono placeholder="sk-…" :invalid="Boolean((touched || apiKey) && keyError)" />
          </Field>
        </section>
      </div>

      <!-- right: what to serve -->
      <div class="col">
        <section class="sec">
          <p class="st">模型 <span v-if="models.length" class="cnt">已选 {{ models.length }}</span></p>
          <p v-if="discoverable && !connectionReady" class="hint">填好地址和密钥后，可以从上游获取模型列表；也可以直接手动添加。</p>
          <ModelPicker v-model="models" :discover="discoverable ? discover : null" :static-models="usePreset ? preset?.staticModels : []" />
          <p v-if="touched && modelsError" class="c-red small">{{ modelsError }}</p>
        </section>

        <section class="sec">
          <p class="st">
            连通性测试 <span class="cnt">可选</span>
            <span v-if="probeSummary.tested" class="cnt">通过 {{ probeSummary.ok }} · 失败 {{ probeSummary.fail }}</span>
          </p>
          <p class="hint">对模型做一次真实调用。测试失败的模型保存后会标为「不可用（已加入冷却）」；不测试则全部按可用处理，由后台探测持续验证。</p>
          <p v-if="!models.length" class="hint">先选择模型。</p>
          <p v-else-if="!connectionReady" class="hint">先填好地址和密钥。</p>
          <ProbePanel v-else :key="probeKey" :models="models" :start="startProbe" :initial="probes" @result="onProbe" />
        </section>
      </div>
    </div>

    <template #footer>
      <span v-if="touched && invalid" class="foot-err">
        {{ nameError || urlError || keyError || (mismatch ? '协议与地址路径不一致' : '') || modelsError }}
      </span>
      <span v-else-if="probeSummary.fail" class="foot-warn">{{ probeSummary.fail }} 个模型测试失败，保存后进入冷却</span>
      <span class="grow" />
      <Btn variant="ghost" :disabled="saving" @click="emit('close')">取消</Btn>
      <Btn variant="primary" :loading="saving" @click="save"><Check /> 保存渠道</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.form {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 22px;
}
.col {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-width: 0;
}
.sec {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.st {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
  font-weight: 650;
}
.cnt {
  font-size: 11.5px;
  font-weight: 500;
  color: var(--text-3);
}
.hint,
.small {
  font-size: 12px;
  line-height: 1.5;
}
.hint {
  color: var(--text-3);
}
.providers {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
  gap: 6px;
  max-height: 196px;
  overflow-y: auto;
  padding: 2px;
}
.prov {
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 34px;
  padding: 4px 8px;
  border-radius: 8px;
  font-size: 12.5px;
  text-align: left;
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.prov:hover {
  background: var(--fill-2);
}
.prov.on {
  background: var(--accent-soft);
  color: var(--accent);
  box-shadow: inset 0 0 0 1px var(--accent);
}
.custom {
  display: grid;
  place-items: center;
  width: 18px;
  height: 18px;
  flex-shrink: 0;
}
.custom svg {
  width: 14px;
  height: 14px;
}
.presets {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.pl {
  font-size: 12px;
  color: var(--text-3);
}
.pchip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 30px;
  padding: 0 10px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--fill-2);
}
.pchip.on {
  background: var(--accent);
  color: #fff;
}
.tag {
  font-size: 10.5px;
  opacity: 0.75;
}
.fixed {
  padding: 7px 10px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  word-break: break-all;
  background: var(--fill-3);
  color: var(--text-2);
}
.link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: 4px;
  font-size: 12px;
  color: var(--accent);
}
.link svg {
  width: 13px;
  height: 13px;
}
.warn {
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--orange) 12%, transparent);
}
.warn > svg {
  width: 16px;
  height: 16px;
  color: var(--orange);
  flex-shrink: 0;
  margin-top: 2px;
}
.warn-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.foot-err {
  font-size: 12px;
  color: var(--red);
}
.foot-warn {
  font-size: 12px;
  color: var(--orange);
}
.grow {
  flex: 1;
}
@media (max-width: 860px) {
  .form {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
