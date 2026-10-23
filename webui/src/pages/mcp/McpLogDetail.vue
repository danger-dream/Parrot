<script setup lang="ts">
import { computed } from 'vue'
import type { Schemas } from '@/api/client'
import { dateTime, integer, ms } from '@/utils/format'
import { goPage, openAccount, openApiKey, openModel } from '@/links'
import Badge from '@/components/ui/Badge.vue'
import CopyText from '@/components/ui/CopyText.vue'
import DetailSection from '@/components/DetailSection.vue'
import McpResult from './McpResult.vue'
import { bytes, errorLabel, STATUS_LABEL, STATUS_TONE, subjectOf, toolIcon, toolLabel } from './labels'

const props = defineProps<{ log: Schemas['McpCallLogData'] }>()
const l = computed(() => props.log)
const subject = computed(() => subjectOf(l.value))
const params = computed(() => {
  const p = l.value.params
  if (p === null || p === undefined) return ''
  if (typeof p === 'object' && !Array.isArray(p) && !Object.keys(p).length) return ''
  try {
    return JSON.stringify(p, null, 2)
  } catch {
    return String(p)
  }
})
const isSearch = computed(() => l.value.toolName === 'web_search' || l.value.toolName === 'web_fetch')
</script>

<template>
  <div class="d">
    <div class="hero">
      <span class="ic"><component :is="toolIcon(l.toolName)" /></span>
      <div class="t">
        <p class="name">{{ toolLabel(l.toolName) }}</p>
        <p class="sub mono">{{ l.toolName }}</p>
      </div>
      <Badge :tone="STATUS_TONE[l.status] || 'neutral'" dot>{{ STATUS_LABEL[l.status] || l.status }}</Badge>
    </div>

    <DetailSection v-if="subject.value" :title="subject.label">
      <p class="subject" :class="{ mono: subject.label === '网址' || subject.label === '任务' }">{{ subject.value }}</p>
    </DetailSection>

    <div v-if="l.errorCode || l.errorMessage" class="error-box">
      <p class="ec">{{ errorLabel(l.errorCode) || '调用失败' }}<span v-if="l.errorCode" class="mono faint"> · {{ l.errorCode }}</span></p>
      <p v-if="l.errorMessage" class="em">{{ l.errorMessage }}</p>
    </div>

    <dl class="kv">
      <div>
        <dt>API Key</dt>
        <dd><button v-if="l.apiKeyName" type="button" class="lk" @click="openApiKey(l.apiKeyName)">{{ l.apiKeyName }}</button><span v-else>—</span></dd>
      </div>
      <div><dt>时间</dt><dd>{{ dateTime(l.createdAt) }}</dd></div>
      <div><dt>耗时</dt><dd class="mono">{{ ms(l.elapsedMs) }}</dd></div>
      <div><dt>结果</dt><dd class="mono">{{ integer(l.resultCount) }} 项 · {{ bytes(l.resultBytes) }}</dd></div>
      <div v-if="l.sourceId || l.sourceType">
        <dt>来源</dt>
        <dd>
          <button v-if="isSearch && l.sourceId" type="button" class="lk mono" @click="goPage('/search', { source: l.sourceId })">{{ l.sourceId }}</button>
          <span v-else class="mono">{{ l.sourceId || l.sourceType }}</span>
        </dd>
      </div>
      <div v-if="l.provider"><dt>提供方</dt><dd class="mono">{{ l.provider }}</dd></div>
      <div v-if="l.model"><dt>模型</dt><dd><button type="button" class="lk mono" @click="openModel(l.model)">{{ l.model }}</button></dd></div>
      <div v-if="l.accountKey" class="full">
        <dt>账户</dt>
        <dd><button type="button" class="lk mono" @click="openAccount(l.accountKey)">{{ l.accountKey }}</button></dd>
      </div>
      <div v-if="l.inputTokens || l.outputTokens"><dt>Token 输入 / 输出</dt><dd class="mono">{{ integer(l.inputTokens) }} / {{ integer(l.outputTokens) }}</dd></div>
      <div v-if="l.videoRequestId" class="full"><dt>视频任务</dt><dd><CopyText :text="l.videoRequestId" /></dd></div>
      <div v-if="l.mediaTokens.length"><dt>媒体资源</dt><dd>{{ l.mediaTokens.length }} 个</dd></div>
    </dl>

    <DetailSection title="客户端">
      <dl class="kv">
        <div><dt>名称</dt><dd>{{ l.clientName ? `${l.clientName}${l.clientVersion ? ` ${l.clientVersion}` : ''}` : '未上报' }}</dd></div>
        <div><dt>协议版本</dt><dd class="mono">{{ l.protocolVersion || '—' }}</dd></div>
        <div class="full"><dt>调用 ID</dt><dd><CopyText :text="l.callId" /></dd></div>
      </dl>
    </DetailSection>

    <DetailSection title="调用结果" note="留存的结果正文">
      <McpResult :key="l.callId" :call-id="l.callId" />
    </DetailSection>

    <DetailSection v-if="params" title="调用参数">
      <pre class="json">{{ params }}</pre>
    </DetailSection>

    <p v-if="isSearch" class="hint">这次调用实际发起的上游搜索记录在「搜索」页的调用记录中。</p>
  </div>
</template>

<style scoped>
.d {
  padding: 16px 18px 28px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.hero {
  display: flex;
  align-items: center;
  gap: 12px;
}
.ic {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: 10px;
  color: var(--green);
  background: color-mix(in srgb, var(--green) 14%, transparent);
  flex-shrink: 0;
}
.ic svg {
  width: 18px;
  height: 18px;
}
.t {
  flex: 1;
  min-width: 0;
}
.name {
  font-size: 15px;
  font-weight: 650;
}
.sub {
  font-size: 11.5px;
  color: var(--text-3);
}
.subject {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  font-size: 12.5px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-all;
}
.error-box {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--red) 10%, transparent);
  color: var(--red);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ec {
  font-weight: 600;
  font-size: 12.5px;
}
.em {
  font-family: var(--font-mono);
  font-size: 12px;
  word-break: break-word;
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
.json {
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 360px;
  overflow: auto;
}
.hint {
  font-size: 11.5px;
  color: var(--text-3);
}
</style>
