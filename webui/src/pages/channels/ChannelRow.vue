<script setup lang="ts">
import { computed } from 'vue'
import { ChevronRight, Coins, Database, GripVertical, Layers, Zap } from '@lucide/vue'
import { channelProvider } from '@/utils/providers'
import { compact, dateTime } from '@/utils/format'
import { tps } from '@/pages/oauth/channelStats'
import ProviderIcon from '@/components/ProviderIcon.vue'
import StatusBars from '@/components/ui/StatusBars.vue'
import Switch from '@/components/ui/Switch.vue'
import ProtocolTag from './ProtocolTag.vue'
import UsageBlock from './UsageBlock.vue'
import { channelStatus, cost3, toUsage, type ChannelListItem } from './meta'

const props = defineProps<{
  channel: ChannelListItem
  priority: number
  providerName?: string
  presetName?: string
  outcomes?: readonly string[]
  lastAt?: string | null
  selected?: boolean
  dragging?: boolean
  draggable?: boolean
  busy?: boolean
}>()
const emit = defineEmits<{ open: []; dragstart: [e: PointerEvent]; toggle: [on: boolean] }>()

const status = computed(() => channelStatus(props.channel))
const usage = computed(() => toUsage(props.channel.monthStats))
const provider = computed(() => channelProvider(props.channel.id, props.channel.providerId))
const cacheShare = computed(() => {
  const u = usage.value
  const r = u ? u.cacheReadRatio ?? (u.prompt ? u.cacheRead / u.prompt : null) : null
  if (r == null) return '—'
  const v = r * 100
  return `${v >= 99.95 && v < 100 ? '99.9' : v.toFixed(1)}%`
})
</script>

<template>
  <div class="crow" :class="{ selected, off: !channel.enabled, dragging }" role="button" tabindex="0" :data-drag-id="channel.id" @click="emit('open')" @keydown.enter="emit('open')">
    <button
      type="button"
      class="grip"
      :class="{ disabled: !draggable }"
      :title="draggable ? '拖动调整顺序' : '清除搜索和筛选后可以拖动排序'"
      aria-label="拖动调整顺序"
      @click.stop
      @pointerdown="draggable && emit('dragstart', $event)"
    >
      <GripVertical />
    </button>

    <div class="c-who">
      <ProviderIcon :provider="provider" :size="34" />
      <div class="who">
        <p class="name truncate">{{ channel.name }}</p>
        <p class="sub truncate">{{ providerName || '自定义地址' }}<template v-if="presetName"> · {{ presetName }}</template></p>
      </div>
    </div>

    <div class="c-proto">
      <ProtocolTag :protocol="channel.protocol" />
      <p class="sub">{{ channel.modelCount }} 个模型 · 并发 {{ channel.maxConcurrent || '默认' }}</p>
    </div>

    <div class="c-state">
      <span class="badge" :class="`b-${status.tone}`">{{ status.text }}</span>
      <p class="sub">优先级 #{{ priority }}<template v-if="channel.affinityCount + channel.clientAffinityCount"> · 亲和 {{ channel.affinityCount + channel.clientAffinityCount }}</template></p>
    </div>

    <div class="c-recent">
      <p class="num t">{{ lastAt ? dateTime(lastAt).slice(0, 11) : '近 7 天无请求' }}</p>
      <StatusBars :outcomes="outcomes" :slots="10" :width="5" :gap="3" :height="18" />
    </div>

    <div class="c-use">
      <template v-if="usage && usage.total">
        <span class="wide" title="读入（含缓存）/ 写出 Token"><Layers class="i-purple" /><b class="num">↑{{ compact(usage.prompt) }} ↓{{ compact(usage.output) }}</b></span>
        <span title="缓存读取占读入"><Database class="i-teal" /><b class="num">{{ cacheShare }}</b></span>
        <span title="平均输出速度"><Zap class="i-blue" /><b class="num">{{ tps(usage.avgTps) }}</b></span>
        <span class="wide" title="本月金额"><Coins class="i-orange" /><b class="num">{{ cost3(usage.costTicks) }}</b></span>
      </template>
      <p v-else class="sub">{{ channel.monthStatsLoading && !channel.monthStats ? '统计初始化中…' : '本月暂无调用' }}</p>
    </div>

    <div class="c-quota">
      <UsageBlock v-if="channel.providerUsage.supported" :usage="channel.providerUsage" compact />
      <p v-else class="sub">该服务商不提供额度查询</p>
    </div>

    <div class="c-act" @click.stop>
      <Switch :model-value="channel.enabled" :loading="busy" label="启用渠道" @update:model-value="emit('toggle', $event)" />
      <button class="detail" @click="emit('open')">详情<ChevronRight /></button>
    </div>
  </div>
</template>

<style scoped>
.crow {
  display: grid;
  grid-template-columns: var(--cols);
  align-items: center;
  column-gap: 14px;
  padding: 12px 16px 12px 8px;
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg);
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
}
.crow:hover {
  box-shadow: var(--shadow-md);
}
.crow.selected {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 4%, var(--surface));
}
.crow.dragging {
  opacity: 0.55;
}
.crow.off .c-who,
.crow.off .c-use {
  opacity: 0.6;
}
.crow > * {
  min-width: 0;
}
.grip {
  display: grid;
  place-items: center;
  color: var(--text-3);
  cursor: grab;
  touch-action: none;
}
.grip.disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.grip svg {
  width: 15px;
  height: 15px;
}
.c-who {
  display: flex;
  align-items: center;
  gap: 10px;
}
.who {
  min-width: 0;
}
.name {
  font-size: 13.5px;
  font-weight: 650;
}
.sub {
  font-size: 11.5px;
  color: var(--text-3);
}
.c-proto,
.c-state {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}
.badge {
  max-width: 100%;
  padding: 2px 9px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  border: 1px solid transparent;
}
.b-green {
  color: var(--text-green);
  background: color-mix(in srgb, var(--green) 10%, transparent);
  border-color: color-mix(in srgb, var(--green) 28%, transparent);
}
.b-yellow,
.b-orange {
  color: var(--text-orange);
  background: color-mix(in srgb, var(--orange) 10%, transparent);
  border-color: color-mix(in srgb, var(--orange) 30%, transparent);
}
.b-red {
  color: var(--text-red);
  background: color-mix(in srgb, var(--red) 9%, transparent);
  border-color: color-mix(in srgb, var(--red) 26%, transparent);
}
.b-neutral,
.b-blue {
  color: var(--text-2);
  background: var(--fill-2);
}
.c-recent {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.c-recent .t {
  font-size: 12.5px;
  color: var(--text-2);
}
.c-use {
  display: grid;
  grid-template-columns: auto auto;
  justify-content: start;
  gap: 5px 14px;
  font-size: 12.5px;
}
.c-use .wide {
  grid-column: 1 / -1;
}
.c-use > span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
}
.c-use svg {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
}
.c-use b {
  font-weight: 650;
  white-space: nowrap;
}
.i-blue { color: var(--blue); }
.i-purple { color: var(--purple); }
.i-orange { color: var(--orange); }
.i-teal { color: var(--teal); }
.c-quota {
  font-size: 12px;
  overflow: hidden;
}
.c-act {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  cursor: default;
}
.detail {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 30px;
  padding: 0 8px 0 10px;
  border-radius: 8px;
  font-size: 12.5px;
  font-weight: 600;
  white-space: nowrap;
  color: var(--accent);
  background: var(--accent-soft);
}
.detail svg {
  width: 14px;
  height: 14px;
}
</style>
