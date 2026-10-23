<script setup lang="ts">
defineProps<{
  items: { label: string; value?: string | number | null; mono?: boolean; tone?: 'green' | 'red' | 'orange' | 'blue'; full?: boolean; private?: boolean }[]
  columns?: 1 | 2 | 3
}>()
</script>

<template>
  <dl class="kv" :style="{ '--cols': columns || 2 }">
    <div v-for="(it, i) in items" :key="i" class="item" :class="{ full: it.full }">
      <dt>{{ it.label }}</dt>
      <dd :class="[{ mono: it.mono }, it.tone ? `c-${it.tone}` : '']" :data-private="it.private ? '' : undefined">
        <slot :name="`v${i}`" :item="it">{{ it.value === null || it.value === undefined || it.value === '' ? '—' : it.value }}</slot>
      </dd>
    </div>
  </dl>
</template>

<style scoped>
.kv {
  display: grid;
  grid-template-columns: repeat(var(--cols), minmax(0, 1fr));
  gap: 10px 16px;
  margin: 0;
}
.item {
  min-width: 0;
}
.item.full {
  grid-column: 1 / -1;
}
dt {
  font-size: 11px;
  color: var(--text-3);
}
dd {
  margin: 2px 0 0;
  font-size: 12.5px;
  word-break: break-word;
}
dd.mono {
  font-size: 12px;
}
</style>
