import { createRouter, createWebHistory, type RouteLocationNormalizedLoaded } from 'vue-router'
import { loadSession, session, setSignedOutHandler } from '@/stores/session'
import { findNav, navItems } from '@/nav'
import { modulePages } from '@/pageRegistry'
import { closeInspector } from '@/stores/ui'

export const router = createRouter({
  history: createWebHistory('/ui/'),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/pages/LoginPage.vue'), meta: { public: true, title: '登录' } },
    {
      path: '/',
      component: () => import('@/components/shell/AppShell.vue'),
      children: [
        { path: '', name: 'overview', component: () => import('@/pages/overview/OverviewPage.vue'), meta: { title: '概览' } },
        { path: 'live', name: 'live', component: () => import('@/pages/live/LivePage.vue'), meta: { title: '实时请求' } },
        { path: 'devices', name: 'devices', component: () => import('@/pages/DevicesPage.vue'), meta: { title: '登录设备' } },
        ...navItems
          .filter((item) => modulePages[item.key])
          .map((item) => ({ path: item.to.slice(1), name: item.key, component: modulePages[item.key], meta: { title: item.label } })),
        { path: 'planned/:key', redirect: (to: { params: Record<string, unknown> }) => findNav(String(to.params.key))?.to || '/' },
      ],
    },
    { path: '/:rest(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  if (session.status === 'unknown') {
    try {
      await loadSession()
    } catch {
      /* network failure: treat as anonymous so the login page can explain */
    }
  }
  if (to.meta.public) {
    if (to.name === 'login' && session.status === 'authenticated') return { name: 'overview' }
    return true
  }
  if (session.status !== 'authenticated') {
    return { name: 'login', query: to.fullPath !== '/' ? { next: to.fullPath } : {} }
  }
  return true
})

export function routeTitle(route: RouteLocationNormalizedLoaded): string {
  return route.meta.title || 'Parrot'
}

router.afterEach((to, from) => {
  if (to.path !== from.path) {
    closeInspector()
    // Pages share one scrolling container, so a new page would otherwise open at the old offset.
    document.querySelector('.content')?.scrollTo({ top: 0 })
  }
  const title = routeTitle(to)
  document.title = title === 'Parrot' ? title : `${title} · Parrot`
})

setSignedOutHandler(() => {
  const current = router.currentRoute.value
  if (!current.meta.public) router.replace({ name: 'login', query: { next: current.fullPath, expired: '1' } })
})
