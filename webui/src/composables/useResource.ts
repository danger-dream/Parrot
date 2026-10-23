import { computed, ref, shallowRef, type Ref } from 'vue'
import { api, ApiError } from '@/api/client'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'

type Revisioned = { revision?: string }

function clone<T>(value: T): T {
  return value === null || value === undefined ? value : JSON.parse(JSON.stringify(value))
}

/**
 * Load a revisioned resource and edit a draft copy of it.
 * `save()` PATCHes (or PUTs) only the changed top-level fields with If-Match; a
 * REVISION_CONFLICT reloads the server copy and keeps the user's draft for review.
 */
export function useResource<T extends Revisioned>(
  path: string | (() => string),
  opts: { method?: 'PATCH' | 'PUT'; fullBody?: boolean; successText?: string; immediate?: boolean } = {},
) {
  const url = () => (typeof path === 'function' ? path() : path)
  const data = shallowRef<T | null>(null) as Ref<T | null>
  const draft = ref<T | null>(null) as Ref<T | null>
  const loading = ref(false)
  const saving = ref(false)
  const error = ref<unknown>(null)

  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const res = await api.get<T>(url())
      data.value = res.data
      draft.value = clone(res.data)
    } catch (err) {
      error.value = err
    } finally {
      loading.value = false
    }
  }

  const changes = computed<Partial<T>>(() => {
    const out: Record<string, unknown> = {}
    if (!data.value || !draft.value) return out as Partial<T>
    for (const [k, v] of Object.entries(draft.value)) {
      if (k === 'revision') continue
      if (JSON.stringify(v) !== JSON.stringify((data.value as Record<string, unknown>)[k])) out[k] = v
    }
    return out as Partial<T>
  })
  const dirty = computed(() => Object.keys(changes.value).length > 0)

  async function save(body?: Record<string, unknown>): Promise<boolean> {
    if (!data.value) return false
    const payload = body ?? (opts.fullBody ? stripRevision(draft.value) : changes.value)
    if (!body && !Object.keys(payload as object).length) return true
    saving.value = true
    try {
      const res = await (opts.method === 'PUT' ? api.put<T> : api.patch<T>)(url(), payload, { ifMatch: data.value.revision })
      if (res.data && typeof res.data === 'object') {
        data.value = res.data
        draft.value = clone(res.data)
      } else {
        await load()
      }
      toast(opts.successText || '已保存', 'success')
      return true
    } catch (err) {
      if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') {
        const keep = clone(draft.value)
        await load()
        draft.value = keep
        toast('数据已在别处被修改', 'warning', '已载入最新数据，你的修改仍保留在表单中，请检查后再保存。', 7000)
      } else {
        toast(errorTitle(err), 'error', errorDetail(err), 7000)
      }
      return false
    } finally {
      saving.value = false
    }
  }

  function reset(): void {
    draft.value = clone(data.value)
  }

  if (opts.immediate !== false) load()

  return { data, draft, loading, saving, error, dirty, changes, load, save, reset }
}

/** The part of `useResource()` the section helpers need. */
type SectionHost<T> = {
  data: Ref<T | null>
  draft: Ref<T | null>
  save: (body?: Record<string, unknown>) => Promise<boolean>
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

/**
 * Adopt a fresh server copy returned by a side action (e.g. a toggle endpoint that returns the whole
 * resource) while keeping the user's unsaved draft edits on other fields.
 */
export function adoptServer<T extends object>(res: SectionHost<T>, value: T, keepDraft = true): void {
  const before = res.data.value as Record<string, unknown> | null
  const draft = res.draft.value as Record<string, unknown> | null
  res.data.value = value
  const next = clone(value) as Record<string, unknown>
  if (keepDraft && before && draft) {
    for (const [k, v] of Object.entries(draft)) {
      if (k === 'revision') continue
      if (!same(v, before[k])) next[k] = clone(v)
    }
  }
  res.draft.value = next as T
}

/**
 * Optional sectioned saving on top of `useResource()`: several SettingsSection cards edit different
 * top-level fields of ONE revisioned resource. Each section has its own dirty/saving state, saves only
 * its own fields, and the unsaved edits of the other sections survive its save.
 */
export function useSection<T extends object>(res: SectionHost<T>, fields: (keyof T & string)[]) {
  const saving = ref(false)
  const changed = computed<Record<string, unknown>>(() => {
    const out: Record<string, unknown> = {}
    const d = res.draft.value as Record<string, unknown> | null
    const s = res.data.value as Record<string, unknown> | null
    if (!d || !s) return out
    for (const f of fields) if (!same(d[f], s[f])) out[f] = d[f]
    return out
  })
  const dirty = computed(() => Object.keys(changed.value).length > 0)

  async function save(extra?: Record<string, unknown>): Promise<boolean> {
    const body = { ...changed.value, ...(extra || {}) }
    if (!Object.keys(body).length) return true
    const before = clone(res.data.value) as Record<string, unknown> | null
    const keep = clone(res.draft.value) as Record<string, unknown> | null
    saving.value = true
    try {
      const ok = await res.save(body)
      if (ok && before && keep && res.draft.value) {
        const d = res.draft.value as Record<string, unknown>
        for (const [k, v] of Object.entries(keep)) {
          if (k === 'revision' || k in body) continue
          if (!same(v, before[k])) d[k] = v
        }
      }
      return ok
    } finally {
      saving.value = false
    }
  }

  function reset(): void {
    const d = res.draft.value as Record<string, unknown> | null
    const s = res.data.value as Record<string, unknown> | null
    if (!d || !s) return
    for (const f of fields) d[f] = clone(s[f])
  }

  return { dirty, saving, changed, save, reset }
}

function stripRevision<T extends Revisioned>(value: T | null): Partial<T> {
  if (!value) return {}
  const { revision: _revision, ...rest } = value
  return rest as Partial<T>
}

/** Run a one-off mutation with consistent toasts; returns the response data or null on failure. */
export async function runAction<R>(fn: () => Promise<{ data: R }>, successText?: string): Promise<R | null> {
  try {
    const res = await fn()
    if (successText) toast(successText, 'success')
    return res.data ?? (true as unknown as R)
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    return null
  }
}
