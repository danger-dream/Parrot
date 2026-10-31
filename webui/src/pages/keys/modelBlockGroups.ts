import type { Schemas } from '../../api/client'

export type ModelBlockRule = Schemas['ModelBlockRule']
export type ModelBlockGroup = Omit<ModelBlockRule, 'model'> & { id: number; models: string[] }
export type ModelOption = { value: string; hint?: string }

/** Only adjacent identical settings may merge: first matching rule wins. */
export function groupModelBlocks(rules: ModelBlockRule[]): ModelBlockGroup[] {
  const groups: ModelBlockGroup[] = []
  for (const { model, ...settings } of rules) {
    const previous = groups.at(-1)
    if (previous && previous.enabled === settings.enabled && previous.preset === settings.preset && previous.message === settings.message) {
      previous.models.push(model)
    } else {
      groups.push({ ...settings, id: groups.length, models: [model] })
    }
  }
  return groups
}

export function flattenModelBlocks(groups: ModelBlockGroup[]): ModelBlockRule[] {
  return groups.flatMap(({ id: _id, models, ...settings }) => models.map((model) => ({ ...settings, model })))
}

/** CheckList emits catalog order. Keep surviving selections in their rule order. */
export function updateModelSelection(previous: string[], next: string[], options: ModelOption[]): string[] {
  const allowed = new Set(options.map((option) => option.value))
  const selected = new Set(next.filter((model) => allowed.has(model)))
  return [...previous.filter((model) => selected.has(model)),
    ...[...selected].filter((model) => !previous.includes(model))]
}

export function modelBlockOptions(catalog: ModelOption[], saved: string[]): ModelOption[] {
  const known = new Set(catalog.map((option) => option.value))
  return [...saved.filter((model) => !known.has(model)).map((value) => ({ value, hint: '当前不可用（已保存，可保留或取消）' })), ...catalog]
}

export function modelBlockValidation(groups: ModelBlockGroup[]): string {
  if (groups.some((group) => !group.models.length)) return '请为每组规则至少选择一个模型。'
  const rules = flattenModelBlocks(groups)
  if (rules.length > 100) return '最多保存 100 条模型规则，请减少所选模型。'
  if (new Set(rules.map((rule) => rule.model)).size !== rules.length) return '同一模型不能在多组规则中重复选择。'
  if (rules.some((rule) => (rule.message?.length || 0) > 1000)) return '自定义提示最多 1000 字符，请在高级选项中修改。'
  return ''
}
