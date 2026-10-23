<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, enc } from '@/api/client'
import type { Operation } from '@/api/operations'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Modal from '@/components/ui/Modal.vue'
import ModelPicker from './ModelPicker.vue'
import { runOperation, type Channel, type DiscoveryResult, type ModelPair } from './meta'

const props = defineProps<{ open: boolean; channel: Channel }>()
const emit = defineEmits<{ close: []; saved: [channel: Channel] }>()

const models = ref<ModelPair[]>([])
const saving = ref(false)

watch(
  () => props.open,
  (o) => {
    if (o) models.value = props.channel.models.map((m) => ({ real: m.real, alias: m.alias }))
  },
  { immediate: true },
)

const dupAlias = computed(() => new Set(models.value.map((m) => m.alias)).size !== models.value.length)
const changed = computed(() => JSON.stringify(models.value) !== JSON.stringify(props.channel.models.map((m) => ({ real: m.real, alias: m.alias }))))

function discover() {
  return runOperation<DiscoveryResult>(() => api.post<Operation>('/channel-model-discoveries', { source: 'existing', channelId: props.channel.id }))
}

async function save(): Promise<void> {
  saving.value = true
  try {
    const res = await api.patch<Channel>(`/channels/${enc(props.channel.id)}`, { models: models.value }, { ifMatch: props.channel.revision })
    toast(`模型列表已更新（${models.value.length} 个）`, 'success')
    emit('saved', res.data)
    emit('close')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal :open="open" title="管理模型" :subtitle="channel.name" width="lg" :dismissible="!saving" @close="emit('close')">
    <ModelPicker v-model="models" :discover="discover" />
    <p v-if="!models.length" class="err">至少保留一个模型</p>
    <p v-else-if="dupAlias" class="err">别名不能重复</p>
    <template #footer>
      <Btn variant="ghost" :disabled="saving" @click="emit('close')">取消</Btn>
      <Btn variant="primary" :loading="saving" :disabled="!models.length || dupAlias || !changed" @click="save">保存（{{ models.length }} 个）</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.err {
  margin-top: 8px;
  font-size: 12px;
  color: var(--red);
}
</style>
