<script setup lang="ts">
/** The models of one source (OAuth account or API channel): the same rows wherever a source lists its models. */
</script>

<template>
  <ul class="model-list"><slot /></ul>
</template>

<style scoped>
.model-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
</style>
