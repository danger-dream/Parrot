import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api } from '@/api/client'
import { notifyProxiesChanged, proxyBus, type Proxy, type ProxyGroup, type Routing } from '@/pages/proxies/meta'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'

export type ProxyRouteSection = 'accounts' | 'channels' | 'models'

const HINTS: Record<ProxyRouteSection, { own: string; inherit: string }> = {
  accounts: { own: '单独设置，优先于模型规则、上游类型和默认路线', inherit: '未单独设置，按模型规则、上游类型、默认路线依次匹配' },
  channels: { own: '单独设置，优先于模型规则、上游类型和默认路线', inherit: '未单独设置，按模型规则、上游类型、默认路线依次匹配' },
  models: { own: '请求这个模型时走它；账号或渠道单独设置的出口优先', inherit: '未单独设置，按账号/渠道规则、上游类型、默认路线依次匹配' },
}

/**
 * One account / channel / model rule of the proxy routing table, edited in place from a detail view.
 * `key` is the routing key: accountId, `api:<name>` or the model id.
 */
export function useProxyRoute(section: ProxyRouteSection, key: () => string) {
  const routing = ref<Routing | null>(null)
  const proxies = ref<Proxy[]>([])
  const groups = ref<ProxyGroup[]>([])
  const failed = ref(false)
  const saving = ref(false)

  async function load(): Promise<void> {
    try {
      const [r, p, g] = await Promise.all([
        api.get<Routing>('/proxy-routing'),
        api.get<Proxy[]>('/proxies', { pageSize: 200 }),
        api.get<ProxyGroup[]>('/proxy-groups', { pageSize: 200 }),
      ])
      routing.value = r.data
      proxies.value = p.data
      groups.value = g.data
      failed.value = false
    } catch {
      failed.value = true
    }
  }
  onMounted(load)
  watch(() => proxyBus.version, load)

  const rules = computed(() => (routing.value?.[section] || {}) as Record<string, string>)
  const value = computed(() => rules.value[key()] || '')
  const known = computed(() => new Set(['direct', ...proxies.value.map((p) => p.name), ...groups.value.map((g) => g.name)]))

  const options = computed(() => {
    const list = [
      { value: '', label: '继承（不单独设置）' },
      { value: 'direct', label: '直连' },
      ...groups.value.map((g) => ({ value: g.name, label: `${g.name}（代理组）` })),
      ...proxies.value.map((p) => ({ value: p.name, label: p.name })),
    ]
    return value.value && !known.value.has(value.value) ? [{ value: value.value, label: `${value.value}（已不存在）` }, ...list] : list
  })

  const hint = computed(() => {
    if (failed.value) return '代理设置读取失败，可到「代理」页修改'
    if (value.value && routing.value && !known.value.has(value.value)) return `指向的「${value.value}」已不存在，请重新选择`
    return value.value ? HINTS[section].own : HINTS[section].inherit
  })

  async function set(target: string): Promise<void> {
    if (!routing.value || saving.value || target === value.value) return
    saving.value = true
    try {
      const change = { [section]: { [key()]: target || null } }
      routing.value = (await api.patch<Routing>('/proxy-routing', change, { ifMatch: routing.value.revision })).data
      toast(target ? `出口已改为${target === 'direct' ? '直连' : ` ${target}`}` : '已改为继承', 'success')
      notifyProxiesChanged()
    } catch (err) {
      toast(errorTitle(err), 'error', errorDetail(err), 7000)
      load()
    } finally {
      saving.value = false
    }
  }

  return reactive({ value, options, hint, saving, ready: computed(() => Boolean(routing.value)), set })
}
