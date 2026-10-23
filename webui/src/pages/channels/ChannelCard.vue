<script setup lang="ts">
import { computed } from 'vue'
import { GripVertical } from '@lucide/vue'
import { channelProvider } from '@/utils/providers'
import StatusDot from '@/components/ui/StatusDot.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import StatsLines from './StatsLines.vue'
import UsageBlock from './UsageBlock.vue'
import { channelStatus, dotTone, toUsage, type ChannelListItem } from './meta'
import ProtocolTag from './ProtocolTag.vue'

const props = defineProps<{
  channel: ChannelListItem
  providerName?: string
  presetName?: string
  selected?: boolean
  dragging?: boolean
  draggable?: boolean
}>()
const emit = defineEmits<{ open: []; dragstart: [e: PointerEvent] }>()

const status = computed(() => channelStatus(props.channel))
const usage = computed(() => toUsage(props.channel.monthStats))
const provider = computed(() => channelProvider(props.channel.id, props.channel.providerId))
</script>

<template>
  <div
    class="cc"
    :class="{ selected, off: !channel.enabled, dragging }"
    role="button"
    tabindex="0"
    :data-drag-id="channel.id"
    @click="emit('open')"
    @keydown.enter="emit('open')"
  >
    <div class="head">
      <ProviderIcon :provider="provider" :size="30" />
      <div class="titles">
        <p class="name truncate">{{ channel.name }}</p>
        <p class="sub truncate">
          {{ providerName || '自定义地址' }}<template v-if="presetName"> · {{ presetName }}</template>
        </p>
      </div>
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

    <div class="state">
      <StatusDot :tone="dotTone(status.tone)" />
      <span class="st" :class="`t-${status.tone}`">{{ status.text }}</span>
      <span class="grow" />
      <ProtocolTag :protocol="channel.protocol" />
    </div>

    <div class="facts">
      <span><b class="num">{{ channel.modelCount }}</b> 个模型</span>
      <span>并发上限 <b class="num">{{ channel.maxConcurrent || '默认' }}</b></span>
      <span v-if="channel.affinityCount + channel.clientAffinityCount">亲和 <b class="num">{{ channel.affinityCount + channel.clientAffinityCount }}</b></span>
    </div>

    <section v-if="channel.providerUsage.supported" class="block">
      <p class="bt">上游额度</p>
      <UsageBlock :usage="channel.providerUsage" compact />
    </section>

    <section class="block">
      <p class="bt">本月 Parrot 统计</p>
      <StatsLines :usage="usage" dense>
        <template #empty>{{ channel.monthStatsLoading && !channel.monthStats ? '统计初始化中…' : '暂无调用' }}</template>
      </StatsLines>
    </section>
  </div>
</template>

<style scoped>
.cc {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-width: 0;
  padding: 14px 14px 14px 16px;
  text-align: left;
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--card-highlight), inset 0 0 0 0.5px var(--border);
  cursor: pointer;
  transition: background 0.15s, box-shadow 0.15s, opacity 0.15s, transform 0.15s;
}
.cc:hover {
  background: var(--surface-hover);
}
.cc.selected {
  box-shadow: var(--card-highlight), inset 0 0 0 1.5px var(--accent);
}
.cc.dragging {
  opacity: 0.75;
  transform: scale(0.985);
  box-shadow: var(--shadow-md), inset 0 0 0 1.5px var(--accent);
}
.cc.off .name {
  color: var(--text-2);
}
.head {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.titles {
  flex: 1;
  min-width: 0;
}
.name {
  font-size: 14.5px;
  font-weight: 650;
}
.sub {
  font-size: 11.5px;
  color: var(--text-3);
}
.handle {
  display: grid;
  place-items: center;
  width: 30px;
  height: 32px;
  margin-right: -4px;
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
.dragging .handle {
  cursor: grabbing;
}
.handle svg {
  width: 16px;
  height: 16px;
}
.state {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
.st {
  font-size: 12.5px;
  font-weight: 600;
}
.t-green { color: var(--green); }
.t-yellow { color: var(--yellow); }
.t-orange { color: var(--orange); }
.t-red { color: var(--red); }
.t-neutral { color: var(--text-2); }
.grow {
  flex: 1;
}
.facts {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  font-size: 12px;
  color: var(--text-2);
}
.facts b {
  color: var(--text);
  font-weight: 600;
}
.block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 10px;
  border-top: 0.5px solid var(--hairline);
}
.block:last-child {
  margin-top: auto;
}
.bt {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
}
</style>
