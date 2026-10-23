<script setup lang="ts">
import { CircleCheck, CircleX } from '@lucide/vue'
import { bytes, targetLabel, type ProbeItem } from './meta'

defineProps<{ items: ProbeItem[] }>()
</script>

<template>
  <ul class="pr">
    <li v-for="(r, i) in items" :key="i" :class="r.ok ? 'ok' : 'fail'">
      <component :is="r.ok ? CircleCheck : CircleX" class="ic" />
      <div class="body">
        <p class="line">
          <b v-if="r.name" class="mono">{{ targetLabel(r.name) }}</b>
          <span v-if="r.ok">出口 IP <span class="mono">{{ r.ip || '未知' }}</span> · 延迟 <span class="mono">{{ r.latencyMilliseconds ?? '—' }}ms</span></span>
          <span v-else>连接失败</span>
        </p>
        <p v-if="r.ok && r.traffic.totalBytes" class="sub">测试流量 {{ bytes(r.traffic.totalBytes) }}</p>
        <p v-if="!r.ok && r.error" class="sub err">{{ r.error.message === 'Proxy probe failed' ? '代理连接测试失败，请核对地址、密码和网络。' : r.error.message }}</p>
      </div>
    </li>
  </ul>
</template>

<style scoped>
.pr {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
li {
  display: flex;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 12.5px;
}
li.ok {
  background: color-mix(in srgb, var(--green) 10%, transparent);
}
li.fail {
  background: color-mix(in srgb, var(--red) 10%, transparent);
}
.ic {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  margin-top: 2px;
}
.ok .ic {
  color: var(--green);
}
.fail .ic {
  color: var(--red);
}
.body {
  min-width: 0;
}
.line {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
}
.sub {
  font-size: 11.5px;
  color: var(--text-3);
}
.sub.err {
  color: var(--red);
  word-break: break-word;
}
</style>
