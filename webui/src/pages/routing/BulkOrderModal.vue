<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Modal from '@/components/ui/Modal.vue'
import Btn from '@/components/ui/Btn.vue'
import CheckList from '@/components/ui/CheckList.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import OrderEditor from './OrderEditor.vue'

// Pick several models, sort the union of their channels once and save it to all of them
// (bulkReplaceModelChannelOrders). The server keeps only the channels each model supports.

type OrderData = Schemas['OrderData']

const props = defineProps<{
  open: boolean
  models: { value: string; hint?: string }[]
  globalOrder: string[]
  revision: string
  initial?: string[]
}>()
const emit = defineEmits<{ close: []; saved: [revision: string] }>()

const step = ref<1 | 2>(1)
const selected = ref<string[]>([])
const union = ref<string[]>([])
const draft = ref<string[]>([])
const loading = ref(false)
const saving = ref(false)
const rev = ref('')

watch(
  () => props.open,
  (o) => {
    if (!o) return
    step.value = 1
    selected.value = [...(props.initial || [])]
    union.value = []
    rev.value = props.revision
  },
)

async function next(): Promise<void> {
  loading.value = true
  step.value = 2
  try {
    const orders = await Promise.all(
      selected.value.map((m) => api.get<OrderData>(`/load-balancing/model-orders/${enc(m)}`).then((r) => r.data).catch(() => null)),
    )
    const set = new Set<string>()
    for (const o of orders) if (o) o.order.forEach((k) => set.add(k))
    const rank = new Map(props.globalOrder.map((k, i) => [k, i]))
    union.value = [...set].sort((a, b) => (rank.get(a) ?? 1e6) - (rank.get(b) ?? 1e6))
    draft.value = [...union.value]
    const r = orders.find((o) => o)?.revision
    if (r) rev.value = r
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  saving.value = true
  try {
    const res = await api.put<Schemas['BulkOrderData']>('/load-balancing/model-orders', { modelIds: selected.value, order: draft.value }, { ifMatch: rev.value })
    toast(`已保存 ${selected.value.length} 个模型的专属顺序`, 'success')
    emit('saved', res.data.revision)
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') toast('负载均衡配置已在别处被修改', 'warning', '请关闭后刷新再试。', 7000)
    else toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    saving.value = false
  }
}

const options = computed(() => props.models.map((m) => ({ value: m.value, label: m.value, hint: m.hint })))
</script>

<template>
  <Modal :open="open" :title="step === 1 ? '批量设置模型专属顺序 · 选择模型' : `批量设置 · 已选 ${selected.length} 个模型`" width="lg" @close="emit('close')">
    <div v-if="step === 1" class="body">
      <p class="explain">选好模型后，会把这些模型可用的渠道合在一起排序，一次保存到所有所选模型。保存时每个模型只保留它真正支持的渠道。</p>
      <CheckList v-model="selected" :options="options" max-height="380px" empty-text="没有可选的模型" />
    </div>
    <div v-else class="body">
      <p class="explain">拖动或用箭头调整顺序。越靠前越优先使用。</p>
      <div v-if="loading" class="sk"><Skeleton v-for="i in 5" :key="i" h="36px" r="8px" /></div>
      <OrderEditor v-else :order="union" hide-footer empty-text="所选模型没有可用的渠道。" @change="(o: string[]) => (draft = o)" />
    </div>
    <template #footer>
      <template v-if="step === 1">
        <Btn variant="ghost" @click="emit('close')">取消</Btn>
        <Btn variant="primary" :disabled="!selected.length" @click="next">下一步（{{ selected.length }}）</Btn>
      </template>
      <template v-else>
        <Btn variant="ghost" :disabled="saving" @click="step = 1">上一步</Btn>
        <Btn variant="primary" :loading="saving" :disabled="loading || !draft.length" @click="save">保存到 {{ selected.length }} 个模型</Btn>
      </template>
    </template>
  </Modal>
</template>

<style scoped>
.body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  container-type: inline-size;
}
.explain {
  font-size: 12.5px;
  color: var(--text-2);
  line-height: 1.6;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
</style>
