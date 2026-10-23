<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check } from '@lucide/vue'
import Modal from '@/components/ui/Modal.vue'
import SearchField from '@/components/ui/SearchField.vue'
import Badge from '@/components/ui/Badge.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Empty from '@/components/ui/Empty.vue'
import { KIND_LABEL, loadModels, modelStatus, modelStore, priceSummary, tokens } from './shared'

// Searchable picker over the model center inventory. Emits the chosen client model id.

const props = withDefaults(
  defineProps<{
    open: boolean
    title?: string
    subtitle?: string
    kinds?: string[]
    current?: string | null
    exclude?: string[]
  }>(),
  { title: '选择模型', kinds: () => ['chat'] },
)
const emit = defineEmits<{ close: []; pick: [modelId: string] }>()

const q = ref('')
watch(
  () => props.open,
  (o) => {
    if (o) {
      q.value = ''
      loadModels()
    }
  },
)

const list = computed(() => {
  const s = q.value.trim().toLowerCase()
  const ex = new Set(props.exclude || [])
  return modelStore.models
    .filter((m) => props.kinds.includes(m.identity.type) && !ex.has(m.modelId))
    .filter((m) => !s || m.modelId.toLowerCase().includes(s) || (m.aliases || []).some((a) => a.toLowerCase().includes(s)))
})
</script>

<template>
  <Modal :open="open" :title="title" :subtitle="subtitle" width="md" @close="emit('close')">
    <div class="picker">
      <SearchField v-model="q" placeholder="搜索模型名或别名" />
      <div class="list">
        <template v-if="modelStore.loading && !modelStore.models.length">
          <Skeleton v-for="i in 6" :key="i" h="36px" r="8px" />
        </template>
        <button
          v-for="m in list"
          :key="m.resourceKey"
          type="button"
          class="opt"
          :class="{ on: m.modelId === current }"
          @click="emit('pick', m.modelId)"
        >
          <span class="main">
            <span class="mono truncate">{{ m.modelId }}</span>
            <span class="sub faint">
              {{ KIND_LABEL[m.identity.type] }} · {{ m.sourceCount }} 个来源
              <template v-if="modelStore.metadata[m.modelId]?.effective.contextWindow"> · 上下文 {{ tokens(modelStore.metadata[m.modelId]?.effective.contextWindow) }}</template>
              <template v-if="priceSummary(modelStore.metadata[m.modelId]?.effective)"> · {{ priceSummary(modelStore.metadata[m.modelId]?.effective) }}</template>
            </span>
          </span>
          <Badge :tone="modelStatus(m).tone">{{ modelStatus(m).label }}</Badge>
          <Check v-if="m.modelId === current" class="ck" />
        </button>
        <Empty v-if="!modelStore.loading && !list.length" title="没有匹配的模型" detail="换个关键词试试" />
      </div>
    </div>
  </Modal>
</template>

<style scoped>
.picker {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: min(56dvh, 460px);
  overflow-y: auto;
  margin: 0 -6px;
  padding: 0 6px;
}
.opt {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 44px;
  padding: 6px 10px;
  border-radius: 8px;
  text-align: left;
  width: 100%;
}
.opt:hover {
  background: var(--fill-3);
}
.opt.on {
  background: var(--accent-soft);
}
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.main .mono {
  font-size: 12.5px;
}
.sub {
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}
.ck {
  width: 14px;
  height: 14px;
  color: var(--accent);
  flex-shrink: 0;
}
</style>
