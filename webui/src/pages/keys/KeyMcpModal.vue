<script setup lang="ts">
import { ref, watch } from 'vue'
import { api, enc } from '@/api/client'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import CheckList from '@/components/ui/CheckList.vue'
import Modal from '@/components/ui/Modal.vue'
import { MCP_TOOLS, type ApiKey, type McpTool } from './meta'

const props = defineProps<{ open: boolean; k: ApiKey }>()
const emit = defineEmits<{ close: []; saved: [key: ApiKey] }>()

const selected = ref<string[]>([])
const saving = ref(false)

watch(
  () => props.open,
  (o) => {
    if (o) selected.value = [...props.k.mcpTools]
  },
)

async function save(): Promise<void> {
  saving.value = true
  try {
    const res = await api.patch<ApiKey>(`/api-keys/${enc(props.k.keyId)}`, { mcpTools: selected.value as McpTool[] }, { ifMatch: props.k.revision })
    toast('MCP 工具设置已保存', 'success')
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
  <Modal :open="open" title="MCP 工具授权" :subtitle="k.name" width="sm" :dismissible="!saving" @close="emit('close')">
    <p class="lead">一个都不选表示<b>跟随全局开关</b>（服务端开放的工具都能用）。勾选后只允许选中的工具；服务端全局关闭的工具无法在这里放开。</p>
    <CheckList v-model="selected" :options="MCP_TOOLS.map((t) => ({ value: t.value, label: t.label, hint: t.value }))" :searchable="false" />
    <template #footer>
      <Btn variant="ghost" :disabled="saving" @click="emit('close')">取消</Btn>
      <Btn variant="primary" :loading="saving" @click="save">{{ selected.length ? `保存（${selected.length} 个）` : '保存（跟随全局）' }}</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.lead {
  margin-bottom: 10px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-2);
}
</style>
