<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Download, ExternalLink, Film, ImageOff, Maximize2, RefreshCw, TriangleAlert } from '@lucide/vue'
import { api, API_BASE, enc, type Schemas } from '@/api/client'
import { dateTime, ms, usd } from '@/utils/format'
import { errorTitle } from '@/utils/errors'
import { openAccount, openModel, openRequest } from '@/links'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import CopyText from '@/components/ui/CopyText.vue'
import Modal from '@/components/ui/Modal.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import DetailSection from '@/components/DetailSection.vue'
import { actionLabel, bytes, CACHE_LABEL, mediaLabel, providerLabel, seconds, statusLabel, statusTone } from './labels'

type Detail = Schemas['MediaLogDetailData']
type Artifact = Schemas['MediaArtifactData']

const props = defineProps<{ id: string }>()
const data = ref<Detail | null>(null)
const error = ref('')
const artifacts = ref<Artifact[]>([])
const artError = ref('')
const artLoading = ref(false)
const preview = ref<Artifact | null>(null)
const broken = ref(new Set<string>())

async function load(): Promise<void> {
  try {
    data.value = (await api.get<Detail>(`/media-logs/${enc(props.id)}`)).data
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
    return
  }
  await loadArtifacts()
}

async function loadArtifacts(): Promise<void> {
  const d = data.value
  if (!d) return
  if (d.status === 'expired') {
    artifacts.value = []
    artError.value = '记录已过期，缓存的媒体文件不再提供。'
    return
  }
  if (!d.artifactCount) {
    artifacts.value = []
    artError.value = ''
    return
  }
  artLoading.value = true
  try {
    artifacts.value = (await api.get<Artifact[]>(`/media-logs/${enc(props.id)}/artifacts`)).data
    artError.value = ''
  } catch (err) {
    artError.value = errorTitle(err)
  } finally {
    artLoading.value = false
  }
}

// Running jobs keep moving; poll until they settle.
let timer: number | undefined
onMounted(async () => {
  await load()
  timer = window.setInterval(() => {
    if (data.value && (data.value.status === 'running' || data.value.status === 'pending')) load()
  }, 5000)
})
onBeforeUnmount(() => window.clearInterval(timer))

/** Same-origin download URL; the session cookie authorises <img>, <video> and <a download>. */
function url(a: Artifact): string {
  return `${API_BASE}/media-logs/${enc(props.id)}/artifacts/${enc(a.id)}`
}

const dims = computed(() => {
  const d = data.value
  if (!d) return '—'
  const parts = [d.aspectRatio, d.resolution].filter(Boolean)
  return parts.length ? parts.join(' · ') : '—'
})

const rows = computed(() => {
  const d = data.value
  if (!d) return []
  const out: { k: string; v: string; mono?: boolean; tone?: string }[] = [
    { k: '类型', v: `${mediaLabel(d.mediaType)} · ${actionLabel(d.action)}` },
    { k: '来源', v: providerLabel(d.provider) },
    { k: '尺寸 / 比例', v: dims.value, mono: true },
  ]
  if (d.mediaType === 'video') out.push({ k: '视频时长', v: seconds(d.durationSeconds) })
  if (d.generatedCount !== null && d.generatedCount !== undefined) out.push({ k: '生成数量', v: `${d.generatedCount} 张` })
  if (d.outputSizes?.length) out.push({ k: '输出尺寸', v: d.outputSizes.join('、'), mono: true })
  if (d.progress !== null && d.progress !== undefined && d.status !== 'success') out.push({ k: '进度', v: `${Math.round(d.progress)}%` })
  out.push(
    { k: '耗时', v: ms(d.durationMilliseconds) },
    { k: '费用', v: d.costTicks ? usd(d.costTicks, { precise: true }) : '未记录' },
    { k: '数据量', v: bytes(d.trafficBytes) },
    { k: '缓存', v: d.cacheStatus ? CACHE_LABEL[d.cacheStatus] || d.cacheStatus : '—', tone: d.cacheStatus === 'failed' ? 'red' : '' },
    { k: '创建时间', v: dateTime(d.createdAt) },
    { k: '完成时间', v: dateTime(d.finishedAt) },
    { k: 'HTTP 状态', v: d.httpStatus ? String(d.httpStatus) : '—', mono: true, tone: d.httpStatus && d.httpStatus >= 400 ? 'red' : '' },
    { k: '上游状态', v: d.upstreamStatus || '—', mono: true },
  )
  if (d.cacheErrorClass) out.push({ k: '缓存错误', v: d.cacheErrorClass, mono: true, tone: 'red' })
  return out
})
</script>

