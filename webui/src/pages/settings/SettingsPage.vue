<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowUpRight } from '@lucide/vue'
import { goPage } from '@/links'
import Page from '@/components/ui/Page.vue'
import FieldsSection from './FieldsSection.vue'
import RetrySection from './RetrySection.vue'
import CooldownSection from './CooldownSection.vue'
import NotificationSection from './NotificationSection.vue'
import WebUiSection from './WebUiSection.vue'
import RuntimeCard from './RuntimeCard.vue'
import {
  AFFINITY_FIELDS,
  API_KEY_CONCURRENCY_FIELDS,
  CCH_FIELDS,
  CONCURRENCY_FIELDS,
  QUOTA_MONITOR_FIELDS,
  SCORING_FIELDS,
  TIMEOUT_FIELDS,
  WEBSOCKET_FIELDS,
} from './fields'

const NAV: { group: string; items: { id: string; label: string }[] }[] = [
  {
    group: '请求处理',
    items: [
      { id: 'retry', label: '重试' },
      { id: 'timeouts', label: '超时' },
      { id: 'cooldown', label: '错误冷却' },
    ],
  },
  {
    group: '调度',
    items: [
      { id: 'scoring', label: '渠道评分' },
      { id: 'affinity', label: '会话亲和' },
      { id: 'cch', label: 'CCH 伪装' },
    ],
  },
  {
    group: '并发与额度',
    items: [
      { id: 'concurrency', label: '渠道并发' },
      { id: 'apikey', label: 'API Key 限流' },
      { id: 'quota', label: '额度监控' },
    ],
  },
  {
    group: '其他',
    items: [
      { id: 'notifications', label: '通知' },
      { id: 'websocket', label: 'OpenAI WebSocket' },
      { id: 'webui', label: '网页控制台' },
      { id: 'runtime', label: '运行状态' },
    ],
  },
]

const route = useRoute()
const router = useRouter()
const active = ref('retry')
const root = ref<HTMLElement | null>(null)

function scrollTo(id: string, smooth = true): void {
  const el = document.getElementById(`set-${id}`)
  if (!el) return
  active.value = id
  el.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' })
  if (route.query.section !== id) router.replace({ query: { ...route.query, section: id } })
}

let io: IntersectionObserver | null = null
onMounted(async () => {
  await nextTick()
  const sections = root.value?.querySelectorAll<HTMLElement>('[data-sec]') || []
  io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) if (e.isIntersecting) active.value = (e.target as HTMLElement).dataset.sec || active.value
    },
    { rootMargin: '-15% 0px -70% 0px' },
  )
  sections.forEach((s) => io?.observe(s))
  const q = String(route.query.section || '')
  // Sections load independently; give them a moment to reach full height before jumping.
  if (q) window.setTimeout(() => scrollTo(q, false), 400)
})
onBeforeUnmount(() => io?.disconnect())
</script>

