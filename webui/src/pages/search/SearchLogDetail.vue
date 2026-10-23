<script setup lang="ts">
import { computed } from 'vue'
import type { Schemas } from '@/api/client'
import { compact, dateTime, integer, ms, TICKS_PER_USD, usdOne } from '@/utils/format'
import { openAccount, openModel, openRequest } from '@/links'
import { openInspector } from '@/stores/ui'
import Badge from '@/components/ui/Badge.vue'
import CopyText from '@/components/ui/CopyText.vue'
import DetailSection from '@/components/DetailSection.vue'
import BackendDetail from './BackendDetail.vue'
import { COST_SOURCE, CREDENTIAL_KIND, errorLabel, OPERATION_LABEL, ORIGIN_LABEL, STATUS_LABEL } from './labels'

const props = defineProps<{ log: Schemas['SearchCallLogData'] }>()
const l = computed(() => props.log)
const tone = computed(() => (l.value.status === 'success' ? 'green' : l.value.status === 'error' ? 'red' : 'blue'))

/** MCP calls carry the MCP call id; other origins carry the parent request id. */
const requestLink = computed(() => (l.value.requestId && l.value.origin === 'managed_round' ? l.value.requestId : ''))

function openSource(): void {
  openInspector(`search:${l.value.sourceId}`, l.value.sourceName, BackendDetail, { id: l.value.sourceId }, '搜索来源')
}
</script>

<template>
  <div class="d">
    <div class="badges">
      <Badge :tone="tone" dot>{{ STATUS_LABEL[l.status] || l.status }}</Badge>
      <Badge>{{ OPERATION_LABEL[l.operation] || l.operation }}</Badge>
      <Badge tone="purple">{{ ORIGIN_LABEL[l.origin] || l.origin }}</Badge>
      <Badge v-if="l.attemptNo > 1" tone="orange">第 {{ l.attemptNo }} 次尝试</Badge>
    </div>

    <DetailSection :title="l.operation === 'extract' ? '提取地址' : '搜索词'">
      <p class="subject" :class="{ mono: l.operation === 'extract' }">{{ l.query || l.url || '—' }}</p>
    </DetailSection>

    <p v-if="l.errorCode" class="error-box">{{ errorLabel(l.errorCode) }}<span class="mono faint"> · {{ l.errorCode }}</span></p>

    <dl class="kv">
      <div><dt>来源</dt><dd><button type="button" class="lk" @click="openSource">{{ l.sourceName }}</button></dd></div>
      <div><dt>凭据</dt><dd>{{ CREDENTIAL_KIND[l.credentialKind] || '—' }}<template v-if="l.credentialIndex !== null && l.credentialIndex !== undefined"> #{{ l.credentialIndex + 1 }}</template></dd></div>
      <div v-if="l.accountKey" class="full">
        <dt>账户</dt>
        <dd><button type="button" class="lk mono" @click="openAccount(l.accountKey)">{{ l.credentialLabel || l.accountKey }}</button></dd>
      </div>
      <div v-if="l.model"><dt>模型</dt><dd><button type="button" class="lk mono" @click="openModel(l.model)">{{ l.model }}</button></dd></div>
      <div><dt>耗时</dt><dd class="mono">{{ ms(l.elapsedMs) }}</dd></div>
      <div><dt>结果</dt><dd class="mono">{{ integer(l.resultCount) }} 条</dd></div>
      <div><dt>正文</dt><dd class="mono">{{ integer(l.contentChars) }} 字符</dd></div>
      <div><dt>开始</dt><dd>{{ dateTime(l.startedAt) }}</dd></div>
      <div><dt>结束</dt><dd>{{ dateTime(l.endedAt) }}</dd></div>
      <div><dt>轮次</dt><dd class="mono">{{ l.roundNo }}</dd></div>
    </dl>

    <DetailSection title="用量与金额">
      <dl class="kv">
        <div><dt>输入 / 输出</dt><dd class="mono">{{ compact(l.inputTokens) }} / {{ compact(l.outputTokens) }}</dd></div>
        <div><dt>缓存读 / 写</dt><dd class="mono">{{ compact(l.cacheReadTokens) }} / {{ compact(l.cacheCreationTokens) }}</dd></div>
        <div><dt>金额</dt><dd class="mono">{{ usdOne((l.costUsd ?? 0) * TICKS_PER_USD) }}</dd></div>
        <div><dt>计价方式</dt><dd>{{ COST_SOURCE[l.costSource] || l.costSource }}</dd></div>
        <div v-if="l.pricingModel" class="full"><dt>计价模型</dt><dd class="mono">{{ l.pricingModel }}</dd></div>
      </dl>
      <p v-if="!l.usageObserved" class="hint">上游没有返回用量；API Key 来源通常按次计费，不计入 Token。</p>
    </DetailSection>

    <DetailSection title="标识">
      <dl class="kv">
        <div class="full">
          <dt>{{ l.origin === 'mcp' ? 'MCP 调用 ID' : '关联请求' }}</dt>
          <dd>
            <button v-if="requestLink" type="button" class="lk mono" @click="openRequest(requestLink)">{{ requestLink }}</button>
            <CopyText v-else-if="l.requestId" :text="l.requestId" />
            <span v-else>—</span>
          </dd>
        </div>
        <div class="full"><dt>调用 ID</dt><dd><CopyText :text="l.callId" /></dd></div>
      </dl>
    </DetailSection>
  </div>
</template>

<style scoped>
.d {
  padding: 16px 18px 28px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.subject {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  font-size: 12.5px;
  line-height: 1.55;
  word-break: break-all;
}
.error-box {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  color: var(--red);
  background: color-mix(in srgb, var(--red) 10%, transparent);
  font-size: 12.5px;
}
.kv {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 16px;
}
.kv .full {
  grid-column: 1 / -1;
}
dt {
  font-size: 11px;
  color: var(--text-3);
}
dd {
  margin: 2px 0 0;
  font-size: 12.5px;
  min-width: 0;
  word-break: break-all;
}
.lk {
  color: var(--accent);
  text-align: left;
  word-break: break-all;
}
.lk:hover {
  text-decoration: underline;
}
.hint {
  font-size: 11.5px;
  color: var(--text-3);
}
</style>
