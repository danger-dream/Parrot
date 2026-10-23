<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, enc } from '@/api/client'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import { sourceLabel, waitText, type ApiKey } from './meta'

const props = defineProps<{ open: boolean; k: ApiKey }>()
const emit = defineEmits<{ close: []; saved: [key: ApiKey] }>()

type Tri = 'inherit' | 'on' | 'off'
type Num = 'inherit' | 'set'

const enabled = ref<Tri>('inherit')
const conc = ref<Num>('inherit')
const concVal = ref<number | null>(0)
const queue = ref<Num>('inherit')
const queueVal = ref<number | null>(0)
const wait = ref<Num>('inherit')
const waitVal = ref<number | null>(0)
const saving = ref(false)

watch(
  () => props.open,
  (o) => {
    if (!o) return
    const ov = props.k.limitOverride || {}
    const lim = props.k.limiter
    enabled.value = ov.enabled === true ? 'on' : ov.enabled === false ? 'off' : 'inherit'
    conc.value = ov.maxConcurrent != null ? 'set' : 'inherit'
    concVal.value = ov.maxConcurrent ?? lim.maxConcurrent
    queue.value = ov.maxQueue != null ? 'set' : 'inherit'
    queueVal.value = ov.maxQueue ?? lim.maxQueue
    wait.value = ov.queueWaitSeconds != null ? 'set' : 'inherit'
    waitVal.value = ov.queueWaitSeconds ?? lim.queueWaitSeconds
  },
)

const invalid = computed(
  () => (conc.value === 'set' && concVal.value == null) || (queue.value === 'set' && queueVal.value == null) || (wait.value === 'set' && waitVal.value == null),
)

async function submit(body: Record<string, unknown>, text: string): Promise<void> {
  saving.value = true
  try {
    const res = await api.patch<ApiKey>(`/api-keys/${enc(props.k.keyId)}`, body, { ifMatch: props.k.revision })
    toast(text, 'success')
    emit('saved', res.data)
    emit('close')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    saving.value = false
  }
}
function save(): void {
  if (invalid.value) return
  submit(
    {
      limitOverride: {
        enabled: enabled.value === 'inherit' ? null : enabled.value === 'on',
        maxConcurrent: conc.value === 'set' ? concVal.value : null,
        maxQueue: queue.value === 'set' ? queueVal.value : null,
        queueWaitSeconds: wait.value === 'set' ? waitVal.value : null,
      },
    },
    '限流设置已保存',
  )
}
function restore(): void {
  submit({ limitOverride: null }, '已恢复默认限流')
}
const numOpts = [
  { value: 'inherit' as Num, label: '继承默认' },
  { value: 'set' as Num, label: '单独设置' },
]
</script>

<template>
  <Modal :open="open" title="请求限流" :subtitle="k.name" width="md" :dismissible="!saving" @close="emit('close')">
    <p class="lead">并发满后新请求进入先进先出的队列；队列满、等待超时或客户端断开时会自动移出并返回。未单独设置的项目沿用「系统设置 · 并发限制」中的默认值。</p>
    <div class="form">
      <Field label="限流开关" :hint="`当前生效：${k.limiter.enabled ? '开' : '关'}（${sourceLabel(k.limiter.enabledSource)}）`">
        <Segmented
          v-model="enabled"
          size="sm"
          :options="[
            { value: 'inherit', label: '继承默认' },
            { value: 'on', label: '开启' },
            { value: 'off', label: '关闭' },
          ]"
        />
      </Field>
      <Field label="并发上限" :hint="`0 表示不限。当前生效：${k.limiter.unlimited ? '不限' : k.limiter.maxConcurrent}（${sourceLabel(k.limiter.maxConcurrentSource)}）`">
        <div class="row">
          <Segmented v-model="conc" size="sm" :options="numOpts" />
          <NumberInput v-if="conc === 'set'" v-model="concVal" :min="0" integer unit="个" width="120px" />
        </div>
      </Field>
      <Field label="队列上限" :hint="`0 表示不排队。当前生效：${k.limiter.maxQueue}（${sourceLabel(k.limiter.maxQueueSource)}）`">
        <div class="row">
          <Segmented v-model="queue" size="sm" :options="numOpts" />
          <NumberInput v-if="queue === 'set'" v-model="queueVal" :min="0" integer unit="个" width="120px" />
        </div>
      </Field>
      <Field label="最长等待" :hint="`0 表示不等待。当前生效：${waitText(k.limiter.queueWaitSeconds)}（${sourceLabel(k.limiter.queueWaitSource)}）`">
        <div class="row">
          <Segmented v-model="wait" size="sm" :options="numOpts" />
          <NumberInput v-if="wait === 'set'" v-model="waitVal" :min="0" integer unit="秒" width="120px" />
          <span v-if="wait === 'set' && waitVal" class="faint small">= {{ waitText(waitVal) }}</span>
        </div>
      </Field>
    </div>
    <template #footer>
      <Btn v-if="k.limitOverride" variant="ghost" :disabled="saving" @click="restore">恢复默认</Btn>
      <span class="grow" />
      <Btn variant="ghost" :disabled="saving" @click="emit('close')">取消</Btn>
      <Btn variant="primary" :loading="saving" :disabled="invalid" @click="save">保存</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.lead {
  margin-bottom: 14px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-2);
}
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.small {
  font-size: 12px;
}
.grow {
  flex: 1;
}
</style>
