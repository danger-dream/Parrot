<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, enc } from '@/api/client'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import TextInput from '@/components/ui/TextInput.vue'
import { nameError, notifyProxiesChanged, parseProxyLine, type Proxy } from './meta'

const props = defineProps<{ open: boolean; proxy: Proxy; taken: string[] }>()
const emit = defineEmits<{ close: []; saved: [proxy: Proxy] }>()

const name = ref('')
const url = ref('')
const saving = ref(false)

watch(
  () => props.open,
  (o) => {
    if (!o) return
    name.value = props.proxy.name
    url.value = ''
  },
)

const nErr = computed(() => nameError(name.value, props.taken.filter((t) => t !== props.proxy.name)))
const uErr = computed(() => (url.value.trim() ? parseProxyLine(url.value, []).error : ''))
const body = computed(() => {
  const out: Record<string, string> = {}
  if (name.value.trim() !== props.proxy.name) out.name = name.value.trim()
  if (url.value.trim()) out.url = parseProxyLine(url.value, []).url
  return out
})

async function save(): Promise<void> {
  if (nErr.value || uErr.value) return
  if (!Object.keys(body.value).length) {
    emit('close')
    return
  }
  saving.value = true
  try {
    const res = await api.patch<Proxy>(`/proxies/${enc(props.proxy.proxyId)}`, body.value, { ifMatch: props.proxy.revision })
    toast('代理已更新', 'success')
    notifyProxiesChanged()
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
  <Modal :open="open" title="编辑代理" :subtitle="proxy.name" width="md" :dismissible="!saving" @close="emit('close')">
    <div class="form">
      <Field label="名称" required hint="改名后，代理组和分流规则里的引用会一起更新" :error="nErr">
        <TextInput v-model="name" mono :invalid="Boolean(nErr)" />
      </Field>
      <Field label="新地址" :hint="`当前：${proxy.maskedUrl}。留空表示不修改。`" :error="uErr">
        <TextInput v-model="url" type="password" mono placeholder="不修改" :invalid="Boolean(uErr)" />
      </Field>
    </div>
    <template #footer>
      <Btn variant="ghost" :disabled="saving" @click="emit('close')">取消</Btn>
      <Btn variant="primary" :loading="saving" :disabled="Boolean(nErr || uErr)" @click="save">保存</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
</style>
