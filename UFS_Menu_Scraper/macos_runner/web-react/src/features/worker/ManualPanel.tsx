import { useEffect, useRef, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { StatusBadge } from '../../components/StatusBadge'
import { formatStore, manualStageLabel, REJECTION_REASON_OPTIONS } from './format'
import type { ManualState } from './types'

interface ManualPanelProps {
  manual: ManualState
  busy: boolean
  error: string
  onCapture: () => void
  onReject: (reason: string, detail: string) => void
  onSkip?: () => void
  compact?: boolean
}

/**
 * 半自动人工面板：F8 截图，F9 标记不合格（复用上次原因）。
 * 服务端以 manual.state 为准，本面板只表达当前门店与可用动作。
 */
export function ManualPanel({ manual, busy, error, onCapture, onReject, onSkip, compact = false }: ManualPanelProps) {
  const panel = useRef<HTMLElement>(null)
  const [reason, setReason] = useState(manual.reason || 'blur')
  const [detail, setDetail] = useState('')
  const [rejectOpen, setRejectOpen] = useState(false)

  useEffect(() => {
    setDetail('')
    setRejectOpen(false)
    if (manual.reason) setReason(manual.reason)
  }, [manual.task_id, manual.wait_id, manual.reason])

  useEffect(() => {
    const ownerDocument = panel.current?.ownerDocument
    if (!manual.waiting || !ownerDocument) return
    const handler = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (event.repeat || event.altKey || event.ctrlKey || event.metaKey
        || target?.closest?.('input,textarea,select,[contenteditable], [role=combobox]') || rejectOpen) return
      if (event.key === 'F8') {
        event.preventDefault()
        if (!busy) onCapture()
      }
      if (event.key === 'F9') {
        event.preventDefault()
        setRejectOpen(true)
      }
    }
    ownerDocument.addEventListener('keydown', handler)
    return () => ownerDocument.removeEventListener('keydown', handler)
  }, [manual.waiting, busy, onCapture, rejectOpen, compact])

  const store = formatStore(manual.store_name)
  const needDetail = reason === 'other'

  return (
    <section ref={panel} className="rounded-xl border border-subtle bg-surface p-4" aria-label="半自动人工采集">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs text-secondary">
            第 {manual.index} / {manual.total} 家 · 半自动人工
          </p>
          <h2 className="heading-sm truncate text-default">{store.name || manual.store_name}</h2>
          <p className="mt-0.5 text-xs text-secondary">
            编号 {store.code || '—'} · {manualStageLabel(manual)}
            {manual.reason ? ` · 上次原因：${REJECTION_REASON_OPTIONS.find((item) => item.value === manual.reason)?.label ?? manual.reason}` : ''}
          </p>
        </div>
        <StatusBadge tone={manual.waiting ? 'warning' : 'info'}>
          {manual.waiting ? '等待操作' : '处理中'}
        </StatusBadge>
      </div>

      {manual.last_result ? (
        <p className="mt-3 rounded-lg bg-surface px-3 py-2 text-xs text-secondary">{manual.last_result}</p>
      ) : null}

      {manual.waiting ? (
        <div className="mt-3 space-y-3">
          <p className="text-xs text-secondary">请在微信小程序进入菜单，再点击开始截图。</p>
          {!compact ? <a className="text-sm underline" href="/remote-control" target="menu-capture-manual-control">打开置顶控制窗</a> : null}
          <div className="flex flex-wrap gap-2">
            <Button color="primary" size="sm" disabled={busy} onClick={onCapture}>
              开始截图 <kbd className="ml-1 opacity-70">F8</kbd>
            </Button>
            <Button color="danger" variant="outline" size="sm" disabled={busy} onClick={() => setRejectOpen((open) => !open)}>
              标记不合格 <kbd className="ml-1 opacity-70">F9</kbd>
            </Button>
            {onSkip ? (
              <Button color="secondary" variant="ghost" size="sm" disabled={busy} onClick={onSkip}>
                跳过本店
              </Button>
            ) : null}
          </div>
          {rejectOpen ? (
            <div className="space-y-2 rounded-lg border border-subtle bg-surface p-3">
              <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
                {compact ? (
                  <select
                    className="w-full rounded-md border border-subtle bg-surface p-2 text-sm"
                    aria-label="不合格原因"
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                  >
                    {REJECTION_REASON_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                ) : (
                  <Select
                    aria-label="不合格原因"
                    value={reason}
                    options={REJECTION_REASON_OPTIONS}
                    onChange={(option) => setReason(option.value)}
                  />
                )}
                <Input
                  aria-label="补充说明"
                  placeholder={needDetail ? '请补充具体原因' : '补充说明（可选）'}
                  value={detail}
                  maxLength={200}
                  onChange={(event) => setDetail(event.target.value)}
                />
              </div>
              <div className="flex justify-end gap-2">
                <Button color="secondary" variant="ghost" size="sm" onClick={() => setRejectOpen(false)}>
                  取消
                </Button>
                <Button
                  color="danger"
                  size="sm"
                  disabled={busy || (needDetail && !detail.trim())}
                  onClick={() => onReject(reason, detail.trim())}
                >
                  提交不合格
                </Button>
              </div>
            </div>
          ) : null}
        </div>
      ) : (
        <p className="mt-3 text-xs text-secondary">正在保存本店结果，请稍候…</p>
      )}

      {error ? (
        <div className="mt-3">
          <Alert color="danger" variant="soft" title={error} />
        </div>
      ) : null}
    </section>
  )
}
