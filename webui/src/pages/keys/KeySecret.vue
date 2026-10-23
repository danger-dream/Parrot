<script setup lang="ts">
import { ref } from 'vue'
import { Check, Copy } from '@lucide/vue'

// The full downstream key with a one-click copy button. Until the backend returns `secret`,
// only the masked hint is available and copying is disabled.
const props = defineProps<{ secret: string | null; masked: string; size?: 'sm' | 'md' }>()
const done = ref(false)

async function copy(): Promise<void> {
  if (!props.secret) return
  try {
    await navigator.clipboard.writeText(props.secret)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = props.secret
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
  <div class="ks" :class="[size === 'md' ? 'md' : 'sm', { missing: !secret }]" @click.stop>
    <code class="v" :title="secret || '后端尚未返回完整密钥'">{{ secret || masked }}</code>
    <button type="button" class="b" :class="{ done }" :disabled="!secret" :title="secret ? (done ? '已复制' : '复制完整 Key') : '后端尚未返回完整密钥，暂时无法复制'" @click.stop="copy">
      <Check v-if="done" /><Copy v-else />
      <span v-if="size === 'md'">{{ done ? '已复制' : '复制' }}</span>
    </button>
  </div>
</template>

<style scoped>
.ks {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  padding: 3px 3px 3px 9px;
  border-radius: 8px;
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
  cursor: default;
}
.v {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11.5px;
  color: var(--text);
  user-select: all;
}
.md {
  padding: 5px 5px 5px 12px;
}
.md .v {
  font-size: 12.5px;
  white-space: normal;
  word-break: break-all;
}
.missing .v {
  color: var(--text-3);
  user-select: text;
}
.b {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-width: 28px;
  height: 26px;
  padding: 0 6px;
  border-radius: 6px;
  flex-shrink: 0;
  font-size: 12px;
  color: var(--accent);
  background: var(--accent-soft);
}
.md .b {
  height: 30px;
  padding: 0 10px;
}
.b:disabled {
  color: var(--text-3);
  background: var(--fill-2);
}
.b.done {
  color: var(--green);
  background: color-mix(in srgb, var(--green) 14%, transparent);
}
.b svg {
  width: 13px;
  height: 13px;
}
</style>
