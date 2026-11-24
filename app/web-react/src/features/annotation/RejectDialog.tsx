import { useEffect, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDialog } from '../../components/AppDialog'
import { REJECTION_REASONS } from './format'

interface RejectDialogProps {
  open: boolean
  taskName: string
  busy?: boolean
  error?: string
  onConfirm: (reason: string, detail: string) => void
  onOpenChange: (open: boolean) => void
}

/** 标记不合格会改变任务状态并自动进入下一家，确认前必须说明影响。 */
export function RejectDialog({ open, taskName, busy = false, error, onConfirm, onOpenChange }: RejectDialogProps) {
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
      title="标记不合格"
      widthClass="w-[min(520px,94vw)]"
      footer={
        <div className="flex justify-end gap-2">
          <Button color="secondary" variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={busy}>
            取消
          </Button>
          <Button color="danger" size="sm" onClick={submit} disabled={busy}>
            {busy ? '处理中…' : '标记并继续'}
          </Button>
        </div>
      }
    >
      <div className="space-y-3 text-sm leading-relaxed text-default">
        <p>
          门店 <span className="font-medium">{taskName}</span> 将转为待检查，并自动继续下一家。
        </p>
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-secondary" htmlFor="annotation-reject-reason">
            不合格原因
          </label>
          <Select
            aria-label="不合格原因"
            value={reason}
            options={[{ value: '', label: '请选择原因' }, ...REJECTION_REASONS]}
            onChange={(option) => setReason(option.value)}
          />
        </div>
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-secondary" htmlFor="annotation-reject-detail">
            补充说明
          </label>
          <textarea
            id="annotation-reject-detail"
            className="min-h-20 w-full rounded-md border border-subtle bg-surface px-3 py-2 text-sm text-default outline-none focus:border-primary"
            value={detail}
            maxLength={200}
            placeholder="可选，用于质量审核时判断（最多 200 字）"
            onChange={(event) => setDetail(event.target.value)}
          />
        </div>
        {localError || error ? <Alert color="danger" variant="soft" title={localError || error} /> : null}
      </div>
    </AppDialog>
  )
}
