import { defineAsyncComponent } from 'vue'
import { openInspector } from '@/stores/ui'

const RequestDetail = defineAsyncComponent(() => import('./RequestDetail.vue'))

export function openRequest(id: string): void {
  openInspector(`req:${id}`, '请求详情', RequestDetail, { id }, id)
}
