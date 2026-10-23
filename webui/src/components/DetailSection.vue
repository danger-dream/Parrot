<script setup lang="ts">
defineProps<{ title: string; note?: string }>()
</script>

<template>
  <section class="ds">
    <header>
      <p class="t">{{ title }}<span v-if="note" class="n">{{ note }}</span></p>
      <div v-if="$slots.actions" class="a"><slot name="actions" /></div>
    </header>
    <slot />
  </section>
</template>

<style scoped>
.ds {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.t {
  font-size: 12.5px;
  font-weight: 650;
}
.n {
  margin-left: 6px;
  font-weight: 400;
  color: var(--text-3);
}
.a {
  display: flex;
  gap: 4px;
}
</style>