<template>
  <div class="md">
    <p v-if="error" class="err"><TriangleAlert /> {{ error }}</p>
    <div v-else-if="!data" class="skl"><Skeleton h="22px" w="50%" /><Skeleton h="160px" r="10px" /><Skeleton h="120px" r="10px" /></div>
    <template v-else>
      <div class="hero">
        <Badge :tone="statusTone(data.status) === 'neutral' ? 'neutral' : statusTone(data.status)" dot>{{ statusLabel(data.status) }}</Badge>
        <Badge>{{ mediaLabel(data.mediaType) }}{{ actionLabel(data.action) }}</Badge>
        <Badge tone="purple">{{ providerLabel(data.provider) }}</Badge>
        <span class="spacer" />
        <Btn size="sm" variant="ghost" icon title="刷新" @click="load"><RefreshCw /></Btn>
      </div>

      <div v-if="data.status === 'running' || data.status === 'pending'" class="progress">
        <div class="pbar"><i :style="{ width: `${Math.max(3, data.progress ?? 0)}%` }" /></div>
        <span class="mono">{{ data.progress !== null && data.progress !== undefined ? `${Math.round(data.progress)}%` : '等待中' }}</span>
      </div>

      <dl class="links">
        <div>
          <dt>模型</dt>
          <dd><button v-if="data.model" type="button" class="lk mono" @click="openModel(data.model)">{{ data.model }}</button><span v-else>—</span></dd>
        </div>
        <div>
          <dt>账号</dt>
          <dd>
            <button v-if="data.accountId" type="button" class="lk" :title="data.accountId" @click="openAccount(data.accountId, data.accountLabel || undefined)">
              {{ data.accountLabel || data.accountId }}
            </button>
            <span v-else>—</span>
          </dd>
        </div>
        <div>
          <dt>关联请求</dt>
          <dd><button v-if="data.requestId" type="button" class="lk mono truncate" @click="openRequest(data.requestId)">{{ data.requestId }}</button><span v-else>—</span></dd>
        </div>
      </dl>

      <DetailSection v-if="data.promptPreview" title="提示词摘要">
        <p class="prompt">{{ data.promptPreview }}</p>
      </DetailSection>

      <DetailSection v-if="data.error" title="错误">
        <p class="error-box">{{ data.error }}</p>
      </DetailSection>

      <DetailSection title="产物" :note="artifacts.length ? `${artifacts.length} 个文件` : undefined">
        <div v-if="artLoading" class="gallery"><Skeleton h="140px" r="10px" /><Skeleton h="140px" r="10px" /></div>
        <p v-else-if="artError" class="faint small">{{ artError }}</p>
        <p v-else-if="!artifacts.length" class="faint small">
          {{ data.status === 'success' ? '没有缓存的媒体文件（可能未开启缓存，或已按保留策略清理）。' : '任务完成后，缓存的媒体文件会显示在这里。' }}
        </p>
        <div v-else class="gallery">
          <figure v-for="a in artifacts" :key="a.id" class="art">
            <video v-if="a.mediaType === 'video'" :src="url(a)" controls preload="metadata" playsinline />
            <button v-else-if="!broken.has(a.id)" type="button" class="thumb" title="放大查看" @click="preview = a">
              <img :src="url(a)" :alt="a.fileName" loading="lazy" @error="broken.add(a.id)" />
              <span class="zoom"><Maximize2 /></span>
            </button>
            <div v-else class="thumb none"><ImageOff /><span>无法加载</span></div>
            <figcaption>
              <span class="fn truncate mono" :title="a.fileName"><Film v-if="a.mediaType === 'video'" class="fi" />{{ a.fileName }}</span>
              <span class="sz faint">{{ bytes(a.sizeBytes) }}</span>
              <a class="dl" :href="url(a)" :download="a.fileName" title="下载"><Download /></a>
            </figcaption>
            <p v-if="a.expiresAt" class="exp faint">{{ dateTime(a.expiresAt) }} 过期</p>
          </figure>
        </div>
      </DetailSection>

      <DetailSection title="详细信息">
        <dl class="kv">
          <div v-for="r in rows" :key="r.k">
            <dt>{{ r.k }}</dt>
            <dd :class="[{ mono: r.mono }, r.tone ? `c-${r.tone}` : '']">{{ r.v }}</dd>
          </div>
          <div class="full">
            <dt>上游任务 ID</dt>
            <dd><CopyText v-if="data.upstreamRequestId" :text="data.upstreamRequestId" /><span v-else>—</span></dd>
          </div>
          <div class="full">
            <dt>记录编号</dt>
            <dd class="mono">#{{ data.id }}</dd>
          </div>
        </dl>
      </DetailSection>

      <div class="acts">
        <Btn v-if="data.requestId" @click="openRequest(data.requestId)"><ExternalLink /> 查看关联请求</Btn>
      </div>
    </template>

    <Modal :open="Boolean(preview)" :title="preview?.fileName" width="xl" @close="preview = null">
      <div v-if="preview" class="big">
        <img :src="url(preview)" :alt="preview.fileName" />
      </div>
      <template #footer>
        <a v-if="preview" class="dl-btn" :href="url(preview)" :download="preview.fileName"><Download /> 下载</a>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
