<script setup lang="ts">
withDefaults(
  defineProps<{
    tone?: 'green' | 'orange' | 'red' | 'blue' | 'gray'
    pulse?: boolean
    size?: number
  }>(),
  { tone: 'green', size: 7 },
)
</script>

<template>
  <span class="sd" :class="[`t-${tone}`, { pulse }]" :style="{ width: `${size}px`, height: `${size}px` }" />
</template>

<style scoped>
.sd {
  position: relative;
  display: inline-block;
  flex-shrink: 0;
  border-radius: 50%;
  background: var(--tone);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--tone) 18%, transparent);
}
.pulse::after {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 50%;
  background: var(--tone);
  animation: pulse-ring 1.8s var(--ease-out) infinite;
}
.t-green { --tone: var(--green); }
.t-orange { --tone: var(--orange); }
.t-red { --tone: var(--red); }
.t-blue { --tone: var(--blue); }
.t-gray { --tone: var(--text-3); }
</style>
