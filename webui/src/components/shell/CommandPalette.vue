<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch, type Component } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowUpRight, CornerDownLeft, LogOut, Moon, RefreshCw, Search, Sun, SunMoon } from '@lucide/vue'
import { navItems } from '@/nav'
import { logout } from '@/stores/session'
import { setTheme, ui } from '@/stores/ui'
import { openRequest } from '@/pages/live/openRequest'

interface Cmd {
  id: string
  label: string
  hint?: string
  group: string
  icon: Component
  tint?: string
  run: () => void
}

const router = useRouter()
const query = ref('')
const cursor = ref(0)
const input = ref<HTMLInputElement | null>(null)
const listRef = ref<HTMLElement | null>(null)

const REQUEST_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

const base = computed<Cmd[]>(() => [
  ...navItems.map((n) => ({
    id: `nav:${n.key}`,
    label: n.label,
    hint: n.phase > 1 ? '规划中' : undefined,
    group: '页面',
    icon: n.icon,
    tint: n.tint,
    run: () => router.push(n.to),
  })),
  { id: 'theme:auto', label: '外观：跟随系统', group: '操作', icon: SunMoon, run: () => setTheme('auto') },
  { id: 'theme:light', label: '外观：浅色', group: '操作', icon: Sun, run: () => setTheme('light') },
  { id: 'theme:dark', label: '外观：深色', group: '操作', icon: Moon, run: () => setTheme('dark') },
  { id: 'reload', label: '重新加载页面', group: '操作', icon: RefreshCw, run: () => location.reload() },
  {
    id: 'logout',
    label: '退出登录',
    group: '操作',
    icon: LogOut,
    run: async () => {
      await logout()
      router.replace({ name: 'login' })
    },
  },
])

const results = computed<Cmd[]>(() => {
  const q = query.value.trim().toLowerCase()
  const out: Cmd[] = []
  if (REQUEST_ID.test(query.value.trim())) {
    const id = query.value.trim()
    out.push({ id: `req:${id}`, label: `打开请求 ${id.slice(0, 8)}…`, group: '请求', icon: ArrowUpRight, run: () => openRequest(id) })
  }
  if (!q) return [...out, ...base.value]
  return [...out, ...base.value.filter((c) => c.label.toLowerCase().includes(q) || c.id.includes(q))]
})

const grouped = computed(() => {
  const groups: { name: string; items: { cmd: Cmd; index: number }[] }[] = []
  results.value.forEach((cmd, index) => {
    let g = groups.find((x) => x.name === cmd.group)
    if (!g) groups.push((g = { name: cmd.group, items: [] }))
    g.items.push({ cmd, index })
  })
  return groups
})

watch(
  () => ui.paletteOpen,
  async (open) => {
    if (!open) return
    query.value = ''
    cursor.value = 0
    await nextTick()
    input.value?.focus()
  },
)
watch(query, () => (cursor.value = 0))

function run(cmd: Cmd | undefined): void {
  if (!cmd) return
  ui.paletteOpen = false
  cmd.run()
}

function move(delta: number): void {
  const n = results.value.length
  if (!n) return
  cursor.value = (cursor.value + delta + n) % n
  nextTick(() => listRef.value?.querySelector('.on')?.scrollIntoView({ block: 'nearest' }))
}

function onKey(e: KeyboardEvent): void {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    ui.paletteOpen = !ui.paletteOpen
    return
  }
  if (!ui.paletteOpen) return
  if (e.key === 'Escape') ui.paletteOpen = false
  else if (e.key === 'ArrowDown') {
    e.preventDefault()
    move(1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    move(-1)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    run(results.value[cursor.value])
  }
}
onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <Transition name="palette">
    <div v-if="ui.paletteOpen" class="wrap" @mousedown.self="ui.paletteOpen = false">
      <div class="palette glass" role="dialog" aria-label="搜索">
        <div class="field">
          <Search />
          <input ref="input" v-model="query" placeholder="搜索页面、操作，或粘贴请求 ID" spellcheck="false" autocomplete="off" />
          <kbd>esc</kbd>
        </div>
        <div ref="listRef" class="list">
          <p v-if="!results.length" class="none">没有匹配的结果</p>
          <section v-for="g in grouped" :key="g.name">
            <p class="g">{{ g.name }}</p>
            <button
              v-for="{ cmd, index } in g.items"
              :key="cmd.id"
              class="row"
              :class="{ on: index === cursor }"
              @mousemove="cursor = index"
              @click="run(cmd)"
            >
              <span class="ic" :style="{ color: cmd.tint }"><component :is="cmd.icon" /></span>
              <span class="label">{{ cmd.label }}</span>
              <span v-if="cmd.hint" class="hint">{{ cmd.hint }}</span>
              <CornerDownLeft v-if="index === cursor" class="enter" />
            </button>
          </section>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.wrap {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding-top: min(16vh, 140px);
  background: var(--overlay);
}
.palette {
  width: min(600px, calc(100vw - 24px));
  max-height: min(520px, 70dvh);
  display: flex;
  flex-direction: column;
  border-radius: 14px;
  background: color-mix(in srgb, var(--surface-raised) 88%, transparent);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.field {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 52px;
  padding: 0 16px;
  border-bottom: 0.5px solid var(--border);
}
.field svg {
  width: 18px;
  height: 18px;
  color: var(--text-3);
}
.field input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: 0;
  background: none;
  font-size: 16px;
}
.field input::placeholder {
  color: var(--text-3);
}
kbd {
  padding: 2px 6px;
  border-radius: 5px;
  background: var(--fill-2);
  font-size: 11px;
  color: var(--text-3);
}
.list {
  overflow-y: auto;
  padding: 6px;
}
.g {
  padding: 8px 10px 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  height: 36px;
  padding: 0 10px;
  border-radius: 8px;
  text-align: left;
  font-size: 13.5px;
}
.row.on {
  background: var(--accent);
  color: #fff;
}
.row.on .ic,
.row.on .hint {
  color: #fff !important;
}
.ic {
  display: grid;
  place-items: center;
  color: var(--text-2);
}
.ic svg {
  width: 16px;
  height: 16px;
}
.label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hint {
  font-size: 11px;
  color: var(--text-3);
}
.enter {
  width: 14px;
  height: 14px;
  opacity: 0.8;
}
.none {
  padding: 28px;
  text-align: center;
  color: var(--text-3);
}
.palette-enter-active,
.palette-leave-active {
  transition: opacity 0.18s;
}
.palette-enter-active .palette,
.palette-leave-active .palette {
  transition: transform 0.22s var(--ease-out), opacity 0.18s;
}
.palette-enter-from,
.palette-leave-to {
  opacity: 0;
}
.palette-enter-from .palette,
.palette-leave-to .palette {
  transform: scale(0.97) translateY(-6px);
  opacity: 0;
}
</style>
