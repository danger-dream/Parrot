<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { KeyRound, Laptop, LogOut, Send, Smartphone, TriangleAlert } from '@lucide/vue'
import { api, ApiError, enc } from '@/api/client'
import type { LoginSession } from '@/api/types'
import { logout } from '@/stores/session'
import { toast } from '@/stores/ui'
import { dateTime, relative } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Skeleton from '@/components/ui/Skeleton.vue'

const router = useRouter()
const sessions = ref<LoginSession[]>([])
const loading = ref(true)
const error = ref('')
const busy = ref('')
const confirmOthers = ref(false)

async function load(): Promise<void> {
  loading.value = true
  try {
    const res = await api.get<LoginSession[]>('/auth/sessions')
    sessions.value = [...res.data].sort((a, b) => Number(b.current) - Number(a.current) || (a.lastSeenAt < b.lastSeenAt ? 1 : -1))
    error.value = ''
  } catch (err) {
    error.value = err instanceof ApiError && (err.status === 404 || err.status === 405) ? '登录设备接口尚未启用。' : (err as Error).message
  } finally {
    loading.value = false
  }
}
onMounted(load)

const others = computed(() => sessions.value.filter((s) => !s.current))

function isPhone(s: LoginSession): boolean {
  return /手机|iOS|Android/.test(s.deviceSummary || '')
}

async function revoke(s: LoginSession): Promise<void> {
  if (s.current) {
    await logout()
    router.replace({ name: 'login' })
    return
  }
  busy.value = s.sessionId
  try {
    await api.del(`/auth/sessions/${enc(s.sessionId)}`)
    sessions.value = sessions.value.filter((x) => x.sessionId !== s.sessionId)
    toast('该设备已下线', 'success')
  } catch (err) {
    toast('操作失败', 'error', (err as Error).message)
  } finally {
    busy.value = ''
  }
}

async function revokeOthers(): Promise<void> {
  busy.value = 'others'
  try {
    const res = await api.post<{ revoked: number }>('/auth/sessions/actions/revoke-others')
    toast(`已让 ${res.data.revoked} 台设备下线`, 'success')
    confirmOthers.value = false
    await load()
  } catch (err) {
    toast('操作失败', 'error', (err as Error).message)
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <div class="page">
    <header class="head">
      <div>
        <h2>登录设备</h2>
        <p class="muted">在这里登录过、且会话仍然有效的浏览器。3 天不使用会自动失效。</p>
      </div>
      <div v-if="others.length" class="head-actions">
        <template v-if="!confirmOthers">
          <Btn variant="danger" @click="confirmOthers = true"><LogOut /> 让其他设备下线</Btn>
        </template>
        <template v-else>
          <span class="confirm-text">确定让其他 {{ others.length }} 台设备下线？</span>
          <Btn variant="danger" :loading="busy === 'others'" @click="revokeOthers">确定</Btn>
          <Btn variant="ghost" @click="confirmOthers = false">取消</Btn>
        </template>
      </div>
    </header>

    <p v-if="error" class="err"><TriangleAlert /> {{ error }}</p>
    <div v-else-if="loading" class="list"><Skeleton v-for="i in 3" :key="i" h="76px" r="14px" /></div>
    <div v-else class="list stagger">
      <article v-for="s in sessions" :key="s.sessionId" class="item" :class="{ current: s.current }">
        <span class="dev"><component :is="isPhone(s) ? Smartphone : Laptop" /></span>
        <div class="info">
          <p class="title">
            {{ s.deviceSummary || s.clientName || '未知设备' }}
            <Badge v-if="s.current" tone="blue">当前设备</Badge>
          </p>
          <p class="meta">
            <span class="method">
              <component :is="s.authMethod === 'telegramApproval' ? Send : KeyRound" />
              {{ s.authMethod === 'telegramApproval' ? 'Telegram 批准' : s.authMethod === 'managementKey' ? '管理密钥' : s.authMethod }}
            </span>
            <span v-if="s.sourceAddress" class="mono">{{ s.sourceAddress }}</span>
            <span>最近活动 {{ relative(s.lastSeenAt) }}</span>
          </p>
          <p class="meta faint">登录于 {{ dateTime(s.issuedAt) }} · 最晚 {{ dateTime(s.expiresAt) }} 失效</p>
        </div>
        <Btn :variant="s.current ? 'secondary' : 'danger'" size="sm" :loading="busy === s.sessionId" @click="revoke(s)">
          {{ s.current ? '退出登录' : '下线' }}
        </Btn>
      </article>
    </div>
  </div>
</template>

<style scoped>
.page {
  max-width: 860px;
  margin: 0 auto;
  padding: 24px 24px 32px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
}
.head h2 {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 700;
}
.head p {
  margin-top: 4px;
  font-size: 12.5px;
}
.head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.confirm-text {
  font-size: 12.5px;
  color: var(--text-2);
}
.list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.item {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 16px;
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--card-highlight), inset 0 0 0 0.5px var(--border);
}
.item.current {
  box-shadow: var(--card-highlight), inset 0 0 0 1px color-mix(in srgb, var(--accent) 45%, transparent);
}
.dev {
  width: 40px;
  height: 40px;
  border-radius: 11px;
  display: grid;
  place-items: center;
  flex-shrink: 0;
  color: var(--accent);
  background: var(--accent-soft);
}
.dev svg {
  width: 20px;
  height: 20px;
}
.info {
  flex: 1;
  min-width: 0;
}
.title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 12px;
  margin-top: 3px;
  font-size: 12px;
  color: var(--text-2);
}
.meta.faint {
  color: var(--text-3);
  font-size: 11.5px;
}
.method {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.method svg {
  width: 12px;
  height: 12px;
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
@media (max-width: 767px) {
  .page {
    padding: 16px 12px 24px;
  }
  .item {
    flex-wrap: wrap;
  }
  .item :deep(.btn) {
    margin-left: 54px;
  }
}
</style>
