<script setup lang="ts">
import { ref } from 'vue'
import { Check, Copy } from '@lucide/vue'

const props = defineProps<{ text: string; display?: string; mono?: boolean }>()
const done = ref(false)

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(props.text)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = props.text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    ta.remove()
  }
  done.value = true
  window.setTimeout(() => (done.value = false), 1400)
}
</script>

<template>
  <span class="ct">
    <span class="t" :class="{ mono: mono !== false }">{{ display ?? text }}</span>
    <button type="button" class="b" :class="{ done }" :title="done ? '已复制' : '复制'" @click.stop="copy">
      <Check v-if="done" /><Copy v-else />
    </button>
  </span>
</template>

<style scoped>
.ct {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  min-width: 0;
}
.t {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.b {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  border-radius: 5px;
  color: var(--text-3);
}
.b:hover {
  color: var(--text);
  background: var(--fill-2);
}
.b.done {
  color: var(--green);
}
.b svg {
  width: 12px;
  height: 12px;
}
</style>
