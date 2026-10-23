import { ref, shallowRef } from 'vue'
import { ApiError } from '@/api/client'
import { waitOperation, type Operation } from '@/api/operations'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'

// Follow a 202 Operation with live progress (see README “Long operations”).

/** Chinese text for a failed Operation (its error code is mapped like an API error). */
export function operationFailureText(op: Operation): string {
  if (op.status === 'cancelled') return '任务已取消'
  const e = op.error
  if (!e) return '任务失败'
  return errorTitle(new ApiError(0, { code: e.code, message: e.message }))
}

/**
 * Start a 202 Operation and follow it to a terminal state, exposing live progress.
 * `run()` resolves with the final Operation (or null when starting/polling failed).
 */
export function useOperation() {
  const op = shallowRef<Operation | null>(null)
  const running = ref(false)
  const error = ref('')

  async function run(start: () => Promise<{ data: Operation }>, opts: { intervalMs?: number; quiet?: boolean } = {}): Promise<Operation | null> {
    running.value = true
    error.value = ''
    op.value = null
    try {
      const res = await start()
      op.value = res.data
      const final = await waitOperation(res.data, {
        intervalMs: opts.intervalMs ?? 800,
        onProgress: (o) => {
          op.value = o
        },
      })
      op.value = final
      if (final.status !== 'succeeded') {
        error.value = operationFailureText(final)
        if (!opts.quiet) toast(error.value, 'error')
      }
      return final
    } catch (err) {
      error.value = errorTitle(err)
      if (!opts.quiet) toast(errorTitle(err), 'error', errorDetail(err), 7000)
      return null
    } finally {
      running.value = false
    }
  }

  function reset(): void {
    op.value = null
    error.value = ''
  }

  return { op, running, error, run, reset }
}
