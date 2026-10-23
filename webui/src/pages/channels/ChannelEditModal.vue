<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { TriangleAlert } from '@lucide/vue'
import { api, enc } from '@/api/client'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle, fieldErrors } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Select from '@/components/ui/Select.vue'
import Switch from '@/components/ui/Switch.vue'
import TextInput from '@/components/ui/TextInput.vue'
import { PROTOCOLS, protocolLabel, splitUrl, type Channel, type Protocol } from './meta'

const props = defineProps<{ open: boolean; channel: Channel }>()
const emit = defineEmits<{ close: []; saved: [channel: Channel] }>()

const name = ref('')
const url = ref('')
const protocol = ref<Protocol>('anthropic')
const apiKey = ref('')
const maxConcurrent = ref<number | null>(0)
const cc = ref(false)
const omitTemperature = ref(false)
const omitThinking = ref(false)
const saving = ref(false)
const errors = ref<Record<string, string>>({})

watch(
  () => props.open,
  (o) => {
    if (!o) return
    const c = props.channel
    name.value = c.name
    url.value = c.url || c.baseUrl
    protocol.value = c.protocol
    apiKey.value = ''
    maxConcurrent.value = c.maxConcurrent
    cc.value = c.ccMimicry
    omitTemperature.value = c.omitTemperature
    omitThinking.value = c.omitThinking
    errors.value = {}
  },
  { immediate: true },
)

const originalUrl = computed(() => props.channel.url || props.channel.baseUrl)
const urlChanged = computed(() => url.value.trim().replace(/\/+$/, '') !== originalUrl.value.replace(/\/+$/, ''))
const parsed = computed(() => splitUrl(url.value))
const effectivePath = computed(() => (urlChanged.value ? parsed.value.path : props.channel.apiPath || null))
const pathProtocol = computed(() => (effectivePath.value ? splitUrl(`http://x${effectivePath.value}`).detected : null))
const mismatch = computed(() => (pathProtocol.value && pathProtocol.value !== protocol.value ? pathProtocol.value : null))

const nameError = computed(() => {
  const n = name.value.trim()
  if (!n) return '请填写名称'
  if (n.length > 64) return '名称最多 64 个字符'
  return errors.value.name || ''
})
const urlError = computed(() => (!url.value.trim() ? '请填写地址' : parsed.value.error || errors.value.baseUrl || errors.value.apiPath || ''))
const keyError = computed(() => (apiKey.value && apiKey.value.trim().length < 5 ? '密钥太短（至少 5 个字符）' : errors.value.apiKey || ''))

const patch = computed(() => {
  const c = props.channel
  const out: Record<string, unknown> = {}
  if (name.value.trim() !== c.name) out.name = name.value.trim()
  if (urlChanged.value) {
    out.baseUrl = parsed.value.base
    out.apiPath = parsed.value.path
  }
  if (protocol.value !== c.protocol) out.protocol = protocol.value
  if (apiKey.value.trim()) out.apiKey = apiKey.value.trim()
  if ((maxConcurrent.value ?? 0) !== c.maxConcurrent) out.maxConcurrent = maxConcurrent.value ?? 0
  if (cc.value !== c.ccMimicry) out.ccMimicry = cc.value
  if (omitTemperature.value !== c.omitTemperature) out.omitTemperature = omitTemperature.value
  if (omitThinking.value !== c.omitThinking) out.omitThinking = omitThinking.value
  return out
})
const invalid = computed(() => Boolean(nameError.value || urlError.value || keyError.value || mismatch.value))

function adoptPathProtocol(): void {
  if (mismatch.value) protocol.value = mismatch.value
}
function dropPath(): void {
  url.value = urlChanged.value ? parsed.value.base : props.channel.baseUrl
}

async function save(): Promise<void> {
  if (invalid.value) return
  if (!Object.keys(patch.value).length) {
    emit('close')
    return
  }
  saving.value = true
  errors.value = {}
  try {
    const res = await api.patch<Channel>(`/channels/${enc(props.channel.id)}`, patch.value, { ifMatch: props.channel.revision })
    toast('渠道已更新', 'success')
    emit('saved', res.data)
    emit('close')
  } catch (err) {
    const fe = fieldErrors(err)
    errors.value = Object.fromEntries(Object.entries(fe).map(([k, v]) => [k.replace(/^body\./, '').split('.')[0], v]))
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal :open="open" title="编辑渠道" :subtitle="channel.name" width="md" :dismissible="!saving" @close="emit('close')">
    <div class="form">
      <Field label="名称" required :error="nameError">
        <TextInput v-model="name" :invalid="Boolean(nameError)" />
      </Field>
      <Field label="地址" required hint="可以填域名、API 根路径，或完整调用路径（会自动拆分出接口路径）" :error="urlError">
        <TextInput v-model="url" type="url" mono :invalid="Boolean(urlError)" />
        <p v-if="urlChanged && parsed.path && !urlError" class="small c-green">已识别接口路径 {{ parsed.path }}</p>
      </Field>
      <Field label="协议" :hint="protocol !== 'anthropic' && channel.protocol === 'anthropic' ? '切换到 OpenAI 协议会自动关闭 CC 伪装；切换协议不会自动修改地址、密钥或模型。' : '切换协议不会自动修改地址、密钥或模型。'">
        <Select v-model="protocol" :options="PROTOCOLS.map((p) => ({ value: p.value, label: p.label }))" />
      </Field>
      <div v-if="mismatch" class="warn">
        <TriangleAlert />
        <div>
          <p class="small">
            地址里的接口路径 <b class="mono">{{ effectivePath }}</b> 属于 {{ protocolLabel(mismatch) }}，与所选的 {{ protocolLabel(protocol) }} 不匹配，直接保存会被拒绝。
          </p>
          <div class="warn-actions">
            <Btn size="sm" variant="primary" @click="adoptPathProtocol">改用 {{ protocolLabel(mismatch) }}</Btn>
            <Btn size="sm" @click="dropPath">保持 {{ protocolLabel(protocol) }}，只保留基础地址</Btn>
          </div>
        </div>
      </div>
      <Field label="密钥" :hint="`当前：${channel.apiKeyMaskedHint || '未配置'}。留空表示不修改；新密钥保存后不会再显示。`" :error="keyError">
        <TextInput v-model="apiKey" type="password" mono placeholder="不修改" :invalid="Boolean(keyError)" />
      </Field>
      <Field label="最大并发" hint="0 表示使用全局默认值；正整数表示同时最多 N 个在途请求，超出会排队">
        <NumberInput v-model="maxConcurrent" :min="0" :max="100000" integer unit="个" />
      </Field>
      <div class="toggles">
        <Field inline label="CC 伪装" hint="以 Claude Code 客户端的身份请求上游，仅对 Anthropic 协议有意义">
          <Switch v-model="cc" label="CC 伪装" />
        </Field>
        <Field inline label="剔除 temperature" hint="发往上游前去掉 temperature 参数，适配不接受该参数的上游">
          <Switch v-model="omitTemperature" label="剔除 temperature" />
        </Field>
        <Field inline label="剔除 thinking" hint="发往上游前去掉 thinking（思考）参数">
          <Switch v-model="omitThinking" label="剔除 thinking" />
        </Field>
      </div>
    </div>
    <template #footer>
      <Btn variant="ghost" :disabled="saving" @click="emit('close')">取消</Btn>
      <Btn variant="primary" :loading="saving" :disabled="invalid" @click="save">保存</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.small {
  font-size: 12px;
  line-height: 1.5;
}
.toggles {
  display: flex;
  flex-direction: column;
  padding: 0 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
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
</style>