<template>
  <Page title="系统设置" subtitle="网关的运行参数。每组单独保存，修改后立即生效，无需重启。" width="normal">
    <div ref="root" class="layout">
      <nav class="nav" aria-label="设置分组">
        <div v-for="g in NAV" :key="g.group" class="ng">
          <p class="ngt">{{ g.group }}</p>
          <button v-for="it in g.items" :key="it.id" type="button" :class="{ on: active === it.id }" @click="scrollTo(it.id)">{{ it.label }}</button>
        </div>
      </nav>

      <div class="secs">
        <div id="set-retry" data-sec="retry" class="anchor"><RetrySection /></div>
        <div id="set-timeouts" data-sec="timeouts" class="anchor">
          <FieldsSection path="/settings/timeouts" title="超时" description="向上游发请求时的各类等待上限，单位为秒，均需为正整数。" :fields="TIMEOUT_FIELDS" />
        </div>
        <div id="set-cooldown" data-sec="cooldown" class="anchor"><CooldownSection /></div>
        <div id="set-scoring" data-sec="scoring" class="anchor">
          <FieldsSection
            path="/settings/scoring"
            title="渠道评分"
            description="负载均衡为「Smart」模式时，按各渠道最近的成功率和速度打分排序。一般保持默认即可。"
            :fields="SCORING_FIELDS"
          >
            <template #actions>
              <button type="button" class="jump" @click="goPage('/routing')">负载均衡 <ArrowUpRight /></button>
            </template>
          </FieldsSection>
        </div>
        <div id="set-affinity" data-sec="affinity" class="anchor">
          <FieldsSection path="/settings/affinity" title="会话亲和" description="让同一会话尽量落在同一渠道，提高缓存命中、减少费用。" :fields="AFFINITY_FIELDS" />
        </div>
        <div id="set-cch" data-sec="cch" class="anchor">
          <FieldsSection path="/settings/cch" title="CCH 伪装" description="向 Claude 上游发送 Claude Code 风格的校验头。只在上游要求时开启。" :fields="CCH_FIELDS" />
        </div>
        <div id="set-concurrency" data-sec="concurrency" class="anchor">
          <FieldsSection path="/settings/concurrency" title="渠道并发" description="限制每个上游渠道同时处理的请求数，避免把单个账号或渠道打满。" :fields="CONCURRENCY_FIELDS">
            <template #actions>
              <button type="button" class="jump" @click="goPage('/channels')">API 渠道 <ArrowUpRight /></button>
            </template>
          </FieldsSection>
        </div>
        <div id="set-apikey" data-sec="apikey" class="anchor">
          <FieldsSection
            path="/settings/api-key-concurrency"
            title="API Key 限流"
            description="限制每个下游 API Key 的并发与排队。这里是默认值，单个 Key 可以在 Key 详情里单独设置。"
            :fields="API_KEY_CONCURRENCY_FIELDS"
          >
            <template #actions>
              <button type="button" class="jump" @click="goPage('/keys')">下游 API Key <ArrowUpRight /></button>
            </template>
          </FieldsSection>
        </div>
        <div id="set-quota" data-sec="quota" class="anchor">
          <FieldsSection
            path="/settings/quota-monitor"
            title="OAuth 额度监控"
            description="自动在额度快用完时暂停账号，避免请求失败或产生额外费用。默认关闭。"
            :fields="QUOTA_MONITOR_FIELDS"
          >
            <template #actions>
              <button type="button" class="jump" @click="goPage('/oauth')">OAuth 账户 <ArrowUpRight /></button>
            </template>
          </FieldsSection>
        </div>
        <div id="set-notifications" data-sec="notifications" class="anchor"><NotificationSection /></div>
        <div id="set-websocket" data-sec="websocket" class="anchor">
          <FieldsSection path="/settings/openai-websocket" title="OpenAI WebSocket" description="OpenAI Responses 接口的上游连接方式。" :fields="WEBSOCKET_FIELDS" />
        </div>
        <div id="set-webui" data-sec="webui" class="anchor"><WebUiSection /></div>
        <div id="set-runtime" data-sec="runtime" class="anchor"><RuntimeCard /></div>
      </div>
    </div>
  </Page>
</template>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: 168px minmax(0, 1fr);
  gap: 20px;
  align-items: start;
}
.nav {
  position: sticky;
  top: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-top: 2px;
}
.ng {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.ngt {
  padding: 0 10px 2px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
}
.nav button {
  height: 28px;
  padding: 0 10px;
  border-radius: var(--radius-sm);
  text-align: left;
  font-size: 12.5px;
  color: var(--text-2);
  white-space: nowrap;
  transition: background 0.15s, color 0.15s;
}
.nav button:hover {
  background: var(--fill-3);
  color: var(--text);
}
.nav button.on {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 500;
}
.secs {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}
.anchor {
  scroll-margin-top: 16px;
}
.jump {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  min-height: 28px;
  font-size: 12px;
  color: var(--accent);
  white-space: nowrap;
}
.jump svg {
  width: 13px;
  height: 13px;
}
@container (max-width: 860px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
    gap: 12px;
  }
  .nav {
    position: sticky;
    top: -16px;
    z-index: 4;
    flex-direction: row;
    gap: 4px;
    margin: 0 -12px;
    padding: 8px 12px;
    overflow-x: auto;
    scrollbar-width: none;
    background: color-mix(in srgb, var(--bg) 80%, transparent);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border-bottom: 0.5px solid var(--hairline);
  }
  .nav::-webkit-scrollbar {
    display: none;
  }
  .ng {
    flex-direction: row;
    gap: 4px;
  }
  .ngt {
    display: none;
  }
  .nav button {
    height: 32px;
    flex-shrink: 0;
    background: var(--fill-3);
    border-radius: 999px;
    padding: 0 12px;
  }
  .anchor {
    scroll-margin-top: 60px;
  }
}
</style>
