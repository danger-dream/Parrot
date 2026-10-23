<script setup lang="ts">
import { RotateCcw, Save } from '@lucide/vue'
import Btn from './Btn.vue'
import Skeleton from './Skeleton.vue'

defineProps<{
  title: string
  description?: string
  dirty?: boolean
  saving?: boolean
  loading?: boolean
  error?: string
}>()
const emit = defineEmits<{ save: []; reset: [] }>()
</script>

<template>
  <section class="sec" :class="{ dirty }">
    <header class="head">
      <div class="titles">
        <h3>{{ title }}</h3>
        <p v-if="description" class="desc">{{ description }}</p>
      </div>
      <div v-if="$slots.actions" class="actions"><slot name="actions" /></div>
    </header>
    <div class="body">
      <p v-if="error" class="err">{{ error }}</p>
      <div v-else-if="loading" class="sk"><Skeleton h="16px" /><Skeleton h="16px" w="70%" /><Skeleton h="16px" w="85%" /></div>
      <slot v-else />
    </div>
    <Transition name="bar">
      <footer v-if="dirty" class="foot">
        <span class="note">有未保存的修改</span>
        <Btn variant="ghost" size="sm" :disabled="saving" @click="emit('reset')"><RotateCcw /> 还原</Btn>
        <Btn variant="primary" size="sm" :loading="saving" @click="emit('save')"><Save /> 保存</Btn>
      </footer>
    </Transition>
  </section>
</template>

<style scoped>
.sec {
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--card-highlight), inset 0 0 0 0.5px var(--border);
  transition: box-shadow 0.2s;
}
.sec.dirty {
  box-shadow: var(--card-highlight), inset 0 0 0 1px color-mix(in srgb, var(--accent) 50%, transparent);
}
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px 12px;
  padding: 16px 18px 4px;
}
.titles {
  min-width: 0;
}
h3 {
  font-size: 14px;
  font-weight: 650;
}
.desc {
  margin-top: 3px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-3);
  max-width: 640px;
}
.actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.body {
  padding: 6px 18px 12px;
}
.foot {
  position: sticky;
  bottom: 0;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  padding: 10px 14px;
  border-top: 0.5px solid var(--hairline);
  border-radius: 0 0 var(--radius-lg) var(--radius-lg);
  background: var(--popover);
  backdrop-filter: blur(16px);
}
.note {
  margin-right: auto;
  font-size: 12px;
  color: var(--accent);
}
.err {
  padding: 12px 0;
  font-size: 12.5px;
  color: var(--red);
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 8px 0;
}
.bar-enter-active,
.bar-leave-active {
  transition: opacity 0.2s, transform 0.2s var(--ease-out);
}
.bar-enter-from,
.bar-leave-to {
  opacity: 0;
  transform: translateY(6px);
}
</style>
