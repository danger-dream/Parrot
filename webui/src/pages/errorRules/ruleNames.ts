import { reactive } from 'vue'
import { api } from '@/api/client'
import type { EditorSample, ErrorRulesData } from './model'

/** Rule and category names for pages that only see ids (request detail). */
export const ruleNames = reactive({
  rules: {} as Record<string, { name: string; layer: 'user' | 'preset' }>,
  categories: {} as Record<string, string>,
  loadedAt: 0,
})

let inflight: Promise<void> | null = null

export function refreshRuleNames(): Promise<void> {
  if (Date.now() - ruleNames.loadedAt < 60_000) return Promise.resolve()
  if (inflight) return inflight
  inflight = api
    .get<ErrorRulesData>('/error-rules')
    .then((res) => storeRuleNames(res.data))
    .finally(() => {
      inflight = null
    })
  return inflight
}

export function storeRuleNames(data: ErrorRulesData): void {
  const rules: typeof ruleNames.rules = {}
  for (const r of data.rules) rules[r.id] = { name: r.name, layer: 'user' }
  for (const r of data.presets) rules[r.id] = { name: r.name, layer: 'preset' }
  ruleNames.rules = rules
  ruleNames.categories = Object.fromEntries(data.categories.map((c) => [c.id, c.label]))
  ruleNames.loadedAt = Date.now()
}

const HANDOFF_KEY = 'parrot.errorRules.createFrom'

/** Carry one failed attempt to the error rules page, which opens a pre-filled editor. */
export function handOffError(sample: EditorSample): void {
  sessionStorage.setItem(HANDOFF_KEY, JSON.stringify(sample))
}

export function takeHandOff(): EditorSample | null {
  const raw = sessionStorage.getItem(HANDOFF_KEY)
  sessionStorage.removeItem(HANDOFF_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as EditorSample
  } catch {
    return null
  }
}
