<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { CircleCheck, CircleX, LoaderCircle } from '@lucide/vue'
import type { Operation } from '@/api/operations'
import { duration } from '@/utils/format'
import { operationFailureText } from '@/composables/useOperation'

// Visible progress for a long-running Operation (HTTP 202).
const props = defineProps<{
  op: Operation | null
  /** Title while running, e.g. “正在测试 DNS”. */
  title: string
  /** Chinese labels for progress.messageCode values. */
  steps?: Record<string, string>
  doneText?: string
}>()

const now = ref(Date.now())
const timer = window.setInterval(() => (now.value = Date.now()), 1000)
onBeforeUnmount(() => window.clearInterval(timer))

const active = computed(() => props.op && (props.op.status === 'queued' || props.op.status === 'running'))
const pct = computed(() => {
  const p = props.op?.progress
  if (!p || !p.total) return null
  return Math.max(4, Math.min(100, (p.current / p.total) * 100))
})
const stepText = computed(() => {
  const p = props.op?.progress
  if (!p) return props.op?.status === 'queued' ? '排队中…' : '处理中…'
  const label = props.steps?.[p.messageCode] || '处理中…'
  return p.total > 1 ? `${label}（${p.current}/${p.total}）` : label
})
const elapsed = computed(() => {
  const start = props.op?.startedAt || props.op?.createdAt
  if (!start) return ''
  const end = props.op?.finishedAt ? new Date(props.op.finishedAt).getTime() : now.value
  return duration((end - new Date(start).getTime()) / 1000)
})
</script>

<template>
  <div v-if="op" class="opp" :class="op.status">
    <div class="row">
      <LoaderCircle v-if="active" class="ic spin" />
      <CircleCheck v-else-if="op.status === 'succeeded'" class="ic ok" />
      <CircleX v-else class="ic bad" />
      <span class="t">
        <template v-if="active">{{ title }} · {{ stepText }}</template>
        <template v-else-if="op.status === 'succeeded'">{{ doneText || '已完成' }}</template>
        <template v-else>{{ operationFailureText(op) }}</template>
      </span>
      <span class="el mono">{{ elapsed }}</span>
    </div>
    <div v-if="active" class="bar" :class="{ indet: pct === null }">
      <span :style="pct !== null ? { width: `${pct}%` } : undefined" />
    </div>
  </div>
</template>

<style scoped>
.opp {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.opp.succeeded {
  background: color-mix(in srgb, var(--green) 8%, transparent);
}
.opp.failed,
.opp.cancelled {
  background: color-mix(in srgb, var(--red) 8%, transparent);
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.ic {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
  color: var(--accent);
}
.ic.ok {
  color: var(--green);
}
.ic.bad {
  color: var(--red);
}
.spin {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
.t {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
}
.el {
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
}
.bar {
  position: relative;
  height: 4px;
  border-radius: 999px;
  background: var(--fill-2);
  overflow: hidden;
}
.bar span {
  position: absolute;
  inset: 0 auto 0 0;
  border-radius: inherit;
  background: var(--accent);
  transition: width 0.4s var(--ease-out);
}
.bar.indet span {
  width: 35%;
  animation: indet 1.2s ease-in-out infinite;
}
@keyframes indet {
  0% {
    left: -35%;
  }
  100% {
    left: 100%;
  }
}
</style>
