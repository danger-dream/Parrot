import { reactive, shallowRef } from 'vue'
import { ApiError } from '@/api/client'
import { adoptServer, type useResource } from '@/composables/useResource'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import type { Settings } from './labels'

// The search page owns one `useResource('/search')`; the backend inspector panel mutates the same
// revisioned document (backends live inside it), so both share it through this module.

type Res = ReturnType<typeof useResource<Settings>>

export const searchRes = shallowRef<Res | null>(null)

export const searchUi = reactive({
  /** Source filter of the call log; set from the detail panel's "查看调用记录". */
  logSource: '',
  /** Bumped after a manual test so the log/stats cards refetch. */
  logsTick: 0,
})

/** Run a mutation that returns the whole settings document, with If-Match and toasts. */
export async function mutateSearch(
  fn: (revision: string | undefined) => Promise<{ data: Settings | undefined }>,
  successText?: string,
): Promise<boolean> {
  const res = searchRes.value
  if (!res) return false
  try {
    const out = await fn(res.data.value?.revision)
    if (out.data && typeof out.data === 'object') adoptServer(res, out.data)
    else await reloadKeepingDraft()
    if (successText) toast(successText, 'success')
    return true
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') {
      await reloadKeepingDraft()
      toast('搜索设置已在别处被修改', 'warning', '已载入最新数据，请重新操作。', 6000)
    } else {
      toast(errorTitle(err), 'error', errorDetail(err), 7000)
    }
    return false
  }
}

export async function reloadKeepingDraft(): Promise<void> {
  const res = searchRes.value
  if (!res) return
  const keep = res.draft.value ? JSON.parse(JSON.stringify(res.draft.value)) : null
  const before = res.data.value ? JSON.parse(JSON.stringify(res.data.value)) : null
  await res.load()
  if (keep && before && res.draft.value) {
    const d = res.draft.value as unknown as Record<string, unknown>
    for (const [k, v] of Object.entries(keep as Record<string, unknown>)) {
      if (k === 'revision' || k === 'backends') continue
      if (JSON.stringify(v) !== JSON.stringify((before as Record<string, unknown>)[k])) d[k] = v
    }
  }
}
