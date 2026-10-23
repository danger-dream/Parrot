<script setup lang="ts">
import { computed } from 'vue'
import { CircleCheck, CircleX, TriangleAlert } from '@lucide/vue'
import type { TestPlan } from './types'

const props = defineProps<{ plan: TestPlan }>()

const rows = computed(() =>
  (props.plan.result.results || []).map((r) => {
    const dns = props.plan.kind === 'dns'
    const ok = dns ? Boolean(r.resolve) : Boolean(r.ok)
    return {
      name: r.label || r.host || r.url || '检测项',
      sub: dns ? r.host : r.host || '',
      ok,
      httpOk: dns && r.resolve ? Boolean(r.http) : null,
      ips: r.ips || [],
      error: r.error || '',
    }
  }),
)
</script>

<template>
  <div class="tr-box" :class="plan.passed ? 'pass' : 'fail'">
    <p class="head">
      <CircleCheck v-if="plan.passed" />
      <TriangleAlert v-else />
      {{
        plan.passed
          ? plan.kind === 'dns'
            ? '检测通过：所有测试域名都能正常解析。'
            : '检测通过：经 SOCKS5 访问测试站点正常。'
          : plan.kind === 'dns'
            ? '部分域名解析失败，保存后相关功能可能异常。'
            : '部分站点经 SOCKS5 访问失败，保存后相关功能可能异常。'
      }}
    </p>
    <ul>
      <li v-for="(r, i) in rows" :key="i">
        <CircleCheck v-if="r.ok" class="ok" />
        <CircleX v-else class="bad" />
        <span class="nm">
          <b>{{ r.name }}</b>
          <small v-if="r.sub" class="mono">{{ r.sub }}</small>
        </span>
        <span class="meta">
          <template v-if="r.ok">
            <span v-if="r.httpOk !== null" :class="r.httpOk ? 'c-green' : 'c-orange'">{{ r.httpOk ? '可访问' : '解析成功，访问失败' }}</span>
            <span v-if="r.ips.length" class="mono ips">{{ r.ips.join(', ') }}</span>
            <span v-if="r.error" class="err">{{ r.error }}</span>
          </template>
          <span v-else class="err">{{ r.error || '失败' }}</span>
        </span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.tr-box {
  border-radius: var(--radius-md);
  padding: 10px 12px;
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.tr-box.pass {
  background: color-mix(in srgb, var(--green) 7%, transparent);
}
.tr-box.fail {
  background: color-mix(in srgb, var(--orange) 8%, transparent);
}
.head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 500;
}
.head svg {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
}
.pass .head svg {
  color: var(--green);
}
.fail .head svg {
  color: var(--orange);
}
ul {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
}
li {
  display: grid;
  grid-template-columns: 16px minmax(120px, 0.8fr) minmax(0, 2fr);
  align-items: start;
  gap: 8px;
  padding: 6px 0;
  border-top: 0.5px solid var(--hairline);
  font-size: 12.5px;
}
li > svg {
  width: 14px;
  height: 14px;
  margin-top: 2px;
}
.ok {
  color: var(--green);
}
.bad {
  color: var(--red);
}
.nm {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.nm b {
  font-weight: 500;
}
.nm small {
  font-size: 11px;
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 10px;
  min-width: 0;
  font-size: 12px;
  color: var(--text-2);
}
.ips {
  font-size: 11.5px;
  color: var(--text-3);
  overflow-wrap: anywhere;
}
.err {
  color: var(--red);
  overflow-wrap: anywhere;
}
@container (max-width: 560px) {
  li {
    grid-template-columns: 16px minmax(0, 1fr);
  }
  .meta {
    grid-column: 2;
  }
}
</style>
