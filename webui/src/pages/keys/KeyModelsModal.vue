<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, enc } from '@/api/client'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import CheckList from '@/components/ui/CheckList.vue'
import Modal from '@/components/ui/Modal.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import { loadModelOptions, type ApiKey } from './meta'

const props = defineProps<{ open: boolean; k: ApiKey }>()
const emit = defineEmits<{ close: []; saved: [key: ApiKey] }>()

const options = ref<{ value: string; hint?: string }[] | null>(null)
const loadError = ref('')
const selected = ref<string[]>([])
const saving = ref(false)

watch(
  () => props.open,
  async (o) => {
    if (!o) return
    selected.value = [...props.k.allowedModels]
    loadError.value = ''
    try {
      options.value = await loadModelOptions()
    } catch (err) {
      loadError.value = errorTitle(err)
      options.value = []
    }
  },
)

const allOptions = computed(() => {
  const base = options.value || []
  const known = new Set(base.map((o) => o.value))
  const missing = props.k.allowedModels.filter((m) => !known.has(m)).map((m) => ({ value: m, hint: '当前不可用（已保留）' }))
  return [...missing, ...base]
})

async function save(): Promise<void> {
  saving.value = true
  try {
    const res = await api.patch<ApiKey>(`/api-keys/${enc(props.k.keyId)}`, { allowedModels: selected.value }, { ifMatch: props.k.revision })
    toast(selected.value.length ? `已允许 ${selected.value.length} 个模型` : '已设为不限制模型', 'success')
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
  <Modal :open="open" title="允许使用的模型" :subtitle="k.name" width="md" :dismissible="!saving" @close="emit('close')">
    <p class="lead">勾选这个 Key 能调用的模型。<b>一个都不选表示不限制</b>。图片、视频接口还需要单独开启对应权限。</p>
    <p v-if="loadError" class="c-red small">模型列表加载失败：{{ loadError }}</p>
    <Skeleton v-if="!options" h="260px" r="10px" />
    <CheckList v-else v-model="selected" :options="allOptions" max-height="340px" empty-text="当前没有可用模型，请先添加渠道或 OAuth 账号" />
    <template #footer>
      <Btn variant="ghost" :disabled="saving" @click="emit('close')">取消</Btn>
      <Btn variant="primary" :loading="saving" :disabled="!options" @click="save">{{ selected.length ? `保存（${selected.length} 个）` : '保存（不限制）' }}</Btn>
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
.small {
  font-size: 12px;
  margin-bottom: 8px;
}
</style>
