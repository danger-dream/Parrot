import { reactive } from 'vue'

export interface ConfirmOptions {
  title: string
  detail?: string
  confirmText?: string
  cancelText?: string
  danger?: boolean
  /** User must type this exact text before confirming (for irreversible actions). */
  requireText?: string
}

export interface PromptOptions {
  title: string
  detail?: string
  label?: string
  placeholder?: string
  initial?: string
  confirmText?: string
  multiline?: boolean
  mono?: boolean
  validate?: (value: string) => string | null
}

type Pending =
  | { kind: 'confirm'; opts: ConfirmOptions; resolve: (v: boolean) => void }
  | { kind: 'prompt'; opts: PromptOptions; resolve: (v: string | null) => void }

export const dialogs = reactive<{ current: Pending | null }>({ current: null })

function dismissCurrent(): void {
  const cur = dialogs.current
  if (!cur) return
  dialogs.current = null
  if (cur.kind === 'confirm') cur.resolve(false)
  else cur.resolve(null)
}

export function confirmAction(opts: ConfirmOptions): Promise<boolean> {
  dismissCurrent()
  return new Promise((resolve) => {
    dialogs.current = { kind: 'confirm', opts, resolve }
  })
}

export function promptText(opts: PromptOptions): Promise<string | null> {
  dismissCurrent()
  return new Promise((resolve) => {
    dialogs.current = { kind: 'prompt', opts, resolve }
  })
}

export function settleDialog(value: boolean | string | null): void {
  const cur = dialogs.current
  dialogs.current = null
  if (!cur) return
  if (cur.kind === 'confirm') cur.resolve(Boolean(value))
  else cur.resolve(typeof value === 'string' ? value : null)
}
