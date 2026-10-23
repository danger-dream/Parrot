<script setup lang="ts">
import { useResource } from '@/composables/useResource'
import { errorTitle } from '@/utils/errors'
import { humanSeconds } from '@/utils/format'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Field from '@/components/ui/Field.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Switch from '@/components/ui/Switch.vue'
import Segmented from '@/components/ui/Segmented.vue'
import type { FieldDef } from './fields'

// A flat settings resource rendered from field descriptors (GET + sparse PATCH with If-Match).
const props = defineProps<{ path: string; title: string; description?: string; fields: FieldDef[] }>()

type Flat = Record<string, unknown> & { revision?: string }
const res = useResource<Flat>(props.path)
const { draft, dirty, saving, loading, error } = res

function num(key: string): number | null {
  const v = draft.value?.[key]
  return typeof v === 'number' ? v : null
}
function setVal(key: string, v: unknown): void {
  if (draft.value && v !== null && v !== undefined) draft.value[key] = v
}
function human(f: FieldDef): string {
  const v = num(f.key)
  if (v === null) return ''
  if (f.zeroText && v === 0) return f.zeroText
  if (f.humanize === 'seconds' && v >= 60) return `约 ${humanSeconds(v)}`
  if (f.humanize === 'minutes' && v >= 60) return `约 ${humanSeconds(v * 60)}`
  return ''
}

defineExpose({ reload: res.load })
</script>

<template>
  <SettingsSection
    :title="title"
    :description="description"
    :dirty="dirty"
    :saving="saving"
    :loading="loading && !draft"
    :error="error ? errorTitle(error) : ''"
    @save="res.save()"
    @reset="res.reset()"
  >
    <template v-if="$slots.actions" #actions><slot name="actions" /></template>
    <template v-if="draft">
      <Field v-for="f in fields" :key="f.key" :label="f.label" :hint="f.hint" inline>
        <Switch v-if="f.type === 'bool'" :model-value="Boolean(draft[f.key])" :label="f.label" @update:model-value="setVal(f.key, $event)" />
        <Segmented
          v-else-if="f.type === 'select'"
          :model-value="String(draft[f.key])"
          :options="f.options || []"
          size="sm"
          @update:model-value="setVal(f.key, $event)"
        />
        <div v-else class="numwrap">
          <span v-if="human(f)" class="human">{{ human(f) }}</span>
          <NumberInput
            :model-value="num(f.key)"
            :min="f.min"
            :max="f.max"
            :integer="f.type === 'int'"
            :unit="f.unit"
            @update:model-value="setVal(f.key, $event)"
          />
        </div>
      </Field>
      <slot :draft="draft" />
    </template>
  </SettingsSection>
</template>

<style scoped>
.numwrap {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 4px 10px;
}
.human {
  font-size: 11.5px;
  color: var(--text-3);
  white-space: nowrap;
}
</style>
