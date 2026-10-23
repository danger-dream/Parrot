<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ImagePlay, RefreshCw } from '@lucide/vue'
import { api } from '@/api/client'
import { goPage } from '@/links'
import Page from '@/components/ui/Page.vue'
import Btn from '@/components/ui/Btn.vue'
import MediaSettingsCard from './MediaSettingsCard.vue'
import SourcesCard from './SourcesCard.vue'
import ImageAccounts from './ImageAccounts.vue'
import XaiModels from './XaiModels.vue'

const imageModels = ref<string[]>([])
const videoModels = ref<string[]>([])

async function loadModelChoices(): Promise<void> {
  const list = async (type: string) => {
    try {
      return (await api.get<{ modelId: string }[]>('/models', { type, pageSize: 200 })).data.map((m) => m.modelId)
    } catch {
      return []
    }
  }
  ;[imageModels.value, videoModels.value] = await Promise.all([list('image'), list('video')])
}
onMounted(loadModelChoices)

const img = ref<InstanceType<typeof MediaSettingsCard> | null>(null)
const vid = ref<InstanceType<typeof MediaSettingsCard> | null>(null)
const imgSrc = ref<InstanceType<typeof SourcesCard> | null>(null)
const vidSrc = ref<InstanceType<typeof SourcesCard> | null>(null)
const accounts = ref<InstanceType<typeof ImageAccounts> | null>(null)
const xai = ref<InstanceType<typeof XaiModels> | null>(null)

/** Model names and account authorisations are shared between panels; refresh the dependants. */
function onModelsChanged(): void {
  img.value?.reload()
  vid.value?.reload()
  loadModelChoices()
}
function onSourcesChanged(): void {
  imgSrc.value?.reload()
  vidSrc.value?.reload()
  accounts.value?.reload()
}
function refreshAll(): void {
  onModelsChanged()
  onSourcesChanged()
  xai.value?.reload()
}
</script>

<template>
  <Page title="图片与视频" subtitle="图片 / 视频生成接口、缓存、可用账号与 xAI 媒体模型。">
    <template #actions>
      <Btn @click="goPage('/media')"><ImagePlay /> 查看媒体日志</Btn>
      <Btn icon title="刷新" @click="refreshAll"><RefreshCw /></Btn>
    </template>

    <div class="pair">
      <MediaSettingsCard ref="img" kind="image" :model-choices="imageModels" />
      <MediaSettingsCard ref="vid" kind="video" :model-choices="videoModels" />
    </div>

    <div class="pair">
      <SourcesCard ref="imgSrc" kind="image" @changed="onSourcesChanged" />
      <SourcesCard ref="vidSrc" kind="video" @changed="onSourcesChanged" />
    </div>

    <ImageAccounts ref="accounts" @changed="onSourcesChanged" />

    <XaiModels ref="xai" @changed="onModelsChanged" />
  </Page>
</template>

<style scoped>
.pair {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: stretch;
}
.pair > * {
  height: 100%;
}
@container (max-width: 900px) {
  .pair {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
