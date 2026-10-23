<script setup lang="ts">
import { ref, watch } from 'vue'
import { ShieldAlert } from '@lucide/vue'
import Btn from '@/components/ui/Btn.vue'
import CopyText from '@/components/ui/CopyText.vue'
import Modal from '@/components/ui/Modal.vue'

// One-time display of a key secret returned by the server. The parent must drop the secret
// from its state on close so it can never be shown again.

const props = defineProps<{ secret: string | null; name: string; title?: string; note?: string }>()
const emit = defineEmits<{ close: [] }>()
const copied = ref(false)

watch(
  () => props.secret,
  () => (copied.value = false),
)
function copy(): void {
  copied.value = true
}
</script>

<template>
  <Modal :open="Boolean(secret)" :title="title || 'API Key 已创建'" :subtitle="name" width="md" :dismissible="false" @close="emit('close')">
    <div class="body">
      <p v-if="note" class="note">{{ note }}</p>
      <div class="box" @click.capture="copy">
        <CopyText :text="secret || ''" class="secret" />
      </div>
      <p class="warn"><ShieldAlert /> 请立即复制并妥善保存。关闭后无法再次查看。</p>
    </div>
    <template #footer>
      <Btn :variant="copied ? 'primary' : 'secondary'" @click="emit('close')">{{ copied ? '我已保存，关闭' : '关闭' }}</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.note {
  font-size: 12.5px;
  color: var(--text-2);
  line-height: 1.6;
}
.box {
  padding: 12px 12px 12px 14px;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border-strong);
}
.secret {
  width: 100%;
  font-size: 13px;
}
.secret :deep(.t) {
  flex: 1;
  white-space: normal;
  word-break: break-all;
}
.warn {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--orange);
}
.warn svg {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
}
</style>
