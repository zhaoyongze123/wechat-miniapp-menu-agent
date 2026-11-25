import { useEffect, useState } from 'react'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDialog } from '../../components/AppDialog'
import { REJECTION_REASONS } from '../materials/format'

interface RejectPendingDialogProps {
  open: boolean
  count: number
  busy: boolean
  error: string
  onConfirm: (reason: string, detail: string) => void
  onOpenChange: (open: boolean) => void
}

/** 待检查转不合格：必须选择原因；选择“其他”时必须补充说明。 */
export function RejectPendingDialog({
  open,
  count,
  busy,
  error,
  onConfirm,
  onOpenChange,
}: RejectPendingDialogProps) {
  const [reason, setReason] = useState('')
  const [detail, setDetail] = useState('')
  const [localError, setLocalError] = useState('')

  useEffect(() => {
    if (!open) return
    setReason('')
    setDetail('')
    setLocalError('')
  }, [open])

  const submit = () => {
    if (!reason) {
      setLocalError('请选择不合格原因')
      return
    }
    if (reason === 'other' && !detail.trim()) {
      setLocalError('选择其他原因时请填写具体说明')
      return
    }
    if (detail.trim().length > 200) {
      setLocalError('补充说明最多 200 字')
      return
    }
    setLocalError('')
    onConfirm(reason, detail.trim())
  }

  return (
    <AppDialog
      open={open}
      onOpenChange={onOpenChange}
      busy={busy}
      title={`标记 ${count} 家门店为不合格`}
      widthClass="w-[min(560px,94vw)]"
      footer={
        <div className="flex justify-end gap-2">
          <Button color="secondary" variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={busy}>
            取消
          </Button>
          <Button color="danger" size="sm" onClick={submit} disabled={busy}>
            {busy ? '处理中…' : '确认标记'}
          </Button>
        </div>
      }
    >
      <div className="space-y-3 text-sm leading-relaxed text-default">
        <p>
          将从待检查清单移入不合格清单，并记录原因与说明。该操作只保存审核结论，不会自动重新采集。
        </p>
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-secondary" htmlFor="quality-reject-reason">
            不合格原因
          </label>
          <Select
            aria-label="不合格原因"
            value={reason}
            options={[
              { value: '', label: '请选择原因' },
              ...REJECTION_REASONS.map((item) => ({ value: item.code, label: item.label })),
            ]}
            onChange={(option) => setReason(option.value)}
          />
        </div>
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-secondary" htmlFor="quality-reject-detail">
            补充说明
          </label>
          <Input
            id="quality-reject-detail"
            value={detail}
            maxLength={200}
            placeholder={reason === 'other' ? '请填写其他原因（必填）' : '补充说明（可选，最多 200 字）'}
            onChange={(event) => setDetail(event.target.value)}
          />
        </div>
        {localError || error ? (
          <p className="text-xs text-danger" role="alert">
            {localError || error}
          </p>
        ) : null}
      </div>
    </AppDialog>
  )
}
