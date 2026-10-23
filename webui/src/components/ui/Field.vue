<script setup lang="ts">
withDefaults(defineProps<{ label?: string; hint?: string; error?: string; inline?: boolean; required?: boolean }>(), {
  inline: false,
})
</script>

<template>
  <div class="field" :class="{ inline }">
    <div v-if="label || hint" class="meta">
      <label v-if="label" class="label">{{ label }}<span v-if="required" class="req">*</span></label>
      <p v-if="hint && inline" class="hint">{{ hint }}</p>
    </div>
    <div class="control">
      <slot />
      <p v-if="hint && !inline" class="hint">{{ hint }}</p>
      <p v-if="error" class="error">{{ error }}</p>
    </div>
  </div>
</template>

<style scoped>
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.field.inline {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 0;
  border-bottom: 0.5px solid var(--hairline);
}
.field.inline:last-child {
  border-bottom: 0;
}
.inline .meta {
  flex: 1;
  min-width: 0;
}
.inline .control {
  flex: 0 1 auto;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  min-width: 0;
}
.label {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text);
}
.inline .label {
  font-size: 13px;
  font-weight: 400;
}
.req {
  margin-left: 2px;
  color: var(--red);
}
.hint {
  margin-top: 2px;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-3);
}
.error {
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--red);
}
@media (max-width: 560px) {
  .field.inline {
    flex-wrap: wrap;
    gap: 8px 12px;
  }
  .inline .meta {
    flex: 1 1 180px;
  }
  .inline .control {
    margin-left: auto;
    align-items: flex-end;
    max-width: 100%;
  }
}
</style>
