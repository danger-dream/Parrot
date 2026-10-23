<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { dialogs, settleDialog } from '@/stores/dialogs'
import Modal from './Modal.vue'
import Btn from './Btn.vue'

const text = ref('')
const input = ref<HTMLInputElement | HTMLTextAreaElement | null>(null)
const cur = computed(() => dialogs.current)

watch(cur, async (c) => {
  if (!c) return
  text.value = c.kind === 'prompt' ? c.opts.initial || '' : ''
  await nextTick()
  input.value?.focus()
})

const invalid = computed(() => {
  const c = cur.value
  if (!c) return null
  if (c.kind === 'confirm') return c.opts.requireText && text.value.trim() !== c.opts.requireText ? '' : null
  return c.opts.validate ? c.opts.validate(text.value) : text.value.trim() ? null : ''
})

function ok(): void {
  const c = cur.value
  if (!c || invalid.value !== null) return
  settleDialog(c.kind === 'confirm' ? true : text.value)
}
</script>

<template>
  <Modal :open="Boolean(cur)" :title="cur?.opts.title" width="sm" :layer="95" @close="settleDialog(cur?.kind === 'confirm' ? false : null)">
    <template v-if="cur">
      <p v-if="cur.opts.detail" class="detail">{{ cur.opts.detail }}</p>
      <template v-if="cur.kind === 'confirm' && cur.opts.requireText">
        <p class="label">请输入 <b class="mono">{{ cur.opts.requireText }}</b> 以确认</p>
        <input ref="input" v-model="text" class="field mono" @keydown.enter="ok" />
      </template>
      <template v-if="cur.kind === 'prompt'">
        <p v-if="cur.opts.label" class="label">{{ cur.opts.label }}</p>
        <textarea v-if="cur.opts.multiline" ref="input" v-model="text" class="field area" :class="{ mono: cur.opts.mono }" :placeholder="cur.opts.placeholder" rows="6" />
        <input v-else ref="input" v-model="text" class="field" :class="{ mono: cur.opts.mono }" :placeholder="cur.opts.placeholder" @keydown.enter="ok" />
        <p v-if="invalid" class="err">{{ invalid }}</p>
      </template>
    </template>
    <template #footer>
      <Btn variant="ghost" @click="settleDialog(cur?.kind === 'confirm' ? false : null)">{{ (cur?.kind === 'confirm' && cur.opts.cancelText) || '取消' }}</Btn>
      <Btn :variant="cur?.kind === 'confirm' && cur.opts.danger ? 'danger' : 'primary'" :disabled="invalid !== null" @click="ok">
        {{ cur?.opts.confirmText || '确定' }}
      </Btn>
    </template>
  </Modal>
</template>

<style scoped>
.detail {
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-2);
  white-space: pre-line;
}
.label {
  margin: 12px 0 6px;
  font-size: 12.5px;
  color: var(--text-2);
}
.field {
  width: 100%;
  height: 32px;
  padding: 0 10px;
  border: 0;
  outline: 0;
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border-strong);
  font-size: 13px;
}
.field:focus {
  box-shadow: inset 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft);
}
.area {
  height: auto;
  padding: 8px 10px;
  resize: vertical;
}
.err {
  margin-top: 6px;
  font-size: 12px;
  color: var(--red);
}
</style>
