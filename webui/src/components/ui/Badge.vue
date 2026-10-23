<script setup lang="ts">
withDefaults(
  defineProps<{
    tone?: 'neutral' | 'blue' | 'green' | 'orange' | 'red' | 'purple' | 'teal' | 'yellow' | 'pink'
    mono?: boolean
    dot?: boolean
  }>(),
  { tone: 'neutral' },
)
</script>

<template>
  <span class="badge" :class="[`t-${tone}`, { mono }]">
    <i v-if="dot" class="dot" />
    <slot />
  </span>
</template>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 18px;
  padding: 0 6px;
  border-radius: 5px;
  font-size: 11px;
  font-weight: 500;
  line-height: 1;
  white-space: nowrap;
  color: var(--tone);
  background: color-mix(in srgb, var(--tone) 14%, transparent);
}
.mono {
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
}
.dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}
.t-neutral {
  --tone: var(--text-2);
  background: var(--fill-2);
}
.t-blue { --tone: var(--blue); }
.t-green { --tone: var(--green); }
.t-orange { --tone: var(--orange); }
.t-red { --tone: var(--red); }
.t-purple { --tone: var(--purple); }
.t-teal { --tone: var(--teal); }
.t-yellow { --tone: var(--yellow); }
.t-pink { --tone: var(--pink); }
</style>
