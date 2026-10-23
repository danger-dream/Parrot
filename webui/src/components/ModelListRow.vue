<script setup lang="ts">
import { openModel } from '@/links'

/**
 * One model of a source. `off` greys the row out (disabled here or in the model center);
 * `meta` is the limits line; problem badges go into #badges, controls into #side, extra lines into the default slot.
 */
defineProps<{ id: string; off?: boolean; meta?: string; metaTitle?: string }>()
</script>

<template>
  <li class="row" :class="{ off }">
    <div class="main">
      <p class="line">
        <button type="button" class="mid mono truncate" :title="`查看模型 ${id}`" @click="openModel(id)">{{ id }}</button>
        <slot name="badges" />
      </p>
      <p class="meta truncate" :title="metaTitle">{{ meta || '暂无元数据' }}</p>
      <slot />
    </div>
    <div v-if="$slots.side" class="side"><slot name="side" /></div>
  </li>
</template>

<style scoped>
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 40px;
  padding: 6px 10px;
  border-bottom: 0.5px solid var(--hairline);
}
.row:last-child {
  border-bottom: 0;
}
.row.off .main {
  opacity: 0.5;
}
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.mid {
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
  text-align: left;
  max-width: 100%;
}
.mid:hover {
  color: var(--accent);
  text-decoration: underline;
}
.meta,
.main :slotted(.sub) {
  font-size: 11px;
  color: var(--text-3);
  overflow-wrap: anywhere;
}
.side {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
</style>