.md {
  padding: 16px 18px 28px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.skl {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.err {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
}
.err svg {
  width: 16px;
  height: 16px;
  color: var(--orange);
}
.hero {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.spacer {
  flex: 1;
}
.progress {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}
.pbar {
  flex: 1;
  height: 6px;
  border-radius: 999px;
  background: var(--fill-2);
  overflow: hidden;
}
.pbar i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--teal), var(--blue));
  transition: width 0.6s var(--ease-out);
}
.links {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.links div {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.links dt {
  width: 64px;
  flex-shrink: 0;
  font-size: 11.5px;
  color: var(--text-3);
}
.links dd {
  margin: 0;
  min-width: 0;
  font-size: 12.5px;
  display: flex;
}
.lk {
  color: var(--accent);
  text-align: left;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.lk:hover {
  text-decoration: underline;
}
.prompt {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.error-box {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  color: var(--red);
  background: color-mix(in srgb, var(--red) 10%, transparent);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}
.small {
  font-size: 12px;
}
.gallery {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 10px;
}
.art {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.art video,
.thumb {
  width: 100%;
  aspect-ratio: 1 / 1;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border);
  object-fit: contain;
  overflow: hidden;
}
.art video {
  aspect-ratio: 16 / 10;
  background: #000;
}
.art:has(video) {
  grid-column: 1 / -1;
}
.thumb {
  position: relative;
  display: grid;
  place-items: center;
  padding: 0;
}
.thumb img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.zoom {
  position: absolute;
  right: 6px;
  bottom: 6px;
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 6px;
  color: #fff;
  background: rgba(0, 0, 0, 0.45);
  opacity: 0;
  transition: opacity 0.15s;
}
.zoom svg {
  width: 13px;
  height: 13px;
}
.thumb:hover .zoom {
  opacity: 1;
}
.thumb.none {
  gap: 4px;
  font-size: 11.5px;
  color: var(--text-3);
}
.thumb.none svg {
  width: 20px;
  height: 20px;
}
figcaption {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  font-size: 11.5px;
}
.fn {
  flex: 1;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.fi {
  width: 12px;
  height: 12px;
  flex-shrink: 0;
}
.sz {
  flex-shrink: 0;
}
.dl {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  flex-shrink: 0;
  border-radius: 6px;
  color: var(--accent);
  background: var(--fill-2);
}
.dl svg {
  width: 14px;
  height: 14px;
}
.exp {
  font-size: 11px;
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
.kv dt {
  font-size: 11px;
  color: var(--text-3);
}
.kv dd {
  margin: 2px 0 0;
  font-size: 12.5px;
  word-break: break-word;
}
.acts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.big {
  display: grid;
  place-items: center;
}
.big img {
  max-width: 100%;
  max-height: 70dvh;
  border-radius: var(--radius-md);
}
.dl-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 12px;
  border-radius: var(--radius-sm);
  font-size: 12.5px;
  font-weight: 500;
  color: #fff;
  background: var(--accent);
}
.dl-btn svg {
  width: 14px;
  height: 14px;
}
</style>
