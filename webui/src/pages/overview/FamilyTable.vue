<script setup lang="ts">
import { computed } from 'vue'
import Card from '@/components/ui/Card.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { channelName } from '@/stores/catalog'
import { channelProvider } from '@/utils/providers'
import { openChannel, openModel } from '@/links'
import { compact, integer, ms, usd } from '@/utils/format'
import { ov } from './data'
import { cacheShare, pctText, promptOf, successRateOf, tpsText, type Metrics } from './metrics'

defineProps<{ ready: boolean }>()

const FAMILY: Record<string, { label: string; sub: string; icon: string }> = {
  anthropic: { label: 'Anthropic', sub: 'Claude Messages 协议', icon: 'claude' },
  openai: { label: 'OpenAI', sub: 'Chat / Responses 协议', icon: 'openai' },
}

const rows = computed(() => {
  const s = ov.summary
  if (!s) return []
  const list: { key: string; label: string; sub: string; icon: string; m: Metrics; tops: { channels: { key: string; n: number }[]; models: { key: string; n: number }[] } | null }[] = [
    { key: 'all', label: '全部', sub: '今日合计', icon: '', m: s.overall, tops: null },
  ]
  for (const k of ['anthropic', 'openai']) {
    const m = s.families?.[k]
    if (!m || !m.total) continue
    const t = s.familyTops?.[k]
    list.push({
      key: k,
      ...FAMILY[k],
      m,
      tops: t ? { channels: t.channels.map((c) => ({ key: c.key, n: c.metrics.total })), models: t.models.map((c) => ({ key: c.key, n: c.metrics.total })) } : null,
    })
  }
  return list
})
</script>

<template>
  <Card title="按协议家族（今日）" flush>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th class="l">家族</th>
            <th>请求</th>
            <th>成功率</th>
            <th>读入 ↑</th>
            <th>写出 ↓</th>
            <th>缓存占比</th>
            <th>金额</th>
            <th>首字</th>
            <th>总耗时</th>
            <th>TPS</th>
            <th class="l">渠道 Top 3</th>
            <th class="l">模型 Top 3</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.key" :class="{ all: r.key === 'all' }">
            <td class="l">
              <div class="fam">
                <ProviderIcon v-if="r.icon" :provider="r.icon" :size="20" />
                <div>
                  <b>{{ r.label }}</b>
                  <span>{{ r.sub }}</span>
                </div>
              </div>
            </td>
            <td class="num">{{ integer(r.m.total) }}</td>
            <td class="num" :class="(successRateOf(r.m) ?? 1) >= 0.99 ? 'g' : 'o'">{{ pctText(successRateOf(r.m)) }}</td>
            <td class="num">{{ compact(promptOf(r.m)) }}</td>
            <td class="num">{{ compact(r.m.outputTokens) }}</td>
            <td class="num">{{ pctText(cacheShare(r.m)) }}</td>
            <td class="num strong">{{ usd(r.m.costTicks) }}</td>
            <td class="num">{{ ms(r.m.averageFirstTokenMilliseconds) }}</td>
            <td class="num">{{ ms(r.m.averageTotalMilliseconds) }}</td>
            <td class="num">{{ tpsText(r.m.averageTokensPerSecond) }}</td>
            <td class="l">
              <div v-if="r.tops" class="tops">
                <button v-for="c in r.tops.channels" :key="c.key" @click="openChannel(c.key)">
                  <ProviderIcon :provider="channelProvider(c.key)" :size="14" />
                  <span class="truncate">{{ channelName(c.key) }}</span>
                  <i class="num">{{ integer(c.n) }}</i>
                </button>
              </div>
              <span v-else class="dim">—</span>
            </td>
            <td class="l">
              <div v-if="r.tops" class="tops">
                <button v-for="c in r.tops.models" :key="c.key" @click="openModel(c.key)">
                  <span class="truncate mono">{{ c.key }}</span>
                  <i class="num">{{ integer(c.n) }}</i>
                </button>
              </div>
              <span v-else class="dim">—</span>
            </td>
          </tr>
          <tr v-if="!ready">
            <td colspan="12" class="dim l">加载中…</td>
          </tr>
        </tbody>
      </table>
    </div>
  </Card>
</template>

<style scoped>
.scroll {
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
th {
  padding: 10px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-3);
  text-align: right;
  white-space: nowrap;
  background: var(--surface-sunken);
  border-top: 1px solid var(--hairline);
  border-bottom: 1px solid var(--hairline);
}
td {
  padding: 12px;
  text-align: right;
  white-space: nowrap;
  border-bottom: 1px solid var(--hairline);
  vertical-align: middle;
}
tr:last-child td {
  border-bottom: 0;
}
.l {
  text-align: left;
}
th:first-child,
td:first-child {
  padding-left: 20px;
}
th:last-child,
td:last-child {
  padding-right: 20px;
}
tr.all td {
  background: color-mix(in srgb, var(--accent) 4%, transparent);
}
.fam {
  display: flex;
  align-items: center;
  gap: 10px;
}
.fam div {
  display: flex;
  flex-direction: column;
}
.fam b {
  font-weight: 650;
}
.fam span {
  font-size: 11.5px;
  color: var(--text-3);
}
.g {
  color: var(--text-green);
}
.o {
  color: var(--text-orange);
}
.strong {
  font-weight: 650;
}
.dim {
  color: var(--text-3);
}
.tops {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.tops button {
  display: flex;
  align-items: baseline;
  gap: 5px;
  max-width: 240px;
  font-size: 12px;
  color: var(--text-2);
  text-align: left;
}
.tops button:hover {
  color: var(--accent);
}
.tops i {
  margin-left: auto;
  padding-left: 8px;
  font-style: normal;
  color: var(--text-3);
}
</style>
