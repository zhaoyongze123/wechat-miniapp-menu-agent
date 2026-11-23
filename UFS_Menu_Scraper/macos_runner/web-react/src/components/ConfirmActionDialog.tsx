import type { ReactNode } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { AppDialog } from './AppDialog'

interface ConfirmActionDialogProps {
  open: boolean
  title: string
  description: ReactNode
  confirmLabel: string
  tone?: 'primary' | 'danger'
  busy?: boolean
  error?: string
  onConfirm: () => void
  onOpenChange: (open: boolean) => void
}

/** 破坏性或批量操作前的确认窗口：必须展示对象与范围，确认按钮明确。 */
export function ConfirmActionDialog({
  open,
  title,
  description,
  confirmLabel,
  tone = 'primary',
  busy = false,
  error,
  onConfirm,
  onOpenChange,
}: ConfirmActionDialogProps) {
  return (
    <AppDialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      widthClass="w-[min(520px,94vw)]"
      footer={
        <div className="flex justify-end gap-2">
          <Button color="secondary" variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={busy}>
            取消
          </Button>
          <Button color={tone === 'danger' ? 'danger' : 'primary'} size="sm" onClick={onConfirm} disabled={busy}>
            {busy ? '处理中…' : confirmLabel}
          </Button>
        </div>
      }
    >
      <div className="space-y-3 text-sm leading-relaxed text-ink">
        <div>{description}</div>
        {error ? <Alert color="danger" variant="soft" title={error} /> : null}
      </div>
    </AppDialog>
  )
}
