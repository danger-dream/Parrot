import type { RouteComponent } from 'vue-router'

// Lazy page modules for every navigation entry outside the core shell pages.
export const modulePages: Record<string, () => Promise<RouteComponent>> = {
  analytics: () => import('@/pages/analytics/AnalyticsPage.vue'),
  media: () => import('@/pages/media/MediaLogsPage.vue'),
  oauth: () => import('@/pages/oauth/OAuthPage.vue'),
  channels: () => import('@/pages/channels/ChannelsPage.vue'),
  keys: () => import('@/pages/keys/KeysPage.vue'),
  proxies: () => import('@/pages/proxies/ProxiesPage.vue'),
  models: () => import('@/pages/models/ModelsPage.vue'),
  routing: () => import('@/pages/routing/RoutingPage.vue'),
  search: () => import('@/pages/search/SearchPage.vue'),
  mcp: () => import('@/pages/mcp/McpPage.vue'),
  translation: () => import('@/pages/translation/TranslationPage.vue'),
  imaging: () => import('@/pages/imaging/ImagingPage.vue'),
  settings: () => import('@/pages/settings/SettingsPage.vue'),
  network: () => import('@/pages/network/NetworkPage.vue'),
  alerts: () => import('@/pages/alerts/AlertsPage.vue'),
  blacklist: () => import('@/pages/blacklist/BlacklistPage.vue'),
  retention: () => import('@/pages/retention/RetentionPage.vue'),
  updates: () => import('@/pages/updates/UpdatesPage.vue'),
}
