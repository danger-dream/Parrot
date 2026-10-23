import { api, type Schemas } from '@/api/client'
import { operationErrorText, waitOperation, type Operation } from '@/api/operations'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import { syncResultLines } from './shared'

type SyncBody = Schemas['MetadataSyncRequest']

/**
 * Run syncModelMetadata (202 → Operation) in place: the caller shows a loading button,
 * the result is a short toast. Manual matches and hand-set fields are kept by the server.
 */
export async function runMetadataSync(body: SyncBody, revision?: string): Promise<boolean> {
  try {
    const res = await api.post<Operation>('/model-metadata/actions/sync', body, { ifMatch: revision || undefined })
    const op = await waitOperation(res.data)
    if (op.status !== 'succeeded') {
      toast('元数据同步未完成', 'error', operationErrorText(op), 7000)
      return false
    }
    toast('元数据已同步', 'success', syncResultLines(op.result).join(' · ') || undefined, 6000)
    return true
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    return false
  }
}
