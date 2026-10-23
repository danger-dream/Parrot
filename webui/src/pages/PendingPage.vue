<script setup lang="ts">
import { computed } from 'vue'
import { Check, Hammer } from '@lucide/vue'
import { findNav } from '@/nav'

const props = defineProps<{ navKey: string }>()
const item = computed(() => findNav(props.navKey))
</script>

<template>
  <div v-if="item" class="page">
    <div class="card">
      <span class="ic" :style="{ color: item.tint, background: `color-mix(in srgb, ${item.tint} 14%, transparent)` }">
        <component :is="item.icon" />
      </span>
      <h2>{{ item.label }}</h2>
      <p class="summary">{{ item.summary }}</p>
      <p class="phase"><Hammer /> 正在建设中，目前仍可在 Telegram Bot 中使用这些功能。</p>
      <ul v-if="item.features?.length" class="features">
        <li v-for="f in item.features" :key="f"><Check /> {{ f }}</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  justify-content: center;
  padding: 48px 16px;
}
.card {
  width: min(520px, 100%);
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 10px;
  padding: 36px 32px;
  border-radius: 20px;
  background: var(--surface);
  box-shadow: var(--card-highlight), inset 0 0 0 0.5px var(--border);
  animation: fade-up 0.5s var(--ease-out) both;
}
.ic {
  width: 56px;
  height: 56px;
  border-radius: 16px;
  display: grid;
  place-items: center;
}
.ic svg {
  width: 28px;
  height: 28px;
}
h2 {
  margin-top: 6px;
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 700;
}
.summary {
  color: var(--text-2);
}
.phase {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 999px;
  font-size: 12px;
  color: var(--text-2);
  background: var(--fill-3);
}
.phase svg {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
}
.features {
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  text-align: left;
  width: 100%;
}
.features li {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
}
.features svg {
  width: 15px;
  height: 15px;
  margin-top: 2px;
  color: var(--green);
  flex-shrink: 0;
}
</style>
