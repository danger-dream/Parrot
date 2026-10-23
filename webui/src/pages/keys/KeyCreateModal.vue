<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '@/api/client'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle, fieldErrors } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import Segmented from '@/components/ui/Segmented.vue'
import TextInput from '@/components/ui/TextInput.vue'
import { nameError, notifyKeysChanged, secretError, type ApiKey } from './meta'

const props = defineProps<{ open: boolean; existing: string[] }>()
const emit = defineEmits<{ close: []; created: [key: ApiKey, secret: string] }>()

const name = ref('')
const mode = ref<'generated' | 'custom'>('generated')
const secret = ref('')
const saving = ref(false)
const touched = ref(false)
const server = ref<Record<string, string>>({})

watch(
  () => props.open,
  (o) => {
    if (!o) return
    name.value = ''
    mode.value = 'generated'
    secret.value = ''
    touched.value = false
    server.value = {}
  },
)

const nErr = computed(() => nameError(name.value, props.existing) || server.value.name || '')
const sErr = computed(() => (mode.value === 'custom' ? secretError(secret.value) || server.value.customSecret || '' : ''))

async function save(): Promise<void> {
  touched.value = true
  if (nErr.value || sErr.value) return
  saving.value = true
  server.value = {}
  try {
    const body: Record<string, string> = { mode: mode.value, name: name.value.trim() }
    if (mode.value === 'custom') body.customSecret = secret.value
    const res = await api.post<{ apiKey: ApiKey; secret: string }>('/api-keys', body)
    notifyKeysChanged()
    emit('created', res.data.apiKey, res.data.secret)
    emit('close')
  } catch (err) {
    const fe = fieldErrors(err)
    server.value = Object.fromEntries(Object.entries(fe).map(([k, v]) => [k.replace(/^body\./, ''), v]))
    if ((err as { code?: string }).code === 'RESOURCE_CONFLICT') server.value = { ...server.value, [mode.value === 'custom' ? 'customSecret' : 'name']: mode.value === 'custom' ? '名称或密钥已被其他 Key 使用' : '名称已存在' }
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal :open="open" title="新建下游 API Key" width="sm" :dismissible="!saving" @close="emit('close')">
    <div class="form">
      <Field label="名称" required hint="字母、数字和 _ - .，最长 64 个字符；用于在日志和统计里识别这个 Key" :error="touched || name ? nErr : ''">
        <TextInput v-model="name" mono placeholder="例如：team-a" :invalid="Boolean((touched || name) && nErr)" @enter="save" />
      </Field>
      <Field label="密钥">
        <Segmented
          v-model="mode"
          :options="[
            { value: 'generated', label: '自动生成' },
            { value: 'custom', label: '自定义' },
          ]"
        />
      </Field>
      <Field
        v-if="mode === 'custom'"
        label="自定义密钥"
        required
        hint="8～256 个字符，可用字母、数字和 - _ . ~ + / =，不能有空格或换行"
        :error="touched || secret ? sErr : ''"
      >
        <TextInput v-model="secret" type="password" mono :invalid="Boolean((touched || secret) && sErr)" @enter="save" />
      </Field>
      <p v-else class="hint">将生成一个以 ccp- 开头的随机密钥，只会显示一次。</p>
      <p class="hint">默认不限制可用模型，创建后可以在详情里设置允许的模型、渠道和限流。</p>
    </div>
    <template #footer>
      <Btn variant="ghost" :disabled="saving" @click="emit('close')">取消</Btn>
      <Btn variant="primary" :loading="saving" @click="save">创建</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.hint {
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.5;
}
</style>
