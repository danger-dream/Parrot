<script setup lang="ts">
import { computed } from 'vue'
import { GripVertical } from '@lucide/vue'
import { integer } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import StatusDot from '@/components/ui/StatusDot.vue'
import StatsLines from '@/pages/channels/StatsLines.vue'
import { toUsage } from '@/pages/channels/meta'
import KeySecret from './KeySecret.vue'
import { keySecret, limitBrief, type ApiKey } from './meta'

const props = defineProps<{
  k: ApiKey
  live?: { inFlight: number; waiting: number } | null
  history?: number | null
  selected?: boolean
  dragging?: boolean
  draggable?: boolean
}>()
const emit = defineEmits<{ open: []; dragstart: [e: PointerEvent] }>()

const inFlight = computed(() => props.live?.inFlight ?? props.k.limiter.inFlight)
const waiting = computed(() => props.live?.waiting ?? props.k.limiter.waiting)
const status = computed(() => {
  if (!props.k.enabled) return { text: '已停用', tone: 'red' as const }
  if (inFlight.value || waiting.value) return { text: '使用中', tone: 'blue' as const }
  if (props.k.monthStats.total) return { text: '活跃', tone: 'green' as const }
  return { text: '闲置', tone: 'gray' as const }
})
const perms = computed(() => {
  const out: { text: string; tone: 'neutral' | 'blue' | 'purple' | 'teal' | 'orange' }[] = [
    { text: props.k.allowedModels.length ? `${props.k.allowedModels.length} 个模型` : '全部模型', tone: props.k.allowedModels.length ? 'blue' : 'neutral' },
  ]
  if ((props.k.allowedChannels || []).length) out.push({ text: props.k.channelBindingEnabled ? `绑定 ${props.k.allowedChannels!.length} 个渠道` : '渠道绑定已关闭', tone: props.k.channelBindingEnabled ? 'orange' : 'neutral' })
  if (props.k.allowImages) out.push({ text: '图片', tone: 'purple' })
  if (props.k.allowVideos) out.push({ text: '视频', tone: 'purple' })
  if (props.k.allowMcp) out.push({ text: 'MCP', tone: 'teal' })
  return out
})
const usage = computed(() => toUsage(props.k.monthStats))
</script>

<template>
  <div
    class="kc"
    :class="{ selected, off: !k.enabled, dragging }"
    role="button"
    tabindex="0"
    :data-drag-id="k.keyId"
    @click="emit('open')"
    @keydown.enter="emit('open')"
  >
    <div class="head">
      <StatusDot :tone="status.tone" :pulse="status.tone === 'blue'" />
      <span class="name mono truncate">{{ k.name }}</span>
      <span class="st" :class="`t-${status.tone}`">{{ status.text }}</span>
      <button
        type="button"
        class="handle"
        :class="{ disabled: !draggable }"
        :title="draggable ? '拖动调整顺序' : '清除搜索和筛选后可以拖动排序'"
        aria-label="拖动调整顺序"
        @click.stop
        @pointerdown="draggable && emit('dragstart', $event)"
      >
        <GripVertical />
      </button>
    </div>

    <KeySecret :secret="keySecret(k)" :masked="k.maskedHint" />

    <div class="perms">
      <Badge v-for="p in perms" :key="p.text" :tone="p.tone">{{ p.text }}</Badge>
    </div>

    <p class="limit">
      <span class="lbl">限流</span>
      <span class="num" :class="{ 'c-orange': waiting }">{{ limitBrief(k.limiter, live) }}</span>
    </p>

    <section class="block">
      <StatsLines :usage="usage" dense>
        <template #empty>
          闲置<template v-if="history != null">（历史 {{ integer(history) }} 次）</template>
        </template>
      </StatsLines>
    </section>
  </div>
</template>

<style scoped>
.kc {
  display: flex;
  flex-direction: column;
  gap: 9px;
  height: 100%;
  min-width: 0;
  padding: 14px 14px 14px 16px;
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--card-highlight), inset 0 0 0 0.5px var(--border);
  cursor: pointer;
  transition: background 0.15s, box-shadow 0.15s, opacity 0.15s, transform 0.15s;
}
.kc:hover {
  background: var(--surface-hover);
}
.kc.selected {
  box-shadow: var(--card-highlight), inset 0 0 0 1.5px var(--accent);
}
.kc.dragging {
  opacity: 0.75;
  transform: scale(0.985);
  box-shadow: var(--shadow-md), inset 0 0 0 1.5px var(--accent);
}
.kc.off .name {
  color: var(--text-2);
}
.head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.name {
  flex: 1;
  font-size: 14.5px;
  font-weight: 650;
}
.st {
  font-size: 12px;
  font-weight: 600;
}
.t-green { color: var(--green); }
.t-blue { color: var(--blue); }
.t-red { color: var(--red); }
.t-gray { color: var(--text-3); }
.handle {
  display: grid;
  place-items: center;
  width: 30px;
  height: 32px;
  margin: -4px -6px -4px 0;
  border-radius: 7px;
  color: var(--text-3);
  cursor: grab;
  touch-action: none;
  flex-shrink: 0;
}
.handle:hover:not(.disabled) {
  background: var(--fill-2);
  color: var(--text);
}
.handle.disabled {
  cursor: not-allowed;
  opacity: 0.35;
}
.handle svg {
  width: 16px;
  height: 16px;
}
.perms {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.limit {
  display: flex;
  gap: 8px;
  font-size: 12px;
  min-width: 0;
}
.lbl {
  width: 40px;
  flex-shrink: 0;
  font-size: 11px;
  color: var(--text-3);
}
.block {
  margin-top: auto;
  padding-top: 9px;
  border-top: 0.5px solid var(--hairline);
}
</style>
