<script setup lang="ts">
import { computed } from 'vue'
import { integer } from '@/utils/format'
import { cacheText, cost3, tokens, tps, type Usage } from './meta'

// Monthly usage lines with the same content as the Telegram channel / API key pages:
// requests · success rate · failures, ↑ prompt · ↓ output · cache, TPS avg / peak / min, cost.
const props = withDefaults(defineProps<{ usage: Usage | null; dense?: boolean; showTps?: boolean }>(), { showTps: true })

const rate = computed(() => (props.usage?.successRate == null ? null : props.usage.successRate * 100))
</script>

<template>
  <dl class="sl" :class="{ dense }">
    <template v-if="usage && usage.total">
      <div class="r">
        <dt>请求</dt>
        <dd>
          <b class="num">{{ integer(usage.total) }}</b> 次
          <span class="sep">·</span>
          <span v-if="rate !== null" :class="rate < 80 ? (rate < 50 ? 'c-red' : 'c-orange') : 'c-green'">成功率 {{ rate.toFixed(1) }}%</span>
          <template v-if="usage.error"><span class="sep">·</span><span class="c-red">失败 {{ integer(usage.error) }}</span></template>
        </dd>
      </div>
      <div class="r">
        <dt>Token</dt>
        <dd class="num">
          ↑ {{ tokens(usage.prompt) }} <span class="sep">·</span> ↓ {{ tokens(usage.output) }}
          <template v-if="usage.cacheRead"><span class="sep">·</span><span class="muted">缓存 {{ cacheText(usage.cacheRead, usage.cacheReadRatio, usage.prompt) }}</span></template>
        </dd>
      </div>
      <div v-if="showTps && usage.avgTps !== null" class="r">
        <dt>速度</dt>
        <dd class="num">
          平均 {{ tps(usage.avgTps) }}<template v-if="usage.maxTps !== null"><span class="sep">·</span>峰值 {{ tps(usage.maxTps) }}</template
          ><template v-if="usage.minTps !== null"><span class="sep">·</span>最低 {{ tps(usage.minTps) }}</template>
        </dd>
      </div>
      <div class="r">
        <dt>费用</dt>
        <dd class="num"><b>{{ cost3(usage.costTicks) }}</b></dd>
      </div>
    </template>
    <div v-else class="r">
      <dt>本月</dt>
      <dd class="faint"><slot name="empty">暂无调用</slot></dd>
    </div>
  </dl>
</template>

<style scoped>
.sl {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  font-size: 12.5px;
  min-width: 0;
}
.sl.dense {
  font-size: 12px;
  gap: 2px;
}
.r {
  display: grid;
  grid-template-columns: 40px minmax(0, 1fr);
  gap: 8px;
  align-items: baseline;
}
dt {
  font-size: 11px;
  color: var(--text-3);
}
dd {
  margin: 0;
  min-width: 0;
  overflow-wrap: anywhere;
}
.sep {
  margin: 0 4px;
  color: var(--text-3);
}
b {
  font-weight: 600;
}
</style>
