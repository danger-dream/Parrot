<script setup lang="ts">
import { computed, ref } from 'vue'
import { FileText, TriangleAlert } from '@lucide/vue'
import { api, ApiError, enc } from '@/api/client'
import { dateTime, integer } from '@/utils/format'
import { errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import CopyText from '@/components/ui/CopyText.vue'
import Skeleton from '@/components/ui/Skeleton.vue'

// TG「📨 查看结果」：读取 getMcpCallResult，搜索结果按条目展开，抓取正文按原文显示，其余格式化 JSON。

interface CallResult {
  callId: string
  createdAt: number
  body: string | null
  purgedAt?: number | null
}
interface Hit {
  title: string
  url: string
  snippet: string
}

const props = defineProps<{ callId: string }>()

const state = ref<'idle' | 'loading' | 'done' | 'missing' | 'error'>('idle')
const result = ref<CallResult | null>(null)
const error = ref('')
const expanded = ref(false)

async function load(): Promise<void> {
  state.value = 'loading'
  try {
    result.value = (await api.get<CallResult>(`/mcp/logs/${enc(props.callId)}/result`)).data
    state.value = 'done'
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) state.value = 'missing'
    else {
      error.value = errorTitle(err)
      state.value = 'error'
    }
  }
}

const parsed = computed(() => {
  const text = (result.value?.body || '').trim()
  const out = { subject: '', answer: '', warnings: [] as string[], hits: [] as Hit[], truncated: false, content: '', json: '', raw: text }
  if (!text.startsWith('{')) return out
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    return out
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return out
  const d = data as Record<string, unknown>
  out.subject = String(d.query || d.url || '')
  out.answer = typeof d.answer === 'string' ? d.answer : ''
  if (Array.isArray(d.warnings)) out.warnings = d.warnings.map(String)
  if (Array.isArray(d.results) && d.results.length) {
    out.hits = d.results.map((item) => {
      if (!item || typeof item !== 'object') return { title: String(item), url: '', snippet: '' }
      const r = item as Record<string, unknown>
      return {
        title: String(r.title || r.name || '').trim(),
        url: String(r.url || r.link || '').trim(),
        snippet: String(r.snippet || r.content || r.description || '').trim(),
      }
    })
    out.truncated = Boolean(d.truncated)
    out.raw = ''
    return out
  }
  if (typeof d.content === 'string' && d.content.trim()) {
    out.content = d.content
    out.raw = ''
    return out
  }
  out.json = JSON.stringify(d, null, 2)
  out.raw = ''
  return out
})

const longText = computed(() => (parsed.value.content || parsed.value.json || parsed.value.raw).length > 2400)
function safeUrl(u: string): string | undefined {
  return /^https?:\/\//i.test(u) ? u : undefined
}
</script>

<template>
  <div class="mr">
    <Btn v-if="state === 'idle'" @click="load"><FileText /> 查看结果</Btn>
    <div v-else-if="state === 'loading'" class="skl"><Skeleton h="14px" /><Skeleton h="14px" w="80%" /><Skeleton h="14px" w="60%" /></div>
    <p v-else-if="state === 'error'" class="warn"><TriangleAlert /> {{ error }}</p>
    <div v-else-if="state === 'missing' || (result && !result.body && !result.purgedAt)" class="note">
      <p class="nt">这条记录没有留存结果内容</p>
      <p>可能原因：调用失败（没有结果可存），或「日志保留 → 保存完整请求」处于关闭状态，或记录已被保留策略清理。</p>
    </div>
    <div v-else-if="result && !result.body && result.purgedAt" class="note">
      <p class="nt">结果内容已按正文保留策略清理</p>
      <p>清理时间：{{ dateTime(result.purgedAt) }}。调用摘要、来源与用量等信息仍保留。</p>
    </div>
    <template v-else-if="result && result.body">
      <div class="meta">
        <span class="faint">共 {{ integer(result.body.length) }} 字符</span>
        <CopyText :text="result.body" display="复制原文" :mono="false" />
      </div>
      <p v-if="parsed.subject" class="kv"><span class="k">查询</span><span class="mono">{{ parsed.subject }}</span></p>
      <p v-if="parsed.answer" class="kv"><span class="k">答案</span><span>{{ parsed.answer }}</span></p>
      <p v-if="parsed.warnings.length" class="kv"><span class="k">提示</span><span>{{ parsed.warnings.join('；') }}</span></p>
      <ol v-if="parsed.hits.length" class="hits">
        <li v-for="(h, i) in parsed.hits" :key="i">
          <a v-if="safeUrl(h.url)" :href="safeUrl(h.url)" target="_blank" rel="noopener noreferrer" class="ht">{{ h.title || h.url }}</a>
          <span v-else class="ht plain">{{ h.title || '（无标题）' }}</span>
          <span v-if="h.url" class="hu mono">{{ h.url }}</span>
          <span v-if="h.snippet" class="hs">{{ h.snippet }}</span>
        </li>
      </ol>
      <p v-if="parsed.truncated" class="faint small">上游标记结果已截断</p>
      <template v-if="!parsed.hits.length">
        <pre class="body" :class="{ clip: longText && !expanded, mono: Boolean(parsed.json) }">{{ parsed.content || parsed.json || parsed.raw }}</pre>
        <Btn v-if="longText" size="sm" variant="ghost" @click="expanded = !expanded">{{ expanded ? '收起' : '展开全部' }}</Btn>
      </template>
    </template>
  </div>
</template>

<style scoped>
.mr {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  min-width: 0;
}
.mr > * {
  max-width: 100%;
}
.skl {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}
.warn {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--orange);
}
.warn svg {
  width: 14px;
  height: 14px;
}
.note {
  width: 100%;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-3);
}
.nt {
  font-weight: 600;
  color: var(--text-2);
  margin-bottom: 2px;
}
.meta {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  font-size: 11.5px;
}
.meta :deep(.t) {
  color: var(--accent);
}
.kv {
  display: flex;
  gap: 8px;
  font-size: 12.5px;
  word-break: break-word;
}
.k {
  flex-shrink: 0;
  color: var(--text-3);
}
.hits {
  margin: 0;
  padding: 0 0 0 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  font-size: 12px;
}
.hits li {
  min-width: 0;
}
.ht {
  display: block;
  color: var(--accent);
  font-weight: 550;
  word-break: break-word;
}
a.ht:hover {
  text-decoration: underline;
}
.ht.plain {
  color: var(--text);
}
.hu {
  display: block;
  font-size: 11px;
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hs {
  display: block;
  margin-top: 2px;
  color: var(--text-2);
  line-height: 1.5;
  word-break: break-word;
}
.body {
  width: 100%;
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.body.mono {
  font-family: var(--font-mono);
  font-size: 11.5px;
}
.body.clip {
  max-height: 360px;
  overflow: hidden;
  -webkit-mask-image: linear-gradient(180deg, #000 80%, transparent);
  mask-image: linear-gradient(180deg, #000 80%, transparent);
}
.small {
  font-size: 11.5px;
}
</style>
