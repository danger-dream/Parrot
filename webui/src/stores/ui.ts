import { markRaw, reactive, type Component } from 'vue'

export type ThemePref = 'auto' | 'light' | 'dark'

const media = window.matchMedia('(prefers-color-scheme: dark)')

function readPref(): ThemePref {
  try {
    const v = localStorage.getItem('parrot.theme')
    return v === 'light' || v === 'dark' ? v : 'auto'
  } catch {
    return 'auto'
  }
}

function readPrivacy(): boolean {
  try {
    return localStorage.getItem('parrot.privacy') === '1'
  } catch {
    return false
  }
}

export const ui = reactive({
  theme: readPref() as ThemePref,
  /** Mask e-mails, phone numbers, API keys and IPs everywhere (see utils/privacy). */
  privacy: readPrivacy(),
  dark: document.documentElement.dataset.theme !== 'light',
  width: window.innerWidth,
  paletteOpen: false,
  sidebarOpen: false,
})

function applyTheme(): void {
  const dark = ui.theme === 'dark' || (ui.theme === 'auto' && media.matches)
  ui.dark = dark
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
}

export function setTheme(pref: ThemePref): void {
  ui.theme = pref
  try {
    localStorage.setItem('parrot.theme', pref)
  } catch {
    /* storage unavailable */
  }
  applyTheme()
}

export function setPrivacy(on: boolean): void {
  ui.privacy = on
  try {
    localStorage.setItem('parrot.privacy', on ? '1' : '0')
  } catch {
    /* storage unavailable */
  }
}

export function cycleTheme(): void {
  setTheme(ui.theme === 'auto' ? 'dark' : ui.theme === 'dark' ? 'light' : 'auto')
}

media.addEventListener('change', applyTheme)
window.addEventListener('resize', () => {
  ui.width = window.innerWidth
})

export function isWide(): boolean {
  return ui.width >= 1280
}

export function isMobile(): boolean {
  return ui.width < 768
}

export type ToastTone = 'info' | 'success' | 'warning' | 'error'

export interface Toast {
  id: number
  tone: ToastTone
  title: string
  detail?: string
}

let toastSeq = 0
export const toasts = reactive<Toast[]>([])

export function toast(title: string, tone: ToastTone = 'info', detail?: string, ttl = 4200): void {
  const id = ++toastSeq
  toasts.push({ id, tone, title, detail })
  if (toasts.length > 4) toasts.shift()
  window.setTimeout(() => dismissToast(id), ttl)
}

export function dismissToast(id: number): void {
  const index = toasts.findIndex((t) => t.id === id)
  if (index >= 0) toasts.splice(index, 1)
}

export interface InspectorState {
  open: boolean
  title: string
  subtitle?: string
  component: Component | null
  props: Record<string, unknown>
  key: string
}

export const inspector = reactive<InspectorState>({
  open: false,
  title: '',
  component: null,
  props: {},
  key: '',
})

type InspectorEntry = Pick<InspectorState, 'title' | 'subtitle' | 'component' | 'props' | 'key'>

// Panels opened from inside another panel stack up so the user can step back.
export const inspectorHistory = reactive<InspectorEntry[]>([])
let lastInsideInteraction = 0

export function markInspectorInteraction(): void {
  lastInsideInteraction = Date.now()
}

export function openInspector(key: string, title: string, component: Component, props: Record<string, unknown> = {}, subtitle?: string): void {
  const fromInside = Date.now() - lastInsideInteraction < 1500
  if (inspector.open && fromInside && inspector.key && inspector.key !== key && inspector.component) {
    inspectorHistory.push({ key: inspector.key, title: inspector.title, subtitle: inspector.subtitle, component: inspector.component, props: inspector.props })
    if (inspectorHistory.length > 20) inspectorHistory.shift()
  } else if (!fromInside) {
    inspectorHistory.splice(0)
  }
  Object.assign(inspector, { open: true, title, subtitle, component: markRaw(component), props, key })
}

export function backInspector(): void {
  const prev = inspectorHistory.pop()
  if (!prev) return
  Object.assign(inspector, { open: true, ...prev })
}

export function closeInspector(): void {
  inspector.open = false
  inspector.key = ''
  inspectorHistory.splice(0)
}
