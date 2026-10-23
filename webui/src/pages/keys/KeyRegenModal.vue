<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { TriangleAlert } from '@lucide/vue'
import { api, enc, type Schemas } from '@/api/client'
import { toast } from '@/stores/ui'
import { dateTime, relative } from '@/utils/format'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import TextInput from '@/components/ui/TextInput.vue'
import { secretError, type ApiKey } from './meta'

type Plan = Schemas['ApiKeyReplacementPlanData']

// mode "generate": plan → confirm → server generates a new secret (shown once by the parent).
// mode "custom": the user supplies the new secret (replaceApiKeySecret).
const props = defineProps<{ open: boolean; k: ApiKey; mode: 'generate' | 'custom' }>()
const emit = defineEmits<{ close: []; done: [key: ApiKey, secret: string] }>()

const plan = ref<Plan | null>(null)
const planError = ref('')
const secret = ref('')
const touched = ref(false)
const saving = ref(false)

watch(
  () => props.open,
  async (o) => {
    plan.value = null
    planError.value = ''
    secret.value = ''
    touched.value = false
    if (!o || props.mode !== 'generate') return
    try {
      plan.value = (await api.post<Plan>(`/api-keys/${enc(props.k.keyId)}/actions/generate-replacement-plan`)).data
    } catch (err) {
      planError.value = errorTitle(err)
    }
  },
)

const sErr = computed(() => (props.mode === 'custom' ? secretError(secret.value) : ''))
const expired = computed(() => Boolean(plan.value && new Date(plan.value.expiresAt).getTime() < Date.now()))

async function confirm(): Promise<void> {
  touched.value = true
  if (props.mode === 'custom' && sErr.value) return
  saving.value = true
  try {
    const res =
      props.mode === 'generate'
        ? await api.post<{ apiKey: ApiKey; secret: string }>(`/api-keys/${enc(props.k.keyId)}/actions/generate-replacement`, {
            planId: plan.value!.planId,
            planToken: plan.value!.planToken,
          })
        : await api.put<{ apiKey: ApiKey; secret: string }>(`/api-keys/${enc(props.k.keyId)}/secret`, { customSecret: secret.value }, { ifMatch: props.k.revision })
    plan.value = null
    emit('done', res.data.apiKey, res.data.secret)
    emit('close')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal :open="open" :title="mode === 'generate' ? '重新生成密钥' : '替换为自定义密钥'" :subtitle="k.name" width="sm" :dismissible="!saving" @close="emit('close')">
    <div class="body">
      <div class="warn">
        <TriangleAlert />
        <p>当前密钥 <b class="mono">{{ k.maskedHint }}</b> 会在确认后<b>立即失效</b>，正在使用它的下游客户端需要更新为新密钥。这个 Key 的排队和限流状态也会被清空。</p>
      </div>
      <template v-if="mode === 'generate'">
        <p v-if="planError" class="c-red">无法生成计划：{{ planError }}</p>
        <Skeleton v-else-if="!plan" h="40px" r="8px" />
        <p v-else class="faint small">
          已生成替换计划，请在 {{ relative(plan.expiresAt) }}（{{ dateTime(plan.expiresAt) }}）之前确认。新密钥会以 ccp- 开头，只显示一次。
        </p>
        <p v-if="expired" class="c-orange small">计划已过期，请关闭后重新打开。</p>
      </template>
      <Field
        v-else
        label="新密钥"
        required
        hint="8～256 个字符，可用字母、数字和 - _ . ~ + / =，不能有空格或换行"
        :error="touched || secret ? sErr : ''"
      >
        <TextInput v-model="secret" type="password" mono :invalid="Boolean((touched || secret) && sErr)" @enter="confirm" />
      </Field>
    </div>
    <template #footer>
      <Btn variant="ghost" :disabled="saving" @click="emit('close')">取消</Btn>
      <Btn variant="danger" :loading="saving" :disabled="mode === 'generate' && (!plan || expired)" @click="confirm">
        {{ mode === 'generate' ? '确认重新生成' : '确认替换' }}
      </Btn>
    </template>
  </Modal>
</template>

<style scoped>
.body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.warn {
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 10px;
  font-size: 12.5px;
  line-height: 1.6;
  background: color-mix(in srgb, var(--orange) 12%, transparent);
}
.warn svg {
  width: 16px;
  height: 16px;
  color: var(--orange);
  flex-shrink: 0;
  margin-top: 3px;
}
.small {
  font-size: 12px;
  line-height: 1.5;
}
</style>
