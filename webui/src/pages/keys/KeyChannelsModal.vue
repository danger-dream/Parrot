<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, enc } from '@/api/client'
import { catalog, refreshCatalog } from '@/stores/catalog'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import CheckList from '@/components/ui/CheckList.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import Switch from '@/components/ui/Switch.vue'
import type { ApiKey } from './meta'

const props = defineProps<{ open: boolean; k: ApiKey }>()
const emit = defineEmits<{ close: []; saved: [key: ApiKey] }>()

const selected = ref<string[]>([])
const binding = ref(false)
const saving = ref(false)

watch(
  () => props.open,
  (o) => {
    if (!o) return
    selected.value = [...(props.k.allowedChannels || [])]
    binding.value = props.k.channelBindingEnabled
    refreshCatalog().catch(() => undefined)
  },
)

const options = computed(() => {
  const list = Object.entries(catalog.channels).map(([id, c]) => ({
    value: id,
    label: c.name,
    hint: `${c.type === 'oauth' ? 'OAuth 账号' : 'API 渠道'} · ${id}`,
  }))
  for (const id of props.k.allowedChannels || []) if (!catalog.channels[id]) list.unshift({ value: id, label: id, hint: '已不存在（已保留）' })
  return list
})
const invalid = computed(() => binding.value && !selected.value.length)

async function save(): Promise<void> {
  if (invalid.value) return
  saving.value = true
  try {
    const res = await api.patch<ApiKey>(
      `/api-keys/${enc(props.k.keyId)}`,
      { allowedChannels: selected.value, channelBindingEnabled: binding.value },
      { ifMatch: props.k.revision },
    )
    toast('渠道设置已保存', 'success')
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
  <Modal :open="open" title="允许的渠道" :subtitle="k.name" width="md" :dismissible="!saving" @close="emit('close')">
    <div class="form">
      <Field inline label="启用渠道绑定" hint="开启后，这个 Key 的请求只会发往下面选中的渠道或账号；关闭时选择会被保留但不生效。">
        <Switch v-model="binding" label="启用渠道绑定" />
      </Field>
      <CheckList v-model="selected" :options="options" max-height="320px" empty-text="没有可选的渠道" />
      <p v-if="invalid" class="err">开启绑定前请至少选择一个渠道</p>
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
  gap: 10px;
}
.err {
  font-size: 12px;
  color: var(--red);
}
.form :deep(.label) {
  font-family: inherit;
}
</style>
