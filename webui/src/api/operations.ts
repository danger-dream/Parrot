import { api, enc, type Schemas } from './client'

export type Operation = Schemas['ManagementOperationData']

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled'])

export function isOperation(value: unknown): value is Operation {
  return Boolean(value && typeof value === 'object' && 'id' in value && 'status' in value && 'kind' in value)
}

/** Poll a 202 Operation until it reaches a terminal state. */
export async function waitOperation(
  op: Operation | string,
  opts: { onProgress?: (op: Operation) => void; intervalMs?: number; signal?: AbortSignal } = {},
): Promise<Operation> {
  let current: Operation | null = typeof op === 'string' ? null : op
  const id = typeof op === 'string' ? op : op.id
  while (!current || !TERMINAL.has(current.status)) {
    if (current) {
      opts.onProgress?.(current)
      await new Promise((r) => setTimeout(r, opts.intervalMs ?? 1000))
    }
    if (opts.signal?.aborted) throw new DOMException('aborted', 'AbortError')
    current = (await api.get<Operation>(`/operations/${enc(id)}`, undefined, opts.signal)).data
  }
  opts.onProgress?.(current)
  return current
}

export async function cancelOperation(id: string): Promise<void> {
  await api.del(`/operations/${enc(id)}`)
}

export function operationErrorText(op: Operation): string {
  const e = op.error as { message?: string; code?: string } | null
  return e?.message || e?.code || '任务失败'
}
