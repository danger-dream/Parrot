<script setup lang="ts">
defineProps<{ title?: string; note?: string }>()
</script>

<template>
  <section class="sb">
    <header v-if="title || $slots.actions" class="sbh">
      <p class="sbt">{{ title }}<span v-if="note" class="sbn">{{ note }}</span></p>
      <div v-if="$slots.actions" class="sba"><slot name="actions" /></div>
    </header>
    <slot />
    <footer v-if="$slots.footer" class="sbf"><slot name="footer" /></footer>
  </section>
</template>

<style scoped>
.sb {
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 11px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  min-width: 0;
}
.sbh {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-height: 22px;
}
.sbt {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-2);
}
.sbn {
  margin-left: 6px;
  font-weight: 400;
  color: var(--text-3);
}
.sba {
  display: flex;
  gap: 4px;
  margin-right: -6px;
}
.sbf {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  padding-top: 9px;
  border-top: 0.5px solid var(--hairline);
}
</style>
