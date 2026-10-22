<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight, Repeat } from '@lucide/vue'
import { channelName } from '@/stores/catalog'
import { clock, compact, ms, usd } from '@/utils/format'
import { statusLabel, statusTone } from './status'

const props = defineProps<{
  id: string
  createdAt?: string | null
  status: string
  apiKeyName?: string | null
  requestedModel?: string | null
  finalModel?: string | null
  channelId?: string | null
  durationMilliseconds?: number | null
  inputTokens: number
  outputTokens: number
  costTicks: number
  retryCount: number
  error?: string | null
  protocol?: string | null
  transport?: string | null
  isStream?: boolean
  billing?: unknown
  revision?: string
  log?: unknown
  receivedAt?: number
  now?: number
  fresh?: boolean
  selected?: boolean
}>()

const tone = computed(() => statusTone(props.status))
const pending = computed(() => props.status === 'pending')
const elapsed = computed(() => {
  if (!pending.value || !props.createdAt || !props.now) return null
  return Math.max(0, props.now - new Date(props.createdAt).getTime())
})
const modelChanged = computed(() => props.finalModel && props.finalModel !== props.requestedModel)
</script>

<template>
  <button class="row" :class="[`t-${tone}`, { fresh, selected, pending }]" type="button">
    <span class="c-time mono">{{ clock(createdAt) }}</span>
    <span class="c-status">
      <i class="dot" />
      <span class="st-label">{{ statusLabel(status) }}</span>
    </span>
    <span class="c-key truncate">{{ apiKeyName || '—' }}</span>
    <span class="c-model">
      <span class="mono truncate">{{ requestedModel || '—' }}</span>
      <template v-if="modelChanged">
        <ArrowRight class="arrow" />
        <span class="mono truncate final">{{ finalModel }}</span>
      </template>
      <span v-if="retryCount" class="retry" :title="`重试 ${retryCount} 次`"><Repeat />{{ retryCount }}</span>
    </span>
    <span class="c-channel truncate">{{ channelId ? channelName(channelId) : pending ? '分配中…' : '—' }}</span>
    <span class="c-dur mono">{{ pending ? ms(elapsed) : ms(durationMilliseconds) }}</span>
    <span class="c-tok mono">
      <template v-if="inputTokens || outputTokens">
        <span class="faint">↑</span>{{ compact(inputTokens) }} <span class="faint">↓</span>{{ compact(outputTokens) }}
      </template>
      <template v-else>—</template>
    </span>
    <span class="c-cost mono">{{ costTicks ? usd(costTicks) : '—' }}</span>
  </button>
</template>

<style scoped>
.row {
  display: grid;
  grid-template-columns: var(--cols);
  grid-template-areas: var(--areas);
  align-items: center;
  column-gap: 12px;
  width: 100%;
  min-height: 38px;
  padding: 0 14px;
  text-align: left;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
  transition: background 0.15s;
}
.row:hover {
  background: var(--fill-3);
}
.row.selected {
  background: var(--accent-soft);
}
.row.fresh {
  animation: flash 1.6s var(--ease-out);
}
@keyframes flash {
  0% {
    background: color-mix(in srgb, var(--tone) 18%, transparent);
  }
  100% {
    background: transparent;
  }
}
.t-green { --tone: var(--green); }
.t-red { --tone: var(--red); }
.t-orange { --tone: var(--orange); }
.t-blue { --tone: var(--blue); }
.t-neutral { --tone: var(--text-3); }
.c-time { grid-area: time; color: var(--text-2); font-size: 11.5px; }
.c-status { grid-area: status; display: flex; align-items: center; gap: 6px; }
.c-key { grid-area: key; color: var(--text-2); }
.c-model { grid-area: model; display: flex; align-items: center; gap: 5px; min-width: 0; }
.c-channel { grid-area: channel; color: var(--text-2); }
.c-dur { grid-area: dur; text-align: right; font-size: 11.5px; }
.c-tok { grid-area: tok; text-align: right; font-size: 11.5px; white-space: nowrap; }
.c-cost { grid-area: cost; text-align: right; font-size: 11.5px; color: var(--text-2); }
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--tone);
  flex-shrink: 0;
}
.pending .dot {
  animation: blink 1s ease-in-out infinite;
}
@keyframes blink {
  50% {
    opacity: 0.3;
  }
}
.st-label {
  font-size: 12px;
  color: var(--tone);
}
.arrow {
  width: 12px;
  height: 12px;
  color: var(--text-3);
  flex-shrink: 0;
}
.final {
  color: var(--accent);
}
.retry {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 0 4px;
  height: 16px;
  border-radius: 4px;
  font-size: 10.5px;
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 14%, transparent);
  flex-shrink: 0;
}
.retry svg {
  width: 10px;
  height: 10px;
}
.pending .c-dur {
  color: var(--blue);
}
</style>
